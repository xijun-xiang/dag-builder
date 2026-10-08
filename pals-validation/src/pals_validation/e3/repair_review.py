"""Offline repair review only. Never generate, grade code, or authorize a resume.

Classify sealed / interrupted / untouched batches without changing the frozen
schedule. Compare parsers on identical raw bytes and retain all invalid rows.
"""
from collections import Counter, defaultdict
from pathlib import Path

from ..io import digest, read, sha256
from .greedy_parse import parse as parse_v2
from .greedy_parse_v3 import parse as parse_v3, VERSION
from .greedy_recovery import parse_equivalent
from .schema import require


def inventory(folder: Path) -> dict:
    manifest = read(folder.parent / 'manifest.json')
    require(manifest['protocol_id'] == digest({k:v for k,v in manifest.items() if k!='protocol_id'}),
            'source manifest identity')
    batches = read(folder/'batches.json')
    require(sha256(folder/'batches.json') == manifest['files'][f'{folder.name}/batches.json'],
            'source schedule hash')
    planned = {b['batch_id']+'.json':b for b in batches}
    require(len(planned)==len(batches), 'duplicate batch')
    ids=[i for b in batches for i in b['problem_ids']]
    require(len(ids)==len(set(ids)), 'duplicate question')
    state={kind:{p.name for p in (folder/kind).glob('*.json')}
           for kind in ('attempts','raw','receipts')}
    require(all(names <= set(planned) for names in state.values()), 'unknown batch evidence')
    require(state['raw'] == state['receipts'] and state['raw'] <= state['attempts'],
            'unsealed raw or orphan receipt: review manually')
    rows=[]
    hashes={str(folder.parent/'manifest.json'):sha256(folder.parent/'manifest.json'),
            str(folder/'batches.json'):sha256(folder/'batches.json')}
    for name,batch in planned.items():
        status='untouched'
        if name in state['attempts']:
            a=read(folder/'attempts'/name)
            require(a['protocol_id']==manifest['protocol_id'] and a['batch']==batch, 'attempt identity')
            hashes[str(folder/'attempts'/name)]=sha256(folder/'attempts'/name)
            status='infrastructure_interrupted_na'
        if name in state['raw']:
            path=folder/'raw'/name
            raw,receipt=read(path),read(folder/'receipts'/name)
            require(receipt=={'protocol_id':manifest['protocol_id'],'sha256':sha256(path)}, 'raw hash')
            require(raw['protocol_id']==manifest['protocol_id'] and raw['batch']==batch, 'raw identity')
            output=raw['output']
            require(output['seed']==batch['seed'] and output['batch_size']==len(batch['problem_ids']) and
                    [r['problem_id'] for r in output['rows']]==batch['problem_ids'], 'raw schedule differs')
            hashes[str(path)]=sha256(path)
            hashes[str(folder/'receipts'/name)]=sha256(folder/'receipts'/name)
            status='sealed'
        rows.extend({'problem_id':item,'benchmark':batch['benchmark'],'batch_id':batch['batch_id'],
                     'state':status,'generation_permitted':status=='untouched'} for item in batch['problem_ids'])
    return {'source_protocol_id':manifest['protocol_id'],'planned':len(ids),
            'counts':dict(Counter(r['state'] for r in rows)),'items':rows,'source_hashes':hashes,
            'resume_authorized':False}


def replay(folder: Path) -> dict:
    """Bounded model folder, no manifest promotion and no inferred g scores."""
    items=[]; files={}; valid_change=0
    for path in sorted((folder/'raw').glob('*.json')):
        raw=read(path); receipt=read(folder/'receipts'/path.name)
        require(receipt=={'protocol_id':raw['protocol_id'],'sha256':sha256(path)}, 'raw hash mismatch')
        files[str(path)]=sha256(path)
        for row in raw['output']['rows']:
            text,finish,benchmark=row['raw_text'],row['finish_reason'],raw['batch']['benchmark']
            before=parse_v2(text,finish,benchmark); after=parse_v3(text,finish,benchmark)
            equivalent=parse_equivalent(before,after)
            if before['process_valid']:
                require(equivalent,'previously valid spans/answer/status changed')
            require(before['answer']==after['answer'], 'answer extraction changed')
            for step in after['steps']:
                require(text[slice(*step['span'])]==step['text'], 'non-lossless extraction')
            valid_change+=int(before['process_valid'] and not equivalent)
            items.append({'problem_id':row['problem_id'],'benchmark':benchmark,'raw_file':str(path),
                          'finish_reason':finish,'generated_tokens':len(row['generated_token_ids']),
                          'before_valid':before['process_valid'],'after_valid':after['process_valid'],
                          'before_scoreable':before['process_valid'] and len(before['steps'])>=2,
                          'after_scoreable':after['process_valid'] and len(after['steps'])>=2,
                          'before_reason':before['reason'],'after_reason':after['reason'],
                          'old_valid_unchanged':not before['process_valid'] or equivalent,
                          'segmentation_methods':after['segmentation_methods'],
                          'step_count':len(after['steps'])})
    require(items and len(items)==len({r['problem_id'] for r in items}), 'empty/duplicate replay')
    groups=defaultdict(list)
    for r in items:groups[r['benchmark']].append(r)
    def summary(rows):
        return {'total':len(rows),**{key:sum(r[key] for r in rows) for key in (
            'before_valid','after_valid','before_scoreable','after_scoreable')},
            'remaining_invalid':dict(Counter(r['after_reason'] for r in rows if not r['after_valid']))}
    return {'candidate_parser':VERSION,'replay_kind':'CPU structural replay; no new probabilities',
            'old_valid_changed':valid_change,'summary':summary(items),
            'by_benchmark':{k:summary(v) for k,v in groups.items()},'items':items,'source_hashes':files}
