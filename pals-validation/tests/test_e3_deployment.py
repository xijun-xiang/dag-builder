"""Cluster migration contracts: CPU only, no SSH, Slurm or model execution."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import deployment, greedy
from pals_validation.io import read, save, sha256
from test_e3_greedy import fixture, SUBMIT


class DeploymentTests(unittest.TestCase):
    def test_allowlist_and_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(deployment.profile().name, 'b1')
        with patch.dict(os.environ, {'PALS_CLUSTER': 'a1'}):
            p = deployment.profile()
            self.assertEqual(p.root, '/work/projects/polyullm/xxj/pals')
            self.assertEqual(p.reservation, 'pretrain')
        with patch.dict(os.environ, {'PALS_CLUSTER': '/tmp/arbitrary'}):
            with self.assertRaisesRegex(ValueError, 'unknown cluster'):
                deployment.profile()

    def test_no_sibling_or_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            # Distinct names also work on macOS case-insensitive filesystems.
            lower, upper = base / 'a1-pals', base / 'b1-PALS'
            lower.mkdir()
            upper.mkdir()
            (lower / 'run').mkdir()
            (lower / 'escape').symlink_to(upper, target_is_directory=True)
            with patch.object(deployment, 'profile', return_value=replace(deployment.PROFILES['a1'], root=str(lower))):
                self.assertEqual(deployment.assert_project_path(lower / 'run'), lower / 'run')
                for path in (upper, lower / 'escape', base):
                    with self.assertRaises(ValueError):
                        deployment.assert_project_path(path)

    def test_a1_mock_roundtrip_and_profile_immutability(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'PALS_CLUSTER': 'a1'}):
            run = fixture(Path(tmp))
            manifest = read(run / 'manifest.json')
            self.assertEqual(manifest['deployment'], deployment.PROFILES['a1'].record())
            for shard in range(8):
                greedy.gpu_worker(run, 'qwen25', shard, barrier=False)
                greedy.evaluate(run, 'qwen25', shard)
            self.assertEqual(greedy.audit(run, 'qwen25')['items'], 5)
            with patch.dict(os.environ, {'PALS_CLUSTER': 'b1'}):
                with self.assertRaisesRegex(ValueError, 'deployment profile changed'):
                    greedy.load(run, 'qwen25')

    def test_a1_serial_jobs_reservation_and_once_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fence = Path(tmp).resolve()
            root, repo, prepared = [fence / n for n in ('run', 'repo', 'prepared')]
            for path in (root, repo, prepared, root / 'preflight'):
                path.mkdir()
            save(root / 'preflight/selftest.json', {'status': 'PASS'})
            (root / 'experiment').mkdir()
            save(root / 'experiment/manifest.json', {'protocol_id': 'test'})
            save(root / 'preflight/context-checks.json', {})
            save(root / 'preflight/init.json', {'status': 'PASS', 'job_id': '10', 'run': str(root / 'experiment'),
                'protocol_id': 'test', 'selftest_sha256': sha256(root / 'preflight/selftest.json'),
                'context_checks_sha256': sha256(root / 'preflight/context-checks.json')})
            calls = []
            def command(args, **kwargs):
                if args[0] == 'sacct':
                    return '10|COMPLETED|0:0\n10.batch|COMPLETED|0:0\n10.0|COMPLETED|0:0'
                if args[0] in ('git', 'squeue'):
                    return ''
                self.assertEqual(args[0], 'sbatch')
                self.assertIn('--reservation=pretrain', args)
                self.assertTrue(args[-1].endswith('/a1-e3-greedy.sbatch'))
                self.assertEqual(kwargs['env']['PALS_CLUSTER'], 'a1')
                calls.append(args)
                return str(100 + len(calls))
            with patch.object(SUBMIT, 'profile', return_value=replace(deployment.PROFILES['a1'], root=str(fence))), \
                 patch.object(SUBMIT, 'load', return_value=({'protocol_id': 'test'}, None, None, None)), \
                 patch.object(SUBMIT.subprocess, 'check_output', side_effect=command):
                jobs = SUBMIT.submit(root, repo, prepared, '10')
                self.assertEqual(len(jobs), 6)
                for index, args in enumerate(calls[1:]):
                    self.assertIn('--dependency=afterok:' + jobs[index]['job_id'], args)
                with self.assertRaisesRegex(ValueError, 'already attempted'):
                    SUBMIT.submit(root, repo, prepared, '10')
                self.assertEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()
