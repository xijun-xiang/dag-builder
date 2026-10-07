"""No real scheduler calls: exclusion, hold, audit and release contracts."""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3.deployment import PROFILES
from pals_validation.io import save, sha256
from test_e3_greedy import SUBMIT

EXCLUDED = 'kb3-a1-nv-dgx42'


class HeldTests(unittest.TestCase):
    @contextmanager
    def setup_scheduler(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root, repo, prepared = [base / n for n in ('run', 'repo', 'prepared')]
            for p in (root, repo, prepared, root / 'preflight'):
                p.mkdir()
            save(root / 'preflight/selftest.json', {'status': 'PASS'})
            save(root / 'preflight/context-checks.json', {})
            save(root / 'preflight/init.json', {'status': 'PASS', 'job_id': '10',
                'run': str(root / 'experiment'), 'protocol_id': 'test',
                'selftest_sha256': sha256(root / 'preflight/selftest.json'),
                'context_checks_sha256': sha256(root / 'preflight/context-checks.json')})
            states, submissions, releases = {}, [], []

            def call(command, **kw):
                if command[0] == 'sacct':
                    return '10|COMPLETED|0:0\n10.batch|COMPLETED|0:0\n10.0|COMPLETED|0:0'
                if command[0] == 'git':
                    return ''
                if command[0] == 'squeue':
                    return '\n'.join(f'{job} {f["JobName"]}' for job, f in states.items())
                if command[0] == 'scontrol':
                    self.assertEqual(command[1:3], ['show', 'job'])
                    return ' '.join(f'{k}={v}' for k, v in states[command[3]].items())
                self.assertEqual(command[0], 'sbatch')
                self.assertIn('--hold', command)
                self.assertIn('--exclude=' + EXCLUDED, command)
                self.assertFalse(any(k.startswith('SBATCH_') for k in kw['env']))
                submissions.append(command)
                job = str(100 + len(submissions))
                name = next(v.split('=', 1)[1] for v in command if v.startswith('--job-name='))
                gpu = name.endswith('-gpu')
                states[job] = {'JobId': job, 'JobName': name, 'JobState': 'PENDING', 'Priority': '0',
                    'ExcNodeList': EXCLUDED, 'Partition': 'defq', 'Reservation': 'pretrain',
                    'WorkDir': str(root), 'Command': str(repo / 'scripts/a1-e3-greedy.sbatch'),
                    'NumNodes': '1-1', 'NumTasks': '1', 'Requeue': '0', 'Restarts': '0',
                    'StdOut': str(root / 'logs' / f'{job}-{name}.out'),
                    'StdErr': str(root / 'logs' / f'{job}-{name}.err'), 'UserId': 'xijun(101112)',
                    'NumCPUs': '32' if gpu else '16', 'CPUs/Task': '32' if gpu else '16',
                    'TimeLimit': '1-00:00:00' if gpu else '04:00:00',
                    'ReqTRES': 'cpu=32,mem=256G,node=1,billing=32,gres/gpu=8' if gpu else 'cpu=16,mem=64G,node=1,billing=16',
                    'Dependency': '(null)' if len(submissions) == 1 else f'afterok:{int(job)-1}(unfulfilled)',
                    'KillOInInvalidDependent': 'Yes'}
                return job

            def release(command, **kw):
                self.assertEqual(command[:2], ['scontrol', 'release'])
                releases.append(command[2])
                return subprocess.CompletedProcess(command, 0)

            with patch.object(SUBMIT, 'profile', return_value=replace(PROFILES['a1'], root=str(base))), \
                 patch.object(SUBMIT, 'load', return_value=({'protocol_id': 'test'}, None, None, None)), \
                 patch.object(SUBMIT.subprocess, 'check_output', side_effect=call), \
                 patch.object(SUBMIT.subprocess, 'run', side_effect=release):
                yield root, repo, prepared, states, submissions, releases

    def test_explicit_hold_exclusion_and_reverse_release(self):
        with self.setup_scheduler() as (root, repo, prepared, states, commands, releases), \
             patch.dict(os.environ, {'SBATCH_EXCLUDE': 'wrong', 'SBATCH_GRES': 'gpu:99'}):
            jobs = SUBMIT.submit(root, repo, prepared, '10', [EXCLUDED])
            self.assertEqual(len(jobs), 6)
            self.assertEqual(SUBMIT.verify_chain(root, repo, prepared, '10')['status'], 'PASS')
            self.assertEqual(releases, [])
            self.assertEqual(SUBMIT.release(root, repo, prepared, '10')['status'], 'RELEASED')
            self.assertEqual(releases, ['106', '105', '104', '103', '102', '101'])
            with self.assertRaisesRegex(ValueError, 'release already attempted'):
                SUBMIT.release(root, repo, prepared, '10')
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                SUBMIT.submit(root, repo, prepared, '10', [EXCLUDED])
            self.assertEqual(len(commands), 6)

    def test_bad_effective_scheduler_fields_never_release(self):
        bad = {'ExcNodeList': '(null)', 'Priority': '100', 'JobState': 'RUNNING',
            'Reservation': 'wrong', 'Partition': 'other', 'UserId': 'other(1)',
            'NumCPUs': '64', 'TimeLimit': '2-00:00:00', 'Dependency': '(null)',
            'KillOInInvalidDependent': 'No', 'Command': '/wrong/script', 'WorkDir': '/wrong',
            'ReqTRES': 'cpu=16,mem=64G,node=1,gres/gpu=8', 'Restarts': '1'}
        for field, value in bad.items():
            with self.subTest(field=field), self.setup_scheduler() as (root, repo, prepared, states, _, releases):
                SUBMIT.submit(root, repo, prepared, '10', [EXCLUDED])
                states['102'][field] = value
                with self.assertRaises(ValueError):
                    SUBMIT.release(root, repo, prepared, '10')
                self.assertEqual(releases, [])

    def test_partial_release_failure_keeps_first_gpu_held(self):
        with self.setup_scheduler() as (root, repo, prepared, _, _, releases):
            SUBMIT.submit(root, repo, prepared, '10', [EXCLUDED])
            def fail(command, **kw):
                releases.append(command[2])
                raise subprocess.CalledProcessError(1, command)
            with patch.object(SUBMIT.subprocess, 'run', side_effect=fail):
                with self.assertRaises(subprocess.CalledProcessError):
                    SUBMIT.release(root, repo, prepared, '10')
            self.assertEqual(releases, ['106'])
            self.assertNotIn('101', releases)
            with self.assertRaisesRegex(ValueError, 'release already attempted'):
                SUBMIT.release(root, repo, prepared, '10')

    def test_bad_node_names_rejected(self):
        for name in ('', 'node,other', '--help', 'node;cmd', '/tmp', 'node 42'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                SUBMIT.exclusions([name])

    def test_exact_node_count_scalar_or_range_only(self):
        for value in ('1', '1-1', '2', '1-2', '0-1', '(null)'):
            with self.subTest(value=value), self.setup_scheduler() as (root, repo, prepared, states, _, releases):
                SUBMIT.submit(root, repo, prepared, '10', [EXCLUDED])
                for f in states.values():
                    f['NumNodes'] = value
                if value in ('1', '1-1'):
                    self.assertEqual(SUBMIT.verify_chain(root, repo, prepared, '10')['status'], 'PASS')
                else:
                    with self.assertRaisesRegex(ValueError, 'node count'):
                        SUBMIT.release(root, repo, prepared, '10')
                self.assertEqual(releases, [])


if __name__ == '__main__':
    unittest.main()
