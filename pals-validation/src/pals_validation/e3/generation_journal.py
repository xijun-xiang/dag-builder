"""Append-only progress and early completed-row evidence; not a retry mechanism.

No text is printed in progress logs. Incomplete batches still fail the existing
no-retry guard: a future explicit recovery must audit these row checkpoints.
"""
import json
from pathlib import Path
import time

from ..io import digest, read, save
from .schema import require


class GenerationJournal:
    def __init__(self, folder: Path, protocol_id: str, batch: dict, config: dict,
                 clock=time.monotonic, interval=30.):
        require(interval > 0, 'invalid heartbeat interval')
        require(batch['batch_id'] == digest({k:v for k,v in batch.items() if k!='batch_id'}),
                'batch id differs')
        self.directory=folder/'generation-journal'/batch['batch_id']
        require(not self.directory.exists(), 'prior generation journal: no automatic retry')
        self.directory.mkdir(parents=True, mode=0o700)
        (self.directory/'rows').mkdir(mode=0o700)
        self.identity={'protocol_id':protocol_id,'batch':batch,'config_sha256':digest(config)}
        self.ids=set(batch['problem_ids']);self.done=set()
        require(len(self.ids)==len(batch['problem_ids']), 'duplicate batch question')
        self.clock,self.interval=clock,interval
        self.started=clock();self.last=self.started;self.sequence=0
        self.last_tokens=0;self.last_finished=0
        save(self.directory/'identity.json',self.identity)
        self._event('generation_started',0,0)

    def _event(self,phase,tokens,finished):
        elapsed=max(0.,self.clock()-self.started)
        event={'protocol_id':self.identity['protocol_id'],'batch_id':self.identity['batch']['batch_id'],
               'sequence':self.sequence,'phase':phase,'elapsed_seconds':elapsed,
               'decode_steps':tokens,'finished_rows':finished,'planned_rows':len(self.ids)}
        save(self.directory/f'event-{self.sequence:06d}.json',event)
        self.sequence+=1
        print('E3_GENERATION_PROGRESS '+json.dumps(event,sort_keys=True),flush=True)

    def progress(self,tokens: int,finished: int):
        require(tokens>=self.last_tokens and self.last_finished<=finished<=len(self.ids),
                'generation progress went backwards')
        self.last_tokens,self.last_finished=tokens,finished
        if self.clock()-self.last>=self.interval:
            self._event('generating',tokens,finished)
            self.last=self.clock()

    def row(self,row: dict):
        item=row['problem_id']
        require(item in self.ids and item not in self.done,'unexpected/duplicate completed row')
        require(row['finish_reason'] in ('boundary','eos','length'), 'unknown row finish')
        record={'protocol_id':self.identity['protocol_id'],
                'batch_id':self.identity['batch']['batch_id'],
                'config_sha256':self.identity['config_sha256'],
                'row_sha256':digest(row),'row':row}
        save(self.directory/'rows'/(digest(item)+'.json'),record)
        self.done.add(item)

    def complete(self,output: dict):
        require(self.done==self.ids and len(output['rows'])==len(self.ids), 'checkpoint coverage incomplete')
        require([r['problem_id'] for r in output['rows']]==self.identity['batch']['problem_ids'],
                'checkpoint output question order differs')
        for row in output['rows']:
            record=read(self.directory/'rows'/(digest(row['problem_id'])+'.json'))
            require(record=={'protocol_id':self.identity['protocol_id'],
                    'batch_id':self.identity['batch']['batch_id'],
                    'config_sha256':self.identity['config_sha256'],
                    'row_sha256':digest(row),'row':row}, 'checkpoint mismatch')
        save(self.directory/'complete.json',{'protocol_id':self.identity['protocol_id'],
            'output_sha256':digest(output),'rows':len(self.ids)})
        self._event('batch_returned',self.last_tokens,len(self.ids))
