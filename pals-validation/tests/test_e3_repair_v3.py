"""CPU-only acceptance of opt-in parsing and semantics-preserving stop optimization."""
from pathlib import Path
import random
import unittest
from unittest.mock import patch
import sys
import types
import tempfile

from pals_validation.e3.backend import AnswerBoundaryTracker, E3HFBackend, E3MockBackend
from pals_validation.e3.greedy_parse import parse as parse_v2
from pals_validation.e3.greedy_parse_v3 import parse, VERSION
from pals_validation.e3.greedy_recovery import parse_equivalent
from pals_validation.e3.protocol import score_text_pair
from pals_validation.e3.generation_journal import GenerationJournal
from pals_validation.e3.repair_review import inventory, replay
from pals_validation.io import digest,read,save


class ParserV3Tests(unittest.TestCase):
    def test_redundant_close_lossless_and_deletion(self):
        raw='<step>Title A</step> Body A.</step>\n<step>Title B</step> Body B.</step><answer>B</answer>'
        self.assertFalse(parse_v2(raw,'boundary','gpqa')['process_valid'])
        p=parse(raw,'boundary','gpqa')
        self.assertTrue(p['process_valid'])
        self.assertEqual(p['segmentation_methods'],['title_body_redundant_close_boundary']*2)
        full,deleted,target=score_text_pair('PROMPT',p,1)
        self.assertEqual(target,'Title B</step> Body B.')
        self.assertIn('Title A</step> Body A.',full)
        self.assertNotIn('Body A.',deleted)
        for s in p['steps']:
            self.assertEqual(raw[slice(*s['span'])],s['text'])
        self.assertEqual(p['answer'],parse_v2(raw,'boundary','gpqa')['answer'])

    def test_all_old_valid_spans_exact(self):
        for raw in ('<step>A</step><step>B</step><answer>X</answer>',
                    '<step>A</step> body<step>B</step> body<answer>X</answer>',
                    '<step>A<step>B<answer>X</answer>',
                    '<step>A</step\n<step>B</step><answer>X</answer>'):
            before=parse_v2(raw,'boundary','gpqa')
            self.assertTrue(before['process_valid'])
            self.assertTrue(parse_equivalent(before,parse(raw,'boundary','gpqa')))

    def test_no_new_answer_or_implicit_step_or_truncation(self):
        bad=(
            '<step>Title</step> body</step>',
            '<step>Title</step> body</step><step>unfinished',
            '<step>Title</step></step><answer>A</answer>',
            '<step>Title</step> body</step></step><answer>A</answer>',
            '<step>Title</step> body</step> more prose<answer>A</answer>',
            '<step>Title</step> body</step extra><answer>A</answer>',
            'preface<step>A</step>body</step><answer>A</answer>',
            '<step>A</step>body</step><answer>A</answer>tail',
            '<step>A</step>body</step><answer>A</answer><answer>B</answer>',
            '<step>1. First. 2. Second.</step>',
        )
        for raw in bad:
            self.assertFalse(parse(raw,'eos','gpqa')['process_valid'],raw)
        p=parse('<step>A</step>body</step><step>B</step><answer>unfinished','length','gpqa')
        self.assertTrue(p['process_valid'])
        self.assertFalse(p['answer']['valid'])

    def test_single_step_stays_single(self):
        p=parse('<step>Title</step> First sentence. Second sentence.</step><answer>A</answer>','boundary','gpqa')
        self.assertTrue(p['process_valid'])
        self.assertEqual(len(p['steps']),1)
        self.assertEqual(p['parser_version'],VERSION)


class Decoder:
    def decode(self,tokens,skip_special_tokens=True):
        return ''.join(chr(t) for t in tokens if t!=0)


