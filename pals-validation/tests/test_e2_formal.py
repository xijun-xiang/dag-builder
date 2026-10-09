"""No GPU/network: parser, dual evidence, exact-once phase and held resources."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.backend import MockBackend
from pals_validation.io import save,read,digest
from pals_validation.protocol import parse_step
from pals_validation.recovery.e2_eos import select_target as v1
from pals_validation.recovery.e2_eos_v2 import select_target as v2
from pals_validation.recovery import e2_formal as F

CONTRACT={'eos_token_ids':[99],'max_new_tokens':4096}
def raw(text,finish='eos',repeat=0):
    return {'raw_text':text,'finish_reason':finish,'parse':parse_step(text,'humaneval'),
            'generated_token_ids':[1,2,99],'repeat':repeat}

class ParserTests(unittest.TestCase):
    def test_known_malformed_prefix_regression(self):
        for text in ('Text.\n</stepINavigationController bad','Text.</stepICollectionView bad'):
            r=raw(text);before=copy.deepcopy(r)
            self.assertEqual(v1(r,CONTRACT,'humaneval')['kind'],'eos_recovered')
            self.assertEqual(v2(r,CONTRACT,'humaneval')['kind'],'invalid')
            self.assertEqual(r,before)

    def test_all_labels_case_and_bracket_variants(self):
        for tag in ('step','think','answer','analysis','final','result'):
            for prefix in ('<','</','[ / ','< / '):
                self.assertEqual(v2(raw('Text '+prefix+tag.upper()+'Junk'),CONTRACT,'humaneval')['kind'],'invalid')

    def test_strict_and_all_unrelated_cases_identical(self):
        rows=[raw('Original.</step>','boundary'),raw('A sentence.'),raw('Finished reasoning.'),
              raw('False but fully formed.'),raw('A wrong answer.'),raw('nonsense foo violet'),
              raw('A\n\nB'),raw('1. One.\n2. Two.'),raw('return True'),raw('If x < y,\ncompare.'),
              raw('Budget text','length'),raw('Why','unknown'),raw('A<answer>'),raw('')]
        for r in rows:self.assertEqual(v1(r,CONTRACT,'humaneval'),v2(r,CONTRACT,'humaneval'))

    def test_no_target_rewrite_or_substring_selection(self):
        r=raw('  First sentence. Second sentence.\n')
        self.assertEqual(v2(r,CONTRACT,'humaneval')['target'],r['raw_text'].strip())

    def test_no_cap_recovery(self):
        r=raw('A sentence.');r['generated_token_ids']=[1]*4095+[99]
        self.assertEqual(v2(r,CONTRACT,'humaneval')['kind'],'invalid')

class DualTests(unittest.TestCase):
    def setUp(self):
        self.job={'prefix_ids':[1,2],'deleted_id':2}
        self.case={'task_type':'humaneval'}
        self.backend=MockBackend(None,{})

    def test_dual_output_keeps_raw_and_original_score(self):
        rows=[raw('Strict.</step>','boundary',0),raw('Natural.',repeat=1),raw('Bad </stepFoo',repeat=2),raw('Length','length',3)]
        gen={'rows':rows,'generation_contract':CONTRACT};before=copy.deepcopy(gen)
        a,b=F.dual_rows(gen,self.case,self.job,self.backend)
        self.assertEqual(gen,before)
        self.assertEqual([r['status']=='ok' for r in a],[True,False,False,False])
        self.assertEqual([r['status']=='ok' for r in b],[True,True,False,False])
        self.assertEqual(a[0]['score'],b[0]['score']);self.assertEqual(a[0]['evidence'],b[0]['evidence'])
        for original,strict,extended in zip(rows,a,b):
            self.assertTrue(all(strict[k]==extended[k]==v for k,v in original.items()))
        self.assertEqual(b[1]['evidence']['target_text'],'Natural.')

    def test_only_scores_accepted_outputs_once(self):
        gen={'rows':[raw('Strict.</step>','boundary'),raw('Natural.'),raw('Truncated','length')],'generation_contract':CONTRACT}
        with patch.object(self.backend,'score',wraps=self.backend.score) as scorer:
            F.dual_rows(gen,self.case,self.job,self.backend)
        self.assertEqual(scorer.call_count,2)

    def test_score_corruption_stops(self):
        def bad(*args):
            r=MockBackend(None,{}).score(*args);r['score']['g']=42;return r
        with patch.object(self.backend,'score',side_effect=bad):
            with self.assertRaises(ValueError):F.dual_rows({'rows':[raw('Natural.')],'generation_contract':CONTRACT},self.case,self.job,self.backend)

class PhaseTests(unittest.TestCase):
    def fixture(self,folder):
        root=Path(folder);run=root/'humaneval/formal'
        for name in ('inputs','workers','generations','generation-attempts','results','eos-results'):(run/name).mkdir(parents=True)
        jobs=[{'job_id':str(i),'item_id':'q','prefix_ids':[1],'deleted_id':1,'temperature':.3,'kind':'e2'} for i in range(16)]
        save(run/'jobs.json',jobs);save(run/'inputs/cases.json',[{'item_id':'q','task_type':'humaneval'}])
        config={'seed':2026091507,'repeats':8}
        backend=MockBackend(None,{})
        def generate(case,prefix,temp,repeats,seed):
            rows=[{**raw('Reason.',repeat=i),'seed':seed} for i in range(repeats)]
            return {'rows':rows,'generation_contract':CONTRACT,'prompt':'fixture','prompt_token_ids':[1]}
        backend.generate=generate
        return root,run,jobs,config,backend

    def test_phase_assignment_seed_once_and_complete_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            root,run,jobs,cfg,b=self.fixture(d)
            with patch.object(F,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)),patch.dict(F.os.environ,{'SLURM_JOB_ID':'mock'}):
                for i in range(8):F.process_phase(root,'humaneval','formal',i,b)
            self.assertEqual(len(list((run/'generations').glob('*.json'))),16)
            for i,j in enumerate(jobs):
                a=read(run/'generation-attempts'/(j['job_id']+'.json'))
                self.assertEqual(a['worker'],i%8)
                self.assertEqual(a['seed'],int(digest([cfg['seed'],j['job_id']])[:8],16))
                self.assertFalse(read(run/'results'/(j['job_id']+'.json'))['scientific_evidence'])
                self.assertEqual(read(run/'eos-results'/(j['job_id']+'.json'))['summary']['valid'],8)

    def test_uncertain_attempt_blocks_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root,run,jobs,cfg,b=self.fixture(d)
            save(run/'generation-attempts/0.json',{'uncertain':True})
            with patch.object(F,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)),patch.dict(F.os.environ,{'SLURM_JOB_ID':'mock'}),patch.object(b,'generate') as gen:
                with self.assertRaises(FileExistsError):F.process_phase(root,'humaneval','formal',0,b)
                gen.assert_not_called()

    def test_generation_failure_leaves_attempt_no_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root,run,jobs,cfg,b=self.fixture(d)
            with patch.object(F,'validate_run',return_value=({'protocol_id':'fixture','scientific_evidence':False},cfg)),patch.dict(F.os.environ,{'SLURM_JOB_ID':'mock'}),patch.object(b,'generate',side_effect=RuntimeError('failure')):
                with self.assertRaises(RuntimeError):F.process_phase(root,'humaneval','formal',0,b)
            self.assertTrue((run/'generation-attempts/0.json').exists())
            self.assertFalse((run/'generations/0.json').exists())

class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p=Path(__file__).resolve().parents[1]/'scripts/e2_formal_submit.py'
        spec=importlib.util.spec_from_file_location('e2_formal_submit_test',p)
        cls.M=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.M)

    def test_exact_remaining_scope_and_serial_resources(self):
        m=self.M;self.assertEqual(len(m.STAGES),9)
        self.assertEqual([b for a,b in m.STAGES if a=='launch'],['humaneval','gsm8k','livecodebench','mmlu'])
        for i,(a,b) in enumerate(m.STAGES):
            cmd=m.command(i,'123' if i else None)
            self.assertIn('--hold',cmd);self.assertIn('--no-requeue',cmd);self.assertIn('--reservation=code-agent',cmd)
            self.assertIn('--gpus-per-node=8' if a=='launch' else '--gres=none',cmd)
            if i:self.assertIn('--dependency=afterok:123',cmd);self.assertIn('--kill-on-invalid-dep=yes',cmd)

    def test_receipt_drift_fails_before_scheduler(self):
        with patch.object(self.M.subprocess,'check_output') as call:
            with self.assertRaisesRegex(ValueError,'receipt'):self.M.held({'index':1,'job_id':'123','command':[]},'122')
            call.assert_not_called()

    def test_active_related_chain_blocks(self):
        with patch.object(self.M.subprocess,'check_output',return_value='123 pals-e2-formal-1\n'):
            with self.assertRaises(ValueError):self.M.active()
            self.M.active({'123'})

    def test_admission_rejects_wrong_allocation(self):
        with patch.object(self.M,'check'),patch.object(self.M,'read',return_value=[{'job_id':'123','index':0}]),patch.dict(self.M.os.environ,{'SLURM_JOB_ID':'999'}):
            with self.assertRaisesRegex(ValueError,'allocation'):self.M.admit(0)

if __name__=='__main__':unittest.main()
