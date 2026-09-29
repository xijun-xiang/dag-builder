"""Opt-in MMLU provider overruns are N/A, never accepted or retried."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.pipeline import Pipeline
from dag_builder.provider_overrun import POLICY, ProviderOverrunOmission
from dag_builder.storage import read_json, write_once


class ProviderOverrunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.subject = Path(self.temp.name).resolve() / "campaign/subjects/professional_law"
        self.attempt = self.subject / "items" / ("a" * 20) / "solve/attempt-00"
        self.request = {"reserved_tokens": 36256, "payload": {
            "model": "deepseek-v4-flash", "max_tokens": 32768}}
        self.body = {"model": "deepseek-v4-flash", "usage": {
            "prompt_tokens": 732, "completion_tokens": 40628,
            "total_tokens": 41360}, "choices": [{"finish_reason": "stop", "message": {
                "content": "not accepted", "reasoning_content": "reasoning"}}]}
        write_once(self.attempt / "request.json", self.request)
        write_once(self.attempt / "response.json", {"body": self.body})
        self.config = Config(prompt_version="mmlu-general-thinking-v4",
                             model="deepseek-v4-flash", thinking="enabled",
                             strict_response_contract=True,
                             content_gated_response=True)

    def test_opt_in_exact_provider_overrun_is_recorded_and_omitted(self):
        write_once(self.subject.parent.parent / "provider_overrun_policy.json", POLICY)
        runner = Pipeline(self.subject, self.config, object())
        runner._restore_budget()
        self.assertFalse(runner._stop.is_set())
        self.assertEqual(runner.accounted_tokens, 41360)
        marker = read_json(self.attempt / "provider_overrun_omission.json")
        self.assertEqual(marker["overage_tokens"], 5104)
        with self.assertRaises(ProviderOverrunOmission):
            runner._check_response(self.attempt, self.body)
        with patch.object(runner, "stage", side_effect=ProviderOverrunOmission("solve")):
            result = runner.process({"item_id": "a" * 20})
        self.assertEqual(result["status"], "infrastructure_omitted")
        self.assertFalse((self.attempt.parent / "output.json").exists())

    def test_policy_absent_or_other_contract_violation_stops(self):
        runner = Pipeline(self.subject, self.config, object())
        runner._restore_budget()
        self.assertTrue(runner._stop.is_set())
        self.assertFalse((self.attempt / "provider_overrun_omission.json").exists())

        write_once(self.subject.parent.parent / "provider_overrun_policy.json", POLICY)
        second = self.subject / "items" / ("b" * 20) / "solve/attempt-00"
        write_once(second / "request.json", self.request)
        write_once(second / "response.json", {"body": {**self.body, "model": "wrong"}})
        runner = Pipeline(self.subject, self.config, object())
        runner._restore_budget()
        self.assertTrue(runner._stop.is_set())
        self.assertIn("items/" + "b" * 20, next(iter(runner._contract_violations)))
        self.assertFalse((second / "provider_overrun_omission.json").exists())

    def test_changed_policy_rejected(self):
        write_once(self.subject.parent.parent / "provider_overrun_policy.json",
                   {**POLICY, "model": "wrong"})
        with self.assertRaisesRegex(ValueError, "policy changed"):
            Pipeline(self.subject, self.config, object())


if __name__ == "__main__":
    unittest.main()
