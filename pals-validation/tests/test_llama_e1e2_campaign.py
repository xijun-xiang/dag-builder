"""CPU-only controller tests. No real scheduler, weights or remote requests."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.io import read, save

PATH = Path(__file__).parents[1] / 'scripts/e1e2_llama_campaign.py'
spec = importlib.util.spec_from_file_location('llama_campaign', PATH)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class LlamaCampaignTests(unittest.TestCase):
    def test_unchanged_science_with_only_declared_model_adaptation(self):
        old = read(Path(__file__).parents[1] / 'configs/mock.json')
        old.update(backend='hf', temperatures=[.3,.7,1.2], repeats=8, seed=2026091507,
                   reviewed_local_code={'files':{}}, runtime_versions={'transformers':'4.47.1'},
                   dtype='bfloat16', attention='sdpa')
        s = {'base_config':old, 'benchmarks':{b:{'max_new_tokens':2048 if b=='gpqa' else 4096,
             'generation_prompt_version':'humaneval-single-step-v2' if b=='humaneval' else 'v1'} for b in M.BENCHES}}
        adaptations = {'model_path','model_revision','max_context','reviewed_local_code',
                       'chat_template_kwargs','runtime_versions','campaign_experiment','canary_coverage_policy',
                       'canary_min_valid_repeats','max_new_tokens','generation_prompt_version'}
        for b,e in M.SLOTS:
            c = M.config(s,b,e,{'transformers':'5.6.0','torch':'2.11.0+cu130'})
            self.assertEqual(c['max_context'],8192)
            self.assertIsNone(c['reviewed_local_code'])
            self.assertEqual(c['canary_min_valid_repeats'],2)
            for k in old.keys()-adaptations:
                self.assertEqual(c[k],old[k],k)
        s['base_config']['temperatures']=[.2,.7,1.2]
        with self.assertRaisesRegex(ValueError,'sampling'):
            M.config(s,'gpqa','e1',{'transformers':'5.6.0','torch':'2'})

    def test_capacity_cannot_silently_change_output_budget(self):
        with self.assertRaises(ValueError):
            M.config({},'unknown','e1',{})
        with self.assertRaisesRegex(ValueError,'runtime'):
            M.config({},'gpqa','e1',{'transformers':'4.47.1','torch':'2'})

    @contextmanager
    def scheduler(self):
        with tempfile.TemporaryDirectory() as d:
            project=Path(d).resolve(); root=project/'run'; root.mkdir(); (root/'submissions').mkdir()
            states, submitted, released = {}, [], []
            def output(cmd, **kw):
                if cmd[0]=='squeue':
                    return '\n'.join(j+' '+f['JobName'] for j,f in states.items())
                if cmd[0]=='scontrol':
                    return ' '.join(k+'='+v for k,v in states[cmd[3]].items())
                self.assertEqual(cmd[0],'sbatch')
                self.assertIn('--hold',cmd); self.assertNotIn('--array',str(cmd))
                self.assertFalse(any(k.startswith('SBATCH_') for k in kw['env']))
                self.assertEqual(kw['env']['PALS_CAMPAIGN_ROOT'],str(root))
                submitted.append(cmd); job=str(100+len(submitted))
                options=dict(x[2:].split('=',1) for x in cmd if x.startswith('--') and '=' in x)
                f={'JobId':job,'JobName':options['job-name'],'JobState':'PENDING','Priority':'0',
                   'Partition':'defq','Reservation':'code-agent','ExcNodeList':M.EXCLUDE,
                   'NumTasks':'1','NumNodes':'1-1','NumCPUs':'32','CPUs/Task':'32','TimeLimit':'08:00:00',
                   'Requeue':'0','Restarts':'0','Command':str(M.LAUNCHER),'WorkDir':str(root),
                   'StdOut':options['output'].replace('%j',job),'StdErr':options['error'].replace('%j',job),
                   'UserId':'xijun(101112)','ReqTRES':'cpu=32,mem=256G,node=1,gres/gpu=8',
                   'Dependency':options.get('dependency','(null)'), 'KillOInInvalidDependent':'Yes'}
                states[job]=f
                return job
            def release(cmd, **kw):
                self.assertEqual(cmd[:2],['scontrol','release']); released.append(cmd[2])
                return subprocess.CompletedProcess(cmd,0)
            with patch.object(M,'ROOT',root),patch.object(M,'PROJECT',project), \
                 patch.object(M,'check',return_value={}),patch.object(M,'e3_gate',return_value={'audit_sha256':'frozen'}), \
                 patch.object(M.subprocess,'check_output',side_effect=output), \
                 patch.object(M.subprocess,'run',side_effect=release):
                yield root,states,submitted,released

    def test_ten_held_serial_jobs_reverse_release_and_no_repeat(self):
        with self.scheduler() as (root,states,submitted,released),patch.dict(os.environ,{'SBATCH_GRES':'gpu:99'}):
            jobs=M.submit(); self.assertEqual(len(jobs),10); self.assertEqual(released,[])
            self.assertEqual(states['101']['Dependency'],'(null)')
            for j in range(102,111): self.assertEqual(states[str(j)]['Dependency'],f'afterok:{j-1}')
            M.release(); self.assertEqual(released,[str(i) for i in range(110,100,-1)])
            with self.assertRaisesRegex(ValueError,'existing/uncertain'): M.submit()
            with self.assertRaisesRegex(ValueError,'already attempted'): M.release()
            self.assertEqual(len(submitted),10)

    def test_bad_effective_scheduler_fields_block_entire_release(self):
        for k,v in {'Reservation':'evaluation','Priority':'10','JobState':'RUNNING','ExcNodeList':'(null)',
                    'UserId':'other(1)','ReqTRES':'cpu=32,mem=256G,node=1,gres/gpu=16',
                    'Dependency':'(null)','KillOInInvalidDependent':'No','WorkDir':'/tmp',
                    'TimeLimit':'24:00:00','Restarts':'1'}.items():
            with self.subTest(k=k), self.scheduler() as (_,states,_,released):
                M.submit(); states['102'][k]=v
                with self.assertRaises(ValueError): M.release()
                self.assertEqual(released,[])

    def test_partial_release_never_starts_first_gpu_or_retries(self):
        with self.scheduler() as (_,_,_,released):
            M.submit()
            def fail(cmd,**kw):
                released.append(cmd[2]); raise subprocess.CalledProcessError(1,cmd)
            with patch.object(M.subprocess,'run',side_effect=fail):
                with self.assertRaises(subprocess.CalledProcessError): M.release()
            self.assertEqual(released,['110'])
            with self.assertRaisesRegex(ValueError,'already attempted'): M.release()

    def test_uncertain_submission_is_not_retried(self):
        with self.scheduler() as (root,_,_,_):
            original=M.subprocess.check_output.side_effect
            def fail(cmd,**kw):
                if cmd[0]=='sbatch': return 'connection lost'
                return original(cmd,**kw)
            with patch.object(M.subprocess,'check_output',side_effect=fail):
                with self.assertRaisesRegex(ValueError,'uncertain sbatch'): M.submit()
            self.assertTrue((root/'submissions/0-attempt.json').exists())
            with self.assertRaisesRegex(ValueError,'existing/uncertain'): M.submit()

    def test_e3_or_active_b1_chain_gate_precedes_sbatch(self):
        with self.scheduler() as (_,states,submitted,_):
            with patch.object(M,'e3_gate',side_effect=ValueError('E3 not complete')):
                with self.assertRaises(ValueError): M.submit()
            states['900']={'JobName':'e3g-internlm3-gpu'}
            with self.assertRaisesRegex(ValueError,'another B1'): M.submit()
            self.assertEqual(submitted,[])

    def test_safe_paths_reject_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d).resolve(); (p/'inside').mkdir(); (p/'link').symlink_to(p/'inside')
            with patch.object(M,'PROJECT',p):
                self.assertEqual(M.safe(p/'inside'),p/'inside')
                for bad in (p,p/'link',p.parent/'outside'):
                    with self.assertRaises(ValueError): M.safe(bad,existing=False)


if __name__=='__main__':
    unittest.main()
