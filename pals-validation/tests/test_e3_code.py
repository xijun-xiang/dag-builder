"""Pure code-harness contract tests; never execute generated code locally."""

import base64
import json
import pickle
import signal
import unittest
import zlib
from pathlib import Path

from pals_validation.e3.code_harness import POLICY, UnsupportedCode, _classify_child, equal_lcb, static_check
from pals_validation.e3.code_tests import decode_lcb_tests


class CodeEvaluationContractTests(unittest.TestCase):
    def test_exit_classification_requires_isolation_handshake(self):
        ready = (json.dumps(dict(policy=POLICY, isolation_probes_passed=True, phase='ready')) + '\n').encode()
        self.assertEqual(_classify_child(ready, -signal.SIGXCPU)['reason'], 'per_test_cpu_limit')
        for code in (-signal.SIGXCPU, -signal.SIGKILL, 1, 0):
            self.assertEqual(_classify_child(b'', code)['status'], 'infrastructure_error')
        for code in (-signal.SIGKILL, -signal.SIGTERM, 1):
            self.assertEqual(_classify_child(ready, code)['status'], 'infrastructure_error')
        self.assertEqual(_classify_child(ready, -9, True)['reason'], 'per_test_wall_limit')
        self.assertEqual(_classify_child(b'', -9, True)['status'], 'infrastructure_error')
        self.assertEqual(_classify_child(ready, 0)['reason'], 'invalid_final_record')
        self.assertEqual(_classify_child(ready + b'[]\n', 0)['status'], 'infrastructure_error')
        result = dict(policy=POLICY, isolation_probes_passed=True, status='executed', value=7)
        checked = _classify_child(ready + json.dumps(result).encode(), 0)
        self.assertEqual(checked['value'], 7)
        self.assertEqual(checked['returncode'], 0)
        self.assertTrue(checked['isolation_ready'])
        self.assertEqual(len(checked['child_output_sha256']), 64)

    def test_cpu_job_can_read_frozen_policy(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" /
                  "b1-e3-cpu.sbatch").read_text(encoding="utf-8")
        policy_mount = ("/work/projects/polyullm/xxj/PALS/configs/e3:"
                        "/work/projects/polyullm/xxj/PALS/configs/e3:ro")
        self.assertIn(policy_mount, script)

    def test_official_style_lcb_comparison(self):
        self.assertTrue(equal_lcb("1.00 2\n", "1 2.0\n", "stdin"))
        self.assertFalse(equal_lcb("1.01\n", "1\n", "stdin"))
        self.assertTrue(equal_lcb([1, 2], [1, 2], "functional"))
        self.assertFalse(equal_lcb(1, True, "functional"))

    def test_safe_private_test_decoder(self):
        case = [{"input": "7", "output": "7", "testtype": "functional"}]
        text = json.dumps(case)
        compressed = base64.b64encode(zlib.compress(pickle.dumps(text))).decode()
        gold = {"io_type": "functional", "public_test_cases": text,
                "private_test_cases": compressed}
        rows = decode_lcb_tests(gold)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["inputs"], [7])
        self.assertEqual(rows[1]["split"], "private")
        gold["private_test_cases"] = base64.b64encode(
            zlib.compress(pickle.dumps(len))).decode()
        with self.assertRaises(ValueError):
            decode_lcb_tests(gold)

    def test_unsafe_or_unreviewed_code_is_not_executed(self):
        static_check("from math import sqrt\ndef f(x): return sqrt(x)")
        with self.assertRaises(UnsupportedCode):
            static_check("import os\ndef f(): return os.listdir('/')")
        with self.assertRaises(UnsupportedCode):
            static_check("def f(x): return x.__class__")
        # Invalid Python is a benchmark-level program error, not a reason to
        # execute or to silently discard the question.
        static_check("def f(:")


if __name__ == "__main__":
    unittest.main()
