"""Budget extension carries known responses, never samples them a second time."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import read_json, write_once
from dag_builder.t2ance_budget_continuation import (
    audit_continuation, audit_paused_parent, prepare_continuation)
from dag_builder.t2ance_pipeline import T2ancePipeline
from dag_builder.t2ance_v3 import EXTRA_REVIEW_CHECKS as V3_CHECKS
from dag_builder.t2ance_v4 import EXTRA_REVIEW_CHECKS as V4_CHECKS, PROTOCOL
from test_t2ance_pipeline import Client, prepared_fixture


class T2anceBudgetContinuationTests(unittest.TestCase):
    def _paused_fixture(self, folder):
        parent, outputs = prepared_fixture(folder, prompt_version=PROTOCOL)
        for key in (*V3_CHECKS, *V4_CHECKS):
            outputs["review_dag"]["checks"][key] = True
        config = Config(task_type="livecodebench", prompt_version=PROTOCOL,
                        solution_source="t2ance_reference_normalization", workers=1,
                        max_calls=3, max_reserved_tokens=15000)
        write_once(parent / "config.json", config.to_dict())
        snapshot = {"source_files": {}, "code_sha256": "synthetic"}
        write_once(parent / "code_origin.json", snapshot)
        write_once(parent / "controller/code/snapshot_origin.json", snapshot)
        client = Client(outputs, PROTOCOL)
        result = T2ancePipeline(parent, config, client).run()
        self.assertEqual(result["results"][0]["reason"], "budget_exhausted")
        self.assertEqual(len(client.calls), 1)
        row = result["results"][0]
        write_once(parent / "completion.json", {
            "status": "paused", "global_stop": True,
            "summary": {"selected": 1, "api_attempts": 1,
                        "counts": {"paused": 1}, "items": [row]},
        })
        return parent, outputs

    def test_carries_first_response_and_only_calls_unfinished_stages(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            folder = Path(temporary)
            parent, outputs = self._paused_fixture(folder)
            destination = folder / "continued"
            summary = prepare_continuation(
                parent, folder / "source", folder / "execution",
                folder / "execution/input-manifest.json", destination,
                max_calls=10, max_reserved_tokens=2000000,
                prior_calls=3, prior_reserved_tokens=15000)
            self.assertEqual(summary["carried_attempts"], 1)
            config = Config.load(destination / "config.json")
            client = Client(outputs, PROTOCOL)
            result = T2ancePipeline(destination, config, client).run()
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertEqual(len(client.calls), 2)
            snapshot = {"source_files": {}, "code_sha256": "synthetic"}
            write_once(destination / "code_origin.json", snapshot)
            write_once(destination / "controller/code/snapshot_origin.json", snapshot)
            write_once(destination / "completion.json", {
                "status": "processed", "global_stop": False,
                "summary": {"selected": 1, "api_attempts": 3,
                            "counts": {"model_accepted": 1}},
            })
            self.assertTrue(audit_continuation(destination)["mechanical_pass"])
            carried = destination / "items" / ("a" * 20) / "normalize/attempt-00/response.json"
            carried.write_text(carried.read_text() + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "carried request, response"):
                audit_continuation(destination)

    def test_unknown_parent_request_is_not_eligible(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            parent, _ = self._paused_fixture(Path(temporary))
            request = next(parent.glob("items/*/normalize/attempt-*/request.json"))
            (request.parent / "response.json").unlink()
            with self.assertRaisesRegex(ValueError, "unknown or failed remote request"):
                audit_paused_parent(parent)


if __name__ == "__main__":
    unittest.main()
