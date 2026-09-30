"""Synthetic recovery evidence only; no candidate code executes locally."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import code_harness
from pals_validation.e3.expanded import prepare as expand, worker
from pals_validation.e3.outcome_recovery import audit, evaluate, prepare, validate
from pals_validation.io import digest, encoded, read, save, sha256
from test_e3_expanded import sources


class RecoveryTests(unittest.TestCase):
    def fixture(self, root):
        src = sources(root)
        run = root / 'expanded'
        expand(src, run, 20)
        for stage in ('generate', 'score', 'evaluate'):
            for shard in range(8):
                worker(run, 'phi4mini', 0, stage, shard)
        # Emulate prior code snapshot; update evidence IDs to its immutable ID.
        m = read(run / 'manifest.json')
        del m['implementation_hashes']['e3/outcome_recovery.py']
        m['implementation_hashes']['e3/code_harness.py'] = 'prior-harness'
        m['implementation_hashes']['scripts/e3_harness_selftest.py'] = 'prior-test'
        m['expansion_id'] = digest({k: v for k, v in m.items() if k != 'expansion_id'})
        (run / 'manifest.json').write_bytes(encoded(m))
        for kind in ('workers', 'scores', 'outcomes'):
            for f in (run / 'phi4mini' / kind).glob('*.json'):
                r = read(f)
                r['expansion_id'] = m['expansion_id']
                f.write_bytes(encoded(r))
        outcomes = sorted((run / 'phi4mini/outcomes').glob('*.json'))
        outcomes[0].unlink()  # synthetic missing result
        bad = read(outcomes[1])
        bad.update(status='infrastructure_error', correct=None)
        outcomes[1].write_bytes(encoded(bad))
        test = root / 'selftest.json'
        save(test, dict(status='PASS', harness_sha256=sha256(code_harness.__file__),
                        isolation_probes_passed=True, cpu_timeout_control=dict(reason='per_test_cpu_limit')))
        return run, test

    def test_recovery_reuses_38_and_replays_all_scores_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run, test = self.fixture(root)
            before = {str(p): sha256(p) for p in run.rglob('*') if p.is_file()}
            out = root / 'recovery'
            m = prepare(run, out, 'phi4mini', 0, test)
            self.assertEqual(sum(i['action'] == 'evaluate' for i in m['items']), 2)
            evaluate(out)
            self.assertEqual(audit(out)['counts']['planned'], 40)
            self.assertEqual(before, {str(p): sha256(p) for p in run.rglob('*') if p.is_file()})
            with self.assertRaisesRegex(ValueError, 'already completed'):
                evaluate(out)
            score = next((run / 'phi4mini/scores').glob('*.json'))
            score.write_bytes(score.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'Integrity check'):
                validate(out)

    def test_failure_kept_and_no_implicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run, test = self.fixture(root)
            out = root / 'recovery'
            prepare(run, out, 'phi4mini', 0, test)
            with patch('pals_validation.e3.outcome_recovery.evaluate_answer',
                       return_value=dict(status='infrastructure_error', correct=None)):
                with self.assertRaisesRegex(ValueError, 'infrastructure failure'):
                    evaluate(out)
            self.assertFalse((out / 'audit.json').exists())
            with self.assertRaisesRegex(ValueError, 'no implicit outcome retry'):
                evaluate(out)

    def test_kernel_change_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run, test = self.fixture(root)
            m = read(run / 'manifest.json')
            m['implementation_hashes']['e3/backend.py'] = 'changed'
            m['expansion_id'] = digest({k: v for k, v in m.items() if k != 'expansion_id'})
            (run / 'manifest.json').write_bytes(encoded(m))
            with self.assertRaisesRegex(ValueError, 'unapproved kernel'):
                prepare(run, root / 'blocked', 'phi4mini', 0, test)
            self.assertFalse((root / 'blocked').exists())
