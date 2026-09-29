"""The single authorized provider overrun cannot mask another violation."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.operator_omission import OMISSION, load_operator_omission
from dag_builder.pipeline import Pipeline
from dag_builder.storage import write_once


class OperatorOmissionTests(unittest.TestCase):
    def _fixture(self, root):
        attempt = (root / "items" / OMISSION["item_id"] / "solve" / "attempt-00")
        request = {"reserved_tokens": 36256, "payload": {
            "model": "deepseek-v4-flash", "max_tokens": 32768}}
        response = {"body": {"model": "deepseek-v4-flash", "usage": {
            "prompt_tokens": 732, "completion_tokens": 40628,
            "total_tokens": 41360}, "choices": []}}
        write_once(attempt / "request.json", request)
        write_once(attempt / "response.json", response)
        omission = dict(OMISSION,
                        request_sha256=hashlib.sha256((attempt / "request.json").read_bytes()).hexdigest(),
                        response_sha256=hashlib.sha256((attempt / "response.json").read_bytes()).hexdigest())
        write_once(root / "operator_infrastructure_omission.json", omission)
        return attempt, omission

    def test_exact_omission_preserves_actual_usage_and_skips_only_one_item(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "professional_law"
            attempt, omission = self._fixture(root)
            config = Config(prompt_version="mmlu-general-thinking-v4", thinking="enabled",
                            strict_response_contract=True, content_gated_response=True)
            with patch("dag_builder.operator_omission.OMISSION", omission):
                self.assertEqual(load_operator_omission(root), omission)
                runner = Pipeline(root, config, object(), runtime_workers=64)
                runner._restore_budget()
                self.assertFalse(runner._stop.is_set())
                self.assertEqual(runner.accounted_tokens, 41360)
                result = runner.process({"item_id": OMISSION["item_id"]})
                self.assertEqual(result["status"], "infrastructure_omitted")
                self.assertFalse((attempt / "output.json").exists())

    def test_tampered_or_unlisted_overrun_still_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "professional_law"
            _, omission = self._fixture(root)
            config = Config(prompt_version="mmlu-general-thinking-v4", thinking="enabled",
                            strict_response_contract=True, content_gated_response=True)
            with patch("dag_builder.operator_omission.OMISSION", omission):
                another = root / "items" / ("a" * 20) / "solve" / "attempt-00"
                write_once(another / "request.json", {"reserved_tokens": 36256,
                           "payload": {"model": "deepseek-v4-flash", "max_tokens": 32768}})
                write_once(another / "response.json", {"body": {
                    "model": "deepseek-v4-flash", "usage": {
                        "prompt_tokens": 732, "completion_tokens": 40628,
                        "total_tokens": 41360}, "choices": []}})
                runner = Pipeline(root, config, object())
                runner._restore_budget()
                self.assertTrue(runner._stop.is_set())
                self.assertEqual(len(runner._contract_violations), 1)
                self.assertIn("items/" + "a" * 20, next(iter(runner._contract_violations)))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "professional_law"
            attempt, omission = self._fixture(root)
            with patch("dag_builder.operator_omission.OMISSION", omission):
                (attempt / "response.json").write_text("{}")
                with self.assertRaisesRegex(ValueError, "evidence changed"):
                    load_operator_omission(root)


if __name__ == "__main__":
    unittest.main()
