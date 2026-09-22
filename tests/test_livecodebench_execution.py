import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_livecodebench_reference.py"
spec = importlib.util.spec_from_file_location("lcb_execution_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ExecutionTests(unittest.TestCase):
    def test_static_guard_without_execution(self):
        gate.static_check("import sys\nprint(sum(map(int, sys.stdin.read().split())))")
        for code in ("import os", "import subprocess", "open('/etc/passwd')", "eval('1')",
                     "sys.__stdout__.write('spoof')", "from ctypes import CDLL"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                gate.static_check(code)

    def test_comparisons_conservative(self):
        self.assertTrue(gate.equal_output([1, 2], [1, 2], "functional"))
        self.assertFalse(gate.equal_output(1, True, "functional"))
        self.assertTrue(gate.equal_output(" 1   2\n3 \n", "1 2\n3", "stdin"))
        self.assertFalse(gate.equal_output("1.00001", "1", "stdin"))

    def test_child_payload_excludes_expected_and_environment(self):
        test = {"inputs": [1], "expected": 2, "split": "private", "index": 0}
        fake = subprocess.CompletedProcess([], 0, json.dumps({"policy": gate.POLICY,
                "isolation_probes_passed": True, "status": "executed", "value": 2}), "")
        with patch.object(gate.subprocess, "run", return_value=fake) as call:
            result = gate.evaluate_test("class Solution: pass", "functional", "f", test, Path("/tmp"))
        self.assertEqual(result["status"], "passed")
        kwargs = call.call_args.kwargs
        self.assertNotIn("expected", json.loads(kwargs["input"]))
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["timeout"], 10)

    def test_timeout_is_not_wrong_answer(self):
        with patch.object(gate.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 10)):
            result = gate.evaluate_test("", "stdin", None,
                {"inputs": "", "expected": "", "split": "private", "index": 0}, Path("/tmp"))
        self.assertEqual(result["status"], "timeout")

    def test_guard_refuses_local_run(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(gate.os.environ, {}, clear=True), self.assertRaises((RuntimeError, FileNotFoundError)):
            gate.run(Path(folder))


if __name__ == "__main__":
    unittest.main()
