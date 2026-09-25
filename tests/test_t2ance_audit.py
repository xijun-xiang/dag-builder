"""Offline t2ance request, result and code-snapshot audit; no network calls."""

import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import digest, read_json, write_once
from dag_builder.t2ance_audit import audit_completed
from dag_builder.t2ance_pipeline import T2ancePipeline
from test_t2ance_pipeline import Client, prepared_fixture


class T2anceAuditTests(unittest.TestCase):
    def test_completed_canary_replays_and_rejects_tampered_response(self):
        for version in ("t2ance-lcb-normalize-v1", "t2ance-lcb-normalize-v2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=version)
                config = Config(task_type="livecodebench", prompt_version=version,
                                solution_source="t2ance_reference_normalization", workers=1,
                                max_calls=24, max_reserved_tokens=2000000)
                write_once(run / "config.json", config.to_dict())
                snapshot = {"source_files": {}, "code_sha256": "synthetic"}
                write_once(run / "code_origin.json", snapshot)
                write_once(run / "controller/code/snapshot_origin.json", snapshot)
                result = T2ancePipeline(run, config, Client(outputs, version)).run()
                write_once(run / "completion.json", {"status": "processed", "global_stop": False,
                    "summary": {"selected": 1, "api_attempts": result["attempt_count"],
                                "counts": {"model_accepted": 1}}})
                report = audit_completed(run)
                self.assertTrue(report["mechanical_pass"])
                self.assertEqual(report["accepted_ids"], ["a" * 20])
                directory = run / "items" / ("a" * 20)
                normalization = directory / "normalization.json"
                original_normalization = normalization.read_text()
                corrupted = read_json(normalization)
                corrupted["nodes"][0]["statement"] = "Altered statement"
                normalization.write_text(json.dumps(corrupted))
                with self.assertRaises(ValueError):
                    audit_completed(run)
                normalization.write_text(original_normalization)
                dag_path = directory / "dag.json"
                result_path = directory / "result.json"
                original_dag, original_result = dag_path.read_text(), result_path.read_text()
                corrupted_dag = read_json(dag_path)
                corrupted_dag["nodes"][0]["statement"] = "Altered statement"
                dag_path.write_text(json.dumps(corrupted_dag))
                corrupted_result = read_json(result_path)
                corrupted_result["dag_sha256"] = digest(corrupted_dag)
                result_path.write_text(json.dumps(corrupted_result))
                with self.assertRaises(ValueError):
                    audit_completed(run)
                dag_path.write_text(original_dag)
                result_path.write_text(original_result)
                response = run / "items" / ("a" * 20) / "normalize/attempt-00/response.json"
                value = read_json(response)
                value["body"]["choices"][0]["message"]["content"] = "{}"
                response.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    audit_completed(run)


if __name__ == "__main__":
    unittest.main()
