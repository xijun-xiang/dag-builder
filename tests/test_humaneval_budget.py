"""Campaign-level accounting must include uncertain and oversized old calls."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

from dag_builder.storage import write_once

SPEC = importlib.util.spec_from_file_location("humaneval_campaign_prepare", Path(__file__).resolve().parents[1] / "scripts/prepare_humaneval_campaign.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BudgetAuditTests(unittest.TestCase):
    def test_failed_cap_requires_explicit_policy_and_is_never_marked_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_once(root / "probe_result.json", {"passed": False, "actual_request_max_tokens": 32768,
                "results": [{"probe": "short", "status": "passed"},
                            {"probe": "cap", "status": "failed", "reason": "response_contract_violation"}]})
            with self.assertRaises(ValueError):
                MODULE.probe_policy(root)
            policy = MODULE.probe_policy(root, True)
            self.assertFalse(policy["cap_probe_passed"])
            self.assertEqual(policy["status"], "failed_cap_preserved_user_authorized_content_gate")

    def test_overshoot_and_unknown_calls_are_carried_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "items/fixture/solve/attempt-00"
            second = root / "items/fixture/solve/attempt-01"
            write_once(first / "request.json", {"reserved_tokens": 100})
            write_once(first / "response.json", {"body": {"usage": {"total_tokens": 125}}})
            write_once(second / "request.json", {"reserved_tokens": 100})
            audit = MODULE.audit_prior_runs([root])
            self.assertEqual(audit["calls"], 2)
            self.assertEqual(audit["accounted_tokens"], 225)
            self.assertFalse(audit["attempts"][1]["response_received"])
            with self.assertRaises(ValueError):
                MODULE.audit_prior_runs([root, root])


if __name__ == "__main__":
    unittest.main()