class StopOptimizationTests(unittest.TestCase):
    def test_old_and_new_token_by_token_identical(self):
        rng=random.Random(79)
        for width in (1,7,129,1500):
            outputs=['x'*n+end for n,end in ((0,'</answer>'),(60,'</answer>'),
                       (64,'\0'),(193,'z'),(201,'</answer>tail'))]
            old=AnswerBoundaryTracker(Decoder(),width,len(outputs),[0])
            new=AnswerBoundaryTracker(Decoder(),width,len(outputs),[0])
            prompts=[[rng.randrange(32,120) for _ in range(width)] for _ in outputs]
            for length in range(1,max(map(len,outputs))+1):
                rows=[pr+list(map(ord,o[:length]))+[0]*max(0,length-len(o)) for pr,o in zip(prompts,outputs)]
                tails=[r[max(width,len(r)-64):] for r in rows]
                self.assertEqual(old.update(rows),new.update_tails(tails,length))
                self.assertEqual(old.lengths,new.lengths)
                self.assertEqual(old.reasons,new.reasons)

    def test_callback_transfers_only_generated_tail(self):
        calls=[]
        class Tensor:
            shape=(2,10001)
            device='fake-cuda'
            def __getitem__(self,key):
                calls.append(key)
                class Slice:
                    def tolist(self):return [[ord('a')]*64 for _ in range(2)]
                return Slice()
            def tolist(self):raise AssertionError('whole-history transfer')
        torch=types.SimpleNamespace(bool='bool',tensor=lambda x,**kw:x)
        with patch.dict(sys.modules,{'torch':torch}):
            tracker=AnswerBoundaryTracker(Decoder(),9990,2,[0])
            # This fake batch has only 11 generated tokens; no prompt leakage.
            class Short(Tensor):
                def __getitem__(self,key):
                    calls.append(key)
                    return types.SimpleNamespace(tolist=lambda:[[ord('a')]*11 for _ in range(2)])
            self.assertEqual(tracker(Short(),None),[False,False])
        self.assertEqual(calls[0][1].start,9990)

    def test_tail_shape_rejected(self):
        t=AnswerBoundaryTracker(Decoder(),3,1,[0])
        for tails,length in (([],1),([[2]],0),([[2]],65)):
            with self.assertRaises(ValueError):t.update_tails(tails,length)

    def test_checkpoint_once_at_individual_stop_before_batch_finishes(self):
        import numpy as np
        finished=[];progress=[]
        class Tensor:
            device='fake-cuda'
            def __init__(self,a):self.a=np.array(a);self.shape=self.a.shape
            def __getitem__(self,key):return Tensor(self.a[key])
            def tolist(self):return self.a.tolist()
        torch=types.SimpleNamespace(bool='bool',tensor=lambda x,**kw:x)
        t=AnswerBoundaryTracker(Decoder(),2,2,[0],
                on_finished=lambda *a:finished.append(a),on_progress=lambda *a:progress.append(a))
        a='<answer>A</answer>';b='a'*len(a)
        with patch.dict(sys.modules,{'torch':torch}):
            self.assertEqual(t(Tensor([[1,1]+list(map(ord,a)),[1,1]+list(map(ord,b))]),None),[True,False])
            self.assertEqual(len(finished),1)
            self.assertEqual(finished[0],(0,list(map(ord,a)),'boundary'))
            self.assertEqual(t(Tensor([[1,1]+list(map(ord,a))+[0],[1,1]+list(map(ord,b))+[0]]),None),[True,True])
        self.assertEqual(len(finished),2)
        self.assertEqual(finished[1][2],'eos')
        self.assertEqual(progress[-1],(len(a)+1,2))


class JournalTests(unittest.TestCase):
    def test_sealed_rows_survive_unfinished_batch_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);now=[0.]
            b={'problem_ids':['a','b'],'benchmark':'gpqa','seed':1};b['batch_id']=digest(b)
            j=GenerationJournal(folder,'p',b,{},clock=lambda:now[0],interval=30.)
            row={'problem_id':'a','finish_reason':'boundary','raw_text':'RAW'}
            j.row(row)
            self.assertEqual(read(j.directory/'rows'/(digest('a')+'.json'))['row'],row)
            self.assertFalse((j.directory/'complete.json').exists())
            with self.assertRaisesRegex(ValueError,'duplicate'):j.row(row)
            with self.assertRaisesRegex(ValueError,'no automatic retry'):GenerationJournal(folder,'p',b,{})
            now[0]=31.;j.progress(50,1)
            self.assertEqual(read(j.directory/'event-000001.json')['decode_steps'],50)
            row2={'problem_id':'b','finish_reason':'length','raw_text':'truncated'}
            j.row(row2)
            with self.assertRaisesRegex(ValueError,'order'):j.complete({'rows':[row,row]})
            j.complete({'rows':[row,row2]})
            self.assertTrue((j.directory/'complete.json').exists())

    def test_bad_checkpoint_aborts_sealing(self):
        with tempfile.TemporaryDirectory() as tmp:
            b={'problem_ids':['a'],'benchmark':'gpqa','seed':1};b['batch_id']=digest(b)
            j=GenerationJournal(Path(tmp),'p',b,{})
            r={'problem_id':'a','finish_reason':'eos','raw_text':'RAW'};j.row(r)
            with self.assertRaisesRegex(ValueError,'mismatch'):
                j.complete({'rows':[{**r,'raw_text':'tampered'}]})
            self.assertFalse((j.directory/'complete.json').exists())


