"""CPU-only whole-cell scheduler invariance and fail-closed acceptance."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from pals_validation.backend import MockBackend
from pals_validation.io import save,read,digest,sha256
from pals_validation.protocol import parse_step
from pals_validation.recovery import e2_dynamic as D
from pals_validation.recovery import e2_formal as F


def fixture(folder,n=40):
    root=Path(folder);run=root/'mmlu/formal'
    for name in ('inputs','workers','generations','generation-attempts','results','eos-results','dispatch'):(run/name).mkdir(parents=True)
    jobs=[{'job_id':str(i),'item_id':'q','prefix_ids':[1],'deleted_id':1,'temperature':[.3,.7,1.2][i%3],'kind':'e2'} for i in range(n)]
    save(run/'jobs.json',jobs);save(run/'inputs/cases.json',[{'item_id':'q'}])
    save(run/'dispatch/state.json',{'next':8,'jobs_sha256':sha256(run/'jobs.json')})
    cfg={'seed':2026091507,'repeats':8}
    backend=MockBackend(None,{})
    def generate(case,prefix,temp,repeats,seed):
        return {'rows':[{'repeat':i,'seed':seed,'raw_text':'Reason.','finish_reason':'eos',
            'parse':parse_step('Reason.'),'generated_token_ids':[1,2,99]} for i in range(repeats)],
            'prompt':'fixture','prompt_token_ids':[1],'generation_contract':{'eos_token_ids':[99],'max_new_tokens':4096}}
    backend.generate=generate
    return root,run,jobs,cfg,backend


class QueueTests(unittest.TestCase):
    def test_filesystem_selftest_no_generation(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            D.queue_selftest(Path(d))
            r=read(Path(d)/'queue-selftest/result.json')
            self.assertEqual(r['status'],'PASS');self.assertFalse(r['scientific_evidence']);self.assertEqual(r['generation_calls'],0)

    def test_eight_reserved_first_jobs_and_work_stealing(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            _,run,jobs,cfg,_=fixture(d,80)
            def work(i):
                owned=[D.claim(run,jobs,cfg,i,True)[0]['job_id']]
                while True:
                    result=D.claim(run,jobs,cfg,i)
                    if result is None:return owned
                    owned.append(result[0]['job_id'])
                    if i==7:time.sleep(.002)
            with ThreadPoolExecutor(max_workers=8) as pool: owned=list(pool.map(work,range(8)))
            self.assertEqual([s[0] for s in owned],[str(i) for i in range(8)])
            alljobs=[j for group in owned for j in group]
            self.assertEqual(len(alljobs),80);self.assertEqual(set(alljobs),{str(i) for i in range(80)})
            for i,group in enumerate(owned):
                for jid in group:
                    a=read(run/'generation-attempts'/(jid+'.json'))
                    self.assertEqual(a['worker'],i);self.assertEqual(a['original_index'],int(jid))
                    self.assertEqual(a['seed'],int(digest([cfg['seed'],jid])[:8],16))

    def test_first_worker_cannot_claim_another_workers_reserved_job(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            _,r,j,c,_=fixture(d,10)
            self.assertEqual(D.claim(r,j,c,0,True)[0]['job_id'],'0')
            self.assertEqual(D.claim(r,j,c,0)[0]['job_id'],'8')
            self.assertEqual(D.claim(r,j,c,0)[0]['job_id'],'9')
            self.assertIsNone(D.claim(r,j,c,0))
            self.assertEqual(D.claim(r,j,c,7,True)[0]['job_id'],'7')

    def test_uncertain_claim_blocks_without_advancing(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            _,r,j,c,_=fixture(d);save(r/'generation-attempts/8.json',{'uncertain':True})
            with self.assertRaises(FileExistsError):D.claim(r,j,c,0)
            self.assertEqual(read(r/'dispatch/state.json')['next'],8)

    def test_partial_failure_after_claim_does_not_reissue(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            _,r,j,c,_=fixture(d)
            with patch.object(D,'replace_cursor',side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):D.claim(r,j,c,0)
            with self.assertRaises(FileExistsError):D.claim(r,j,c,1)

    def test_existing_raw_and_invalid_cursor_fail(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            _,r,j,c,_=fixture(d);save(r/'generations/0.json',{})
            with self.assertRaisesRegex(ValueError,'existing result'):D.claim(r,j,c,0,True)
            D.replace_cursor(r/'dispatch/state.json',{'next':99,'jobs_sha256':sha256(r/'jobs.json')})
            with self.assertRaisesRegex(ValueError,'queue identity'):D.claim(r,j,c,1)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        capture=contextlib.redirect_stdout(io.StringIO());capture.__enter__();self.addCleanup(capture.__exit__,None,None,None)

    def test_original_scientific_outputs_equal_static_execution(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            ra,run,jobs,cfg,backend=fixture(a)
            rb,run_b,_,_,backend_b=fixture(b)
            with patch.object(D,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)):
                for i in range(8):D.process_phase(ra,'formal',i,backend)
            with patch.object(F,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)):
                for i in range(8):F.process_phase(rb,'mmlu','formal',i,backend_b)
            D.owners(run,jobs)
            for j in jobs:
                for folder in ('generations','results','eos-results'):
                    self.assertEqual(read(run/folder/(j['job_id']+'.json')),read(run_b/folder/(j['job_id']+'.json')))
            self.assertGreater(read(run/'workers/0/completion.json')['jobs'],len(jobs)//8)

    def test_generation_error_keeps_attempt_and_prevents_restart(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            root,r,j,c,b=fixture(d)
            with patch.object(D,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},c)),patch.object(b,'generate',side_effect=RuntimeError('failure')):
                with self.assertRaises(RuntimeError):D.process_phase(root,'formal',0,b)
            self.assertTrue((r/'generation-attempts/0.json').exists());self.assertFalse((r/'generations/0.json').exists())
            with self.assertRaises(FileExistsError):D.claim(r,j,c,0,True)

    def test_changed_worker_receipt_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            root,r,j,c,b=fixture(d)
            with patch.object(D,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},c)):
                for i in range(8):D.process_phase(root,'formal',i,b)
            p=r/'generation-attempts/8.json';a=read(p);D.replace_cursor(p,{**a,'worker':7})
            with self.assertRaisesRegex(ValueError,'claim ownership'):D.owners(r,j)

    def test_duplicate_and_modified_result_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            root,r,j,c,b=fixture(d)
            with patch.object(D,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},c)):
                for i in range(8):D.process_phase(root,'formal',i,b)
            p=r/'results/0.json';value=read(p);value['status']='bad';D.replace_cursor(p,value)
            with self.assertRaisesRegex(ValueError,'result hash'):D.owners(r,j)


class PreparationTests(unittest.TestCase):
    def test_frozen_inputs_and_python_source_copied_without_reinitialization(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}):
            base=Path(d);source=base/'old';root=base/'new';root.mkdir();files={}
            cfg={'seed':2026091507,'repeats':8,'temperatures':[.3,.7,1.2],'max_new_tokens':4096,'max_context':8192,'model_path':str(base/'model')}
            (source/'mmlu').mkdir(parents=True);save(source/'mmlu/config.json',cfg)
            files['mmlu/config.json']=sha256(source/'mmlu/config.json')
            for phase,n in [('canary',24),('formal',12906)]:
                p=source/'mmlu'/phase;(p/'source').mkdir(parents=True)
                save(p/'jobs.json',[{'job_id':str(i)} for i in range(n)])
                save(p/'config.json',cfg)
                # JSON bytes stand in for opaque source bytes; prepare must copy every protocol file.
                save(p/'source/backend.py',{'synthetic_opaque_source':True})
                protocol={'files':{str(f.relative_to(p)):sha256(f) for f in p.rglob('*') if f.is_file()},'model_files':{}}
                save(p/'protocol.json',{**protocol,'protocol_id':digest(protocol)})
                for f in p.rglob('*.json'):files[str(f.relative_to(source))]=sha256(f)
            old={'files':files,'model_files':{},'source_audit_sha256':'fixture'};save(source/'manifest.json',old)
            with patch.object(D,'SOURCE',source),patch.object(D,'source_identity',return_value=old),patch.object(D,'validate_run',return_value=({'model_files':{}},cfg)):
                D.prepare(root)
            self.assertEqual(read(root/'ready.json')['status'],'PASS')
            for phase in ('canary','formal'):
                self.assertEqual(sha256(source/'mmlu'/phase/'source/backend.py'),sha256(root/'mmlu'/phase/'source/backend.py'))
                self.assertEqual(read(source/'mmlu'/phase/'protocol.json'),read(root/'mmlu'/phase/'protocol.json'))
            self.assertEqual(read(root/'manifest.json')['formal_draws'],103248)


class AuditTests(unittest.TestCase):
    def test_complete_dual_audit_with_dynamic_ownership(self):
        class Tokenizer:
            def encode(self,text,**kw):return list(text.encode())
            def decode(self,ids,**kw):return bytes(x for x in ids if x).decode()
        tok=Tokenizer()
        cfg={'seed':2026091507,'repeats':8,'temperatures':[.3,.7,1.2],'model_path':'/synthetic',
             'generation_prompt_version':'v1','canary_coverage_policy':'report_invalid'}
        def context(self,case,ids,**kw):return 'prefix:'+str(ids)
        class Backend(MockBackend):
            def generate(self,case,prefix,temp,repeats,seed):
                prompt=context(self,case,prefix)
                text='Reason.';ids=tok.encode(text)+[0]
                return {'prompt':prompt,'prompt_token_ids':tok.encode(prompt),
                    'generation_contract':{'eos_token_ids':[0],'max_new_tokens':4096,'max_context':8192,'prompt_version':'v1',
                       'prompt_tokens':len(tok.encode(prompt)),'boundary':'</step>','constrained_decoding':False},
                    'rows':[{'repeat':i,'seed':seed,'raw_text':text,'finish_reason':'eos','generated_token_ids':ids,
                             'parse':parse_step(text)} for i in range(8)]}
            def score(self,case,prefix,deleted,target):
                r=super().score(case,prefix,deleted,target)
                r['evidence'].update(full_context_ids=tok.encode(context(self,case,prefix)),
                    deleted_context_ids=tok.encode(context(self,case,[n for n in prefix if n!=deleted])))
                return r
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SLURM_JOB_ID':'mock'}),contextlib.redirect_stdout(io.StringIO()):
            root,r,j,_,_=fixture(d,24);import shutil
            shutil.copytree(r,root/'mmlu/canary')
            backend=Backend(None,{})
            with patch.object(D,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)):
                for phase in ('canary','formal'):
                    for i in range(8):D.process_phase(root,phase,i,backend)
                slot=root/'mmlu';(slot/'worker-logs').mkdir();save(slot/'config.json',cfg)
                save(slot/'gpu-complete.json',{'status':'PASS','job_id':'mock','protocol_id':'new'})
                save(slot/'numeric-gate.json',{'status':'PASS','workers':8})
                save(slot/'canary-gate.json',{'status':'PASS','coverage':D.coverage(slot)})
                probe=backend.score({},[1],1,'Probe')
                reference={'status':'PASS','repeat_max_abs':0,'probe':probe,'repeat_probe':probe,
                    'masked_loss_errors':{'full':0,'deleted':0},
                    'native_losses':{k:probe['score'][k+'_nll'] for k in ('full','deleted')}}
                for i in range(8):
                    save(slot/'worker-logs'/f'{i}-reference.json',reference)
                    save(slot/'worker-logs'/f'{i}-complete.json',{'status':'PASS','worker':i,'job_id':'mock'})
                transformers=types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda *a,**kw:tok))
                with patch.dict('sys.modules',{'transformers':transformers}),patch.object(D,'check',return_value={'protocol_id':'new','model_files':{}}),patch.object(D.HFBackend,'context',context):
                    D.audit(root)
                audit=read(slot/'audit.json');self.assertEqual(audit['status'],'PASS')
                self.assertEqual(audit['results']['formal']['strict_complete'],0)
                self.assertEqual(audit['results']['formal']['eos_complete'],1)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p=Path(__file__).resolve().parents[1]/'scripts/e2_dynamic_submit.py'
        spec=importlib.util.spec_from_file_location('dynamic_submit_tests',p);cls.M=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.M)

    def test_scope_resources_and_dependencies(self):
        m=self.M;self.assertEqual(m.OLD_JOBS,('115308','115309'));self.assertEqual(m.PREDECESSOR,'115307')
        for i,stage in enumerate(m.STAGES):
            cmd=m.command(i,'123');self.assertIn('--hold',cmd);self.assertIn('--dependency=afterok:123',cmd)
            self.assertIn('--kill-on-invalid-dep=yes',cmd);self.assertIn('--reservation=code-agent',cmd)
            self.assertIn('--gpus-per-node=8' if stage=='launch' else '--gres=none',cmd)

    def test_wrong_receipt_rejected_before_query(self):
        with patch.object(self.M,'state') as s:
            with self.assertRaisesRegex(ValueError,'command receipt'):self.M.held({'index':1,'stage':'launch','job_id':'123','command':[]},'122')
            s.assert_not_called()

    def test_failed_predecessor_blocks(self):
        with patch.object(self.M.legacy,'scheduler_rows',return_value={'115306':['FAILED','1:0'],'115307':['PENDING','0:0']}):
            with self.assertRaisesRegex(ValueError,'predecessor'):self.M.predecessor_available()

    def test_other_active_jobs_block(self):
        with patch.object(self.M.subprocess,'check_output',return_value='115308 pals-e2-formal-7\n999 pals-e2-unrelated\n'):
            with self.assertRaisesRegex(ValueError,'other related'):self.M.active()
            self.M.active(['999'])

    def test_old_running_job_cannot_be_replaced(self):
        receipts=[{}]*9;receipts[7]={'job_id':'115308','benchmark':'mmlu'};receipts[8]={'job_id':'115309','benchmark':'mmlu'}
        with patch.object(self.M,'source_identity'),patch.object(self.M,'read',return_value=receipts),patch.object(self.M,'state',return_value={'JobState':'RUNNING'}):
            with self.assertRaisesRegex(ValueError,'unstarted'):self.M.old_pending()


if __name__=='__main__':unittest.main()