class InventoryTests(unittest.TestCase):
    def test_complete_interrupted_untouched_partition_and_tamper(self):
        from test_e3_greedy import fixture
        from pals_validation.e3 import greedy
        from pals_validation.e3.backend import E3MockBackend
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(Path(tmp).resolve());slot='qwen25'
            m,c,p,bs=greedy.load(root,slot)
            greedy.process_batch(root,slot,bs[0],E3MockBackend('',c),m,p)
            save(root/slot/'attempts'/(bs[1]['batch_id']+'.json'),{'protocol_id':m['protocol_id'],'batch':bs[1]})
            r=inventory(root/slot)
            self.assertEqual(r['counts'],{'sealed':1,'infrastructure_interrupted_na':1,'untouched':3})
            self.assertFalse(r['resume_authorized'])
            self.assertEqual(sum(x['generation_permitted'] for x in r['items']),3)
            rr=replay(root/slot)
            self.assertEqual(rr['summary']['total'],1)
            self.assertEqual(rr['old_valid_changed'],0)
            (root/slot/'receipts'/(bs[0]['batch_id']+'.json')).write_text('{}')
            with self.assertRaisesRegex(ValueError,'raw hash'):inventory(root/slot)


class JournalIntegrationTests(unittest.TestCase):
    class OfflineHF(E3HFBackend):
        """Exercise the production journal branch without a model or GPU."""
        def __init__(self,config,interrupt=False):
            self.e3_config=config;self.interrupt=interrupt;self.calls=0
            self.mock=E3MockBackend('',config)
            self.tokenizer=types.SimpleNamespace(encode=lambda s,**kw:list(s.encode()))
        def base_prompt(self,problem):return self.mock.base_prompt(problem)
        def score_pair(self,*args):return self.mock.score_pair(*args)
        def generate_batch(self,problems,seed,*,on_row,on_progress):
            self.calls+=1
            out=self.mock.generate_batch(problems,seed)
            for row in out['rows']:on_row(row)
            on_progress(max(len(r['generated_token_ids']) for r in out['rows']),len(out['rows']))
            if self.interrupt:raise RuntimeError('synthetic interruption after completed row')
            return out

    def test_complete_path_seals_identical_raw_and_does_not_regenerate(self):
        from test_e3_greedy import fixture
        from pals_validation.e3 import greedy
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(Path(tmp).resolve());m,c,p,bs=greedy.load(root,'qwen25')
            backend=self.OfflineHF(c);batch=bs[0]
            first=greedy.process_batch(root,'qwen25',batch,backend,m,p)
            folder=root/'qwen25';raw=greedy.raw_batch(folder,batch,m['protocol_id'])
            expected=backend.mock.generate_batch([p[i] for i in batch['problem_ids']],batch['seed'])
            self.assertEqual(raw['output'],expected)
            self.assertEqual(read(folder/'generation-journal'/batch['batch_id']/'complete.json')['output_sha256'],digest(expected))
            self.assertEqual(greedy.process_batch(root,'qwen25',batch,backend,m,p),first)
            self.assertEqual(backend.calls,1)

    def test_interrupted_path_keeps_row_but_refuses_automatic_retry(self):
        from test_e3_greedy import fixture
        from pals_validation.e3 import greedy
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(Path(tmp).resolve());m,c,p,bs=greedy.load(root,'qwen25')
            backend=self.OfflineHF(c,interrupt=True);batch=bs[0];folder=root/'qwen25'
            with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):
                greedy.process_batch(root,'qwen25',batch,backend,m,p)
            self.assertTrue((folder/'attempts'/(batch['batch_id']+'.json')).exists())
            self.assertTrue((folder/'generation-journal'/batch['batch_id']/'rows'/(digest(batch['problem_ids'][0])+'.json')).exists())
            self.assertFalse((folder/'raw'/(batch['batch_id']+'.json')).exists())
            self.assertFalse((folder/'receipts'/(batch['batch_id']+'.json')).exists())
            with self.assertRaisesRegex(ValueError,'UNCERTAIN_GENERATION_NO_RETRY'):
                greedy.process_batch(root,'qwen25',batch,backend,m,p)
            self.assertEqual(backend.calls,1)


if __name__=='__main__':unittest.main()
