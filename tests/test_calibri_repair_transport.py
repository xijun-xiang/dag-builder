"""Synthetic, offline tests for partial checked-repair transport continuation."""

import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_repair import CALIBRIRepairPipeline, CHECKED_PROTOCOL
from dag_builder.calibri_repair_transport import (
    PROTOCOL, prepare_partial_transport, replay_terminal_item,
    verify_partial_transport,
)
from dag_builder.client import CallFailure
from dag_builder.config import Config
from dag_builder.pipeline import now
from dag_builder.run_status import record_pause
from dag_builder.stages import prompt
from dag_builder.storage import digest, read_json, write_once
from test_calibri_pipeline import config
from test_calibri_repair import Client, failed_parent, repair_outputs
from test_calibri_repair_plan import checked_plan
from audit_calibri_repair import audit as offline_audit


class FailingDependenciesClient:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        if request["messages"][0]["content"] == prompt(
                "repair", CHECKED_PROTOCOL, "livecodebench"):
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(self.proposal)}}]}
        raise CallFailure("uncertain_remote_state", status=500)


def paused_repair(root):
    parent, outputs = failed_parent(root)
    source = root / "partial-source"
    from dag_builder.calibri_repair import prepare_repair
    prepare_repair(parent, source, prompt_version=CHECKED_PROTOCOL,
                   first_pass=True, max_calls=9, max_reserved_tokens=1200000)
    old_config = config(prompt_version=CHECKED_PROTOCOL, max_calls=9,
                        max_reserved_tokens=1200000)
    old_client = FailingDependenciesClient(checked_plan())
    snapshot = CALIBRIRepairPipeline(source, old_config, old_client).run()
    assert snapshot["results"][0]["status"] == "paused"
    assert snapshot["results"][0]["stage"] == "dependencies"
    write_once(source / "completion.json", {"status": "paused"})
    outputs = repair_outputs(outputs)
    from dag_builder.calibri_repair_plan import AUDIT_CHECKS
    outputs["review_dag"]["checks"].update(dict.fromkeys(AUDIT_CHECKS, True))
    return source, outputs


class PartialTransportTests(unittest.TestCase):
    def test_terminal_raw_replay_rejects_rehashed_dag_contradicting_content(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            parent, outputs = failed_parent(folder)
            source = folder / "accepted-source"
            from dag_builder.calibri_repair import prepare_repair
            prepare_repair(parent, source, prompt_version=CHECKED_PROTOCOL,
                           first_pass=True, max_calls=9, max_reserved_tokens=1200000)
            responses = repair_outputs(outputs)
            from dag_builder.calibri_repair_plan import AUDIT_CHECKS
            responses["repair"] = checked_plan()
            responses["review_dag"]["checks"].update(dict.fromkeys(AUDIT_CHECKS, True))
            conf = config(prompt_version=CHECKED_PROTOCOL, max_calls=9,
                          max_reserved_tokens=1200000)
            CALIBRIRepairPipeline(source, conf, Client(responses, CHECKED_PROTOCOL)).run()
            item = read_json(source / "items.json")[0]
            self.assertEqual(replay_terminal_item(source, item, conf)["status"],
                             "model_accepted")
            directory = source / "items" / item["item_id"]
            dag_path = directory / "dag.json"
            dag = read_json(dag_path)
            dag["nodes"][-1]["statement"] = "This fabricated answer contradicts raw dependency content."
            dag_path.write_text(json.dumps(dag))
            result_path = directory / "result.json"
            result = read_json(result_path)
            result["dag_sha256"] = digest(dag)
            result_path.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "contradicts raw stage outputs"):
                replay_terminal_item(source, item, conf)

    def test_terminal_failure_reason_must_replay_from_raw_content(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            parent, outputs = failed_parent(folder)
            source = folder / "failed-source"
            from dag_builder.calibri_repair import prepare_repair
            prepare_repair(parent, source, prompt_version=CHECKED_PROTOCOL,
                           first_pass=True, max_calls=9, max_reserved_tokens=1200000)
            responses = repair_outputs(outputs)
            responses["repair"] = {"wrong": "shape"}
            conf = config(prompt_version=CHECKED_PROTOCOL, max_calls=9,
                          max_reserved_tokens=1200000)
            CALIBRIRepairPipeline(source, conf, Client(responses, CHECKED_PROTOCOL)).run()
            item = read_json(source / "items.json")[0]
            self.assertEqual(replay_terminal_item(source, item, conf)["status"],
                             "needs_review")
            directory = source / "items" / item["item_id"]
            result_path = directory / "result.json"
            result = read_json(result_path)
            result["reason"] = "fabricated failure"
            result_path.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "terminal reason contradicts"):
                replay_terminal_item(source, item, conf)

    def test_input_only_paused_stage_is_missing_not_an_unbilled_call(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, _ = paused_repair(folder)
            stage = source / "items" / ("a" * 20) / "dependencies"
            (stage / "attempt-00/error.json").unlink()
            (stage / "attempt-00/request.json").unlink()
            (stage / "attempt-00").rmdir()
            record_pause(source, {"item_id": "a" * 20, "status": "paused",
                                  "stage": "dependencies", "reason": "synthetic"}, now())
            report = prepare_partial_transport(source, folder / "new-run",
                max_new_calls=3, max_new_tokens=300000,
                campaign_token_limit=25_000_000)
            self.assertEqual(report["actions"], {"a" * 20: "dependencies"})

    def test_reuse_returned_repair_and_charge_old_http500(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, outputs = paused_repair(folder)
            root = folder / "continuation"
            report = prepare_partial_transport(source, root, max_new_calls=3,
                max_new_tokens=300000, campaign_token_limit=25_000_000)
            self.assertEqual(report["selected_count"], 1)
            self.assertEqual(report["actions"], {"a" * 20: "dependencies"})
            self.assertEqual(report["imported_calls"], 1)
            conf = Config.load(root / "config.json")
            self.assertEqual(conf.max_calls, 4)
            self.assertEqual(len(verify_partial_transport(root, conf)), 1)
            proof = read_json(root / "calibri-normalization-manifest.json")
            self.assertEqual(proof["prior_calls"] + conf.max_calls,
                             report["source_calls"] + 3)
            old_error = source / "items" / ("a" * 20) / "dependencies/attempt-00/error.json"
            self.assertTrue((root / "source-run-evidence" / old_error.relative_to(source)).exists())
            self.assertFalse((root / "items" / ("a" * 20) / "dependencies").exists())
            client = Client({"repair": checked_plan(), "dependencies": outputs["dependencies"],
                             "review_dag": outputs["review_dag"]}, CHECKED_PROTOCOL)
            result = CALIBRIRepairPipeline(root, conf, client, resilient=True).run()
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertEqual(len(client.calls), 2, "the successful repair must not be resampled")
            dag = read_json(root / "items" / ("a" * 20) / "dag.json")
            self.assertEqual(dag["recovery_provenance"]["partial_transport_continuation_protocol"], PROTOCOL)
            self.assertFalse(dag["formal_eligible"])
            write_once(root / "completion.json", {"status": "processed"})
            write_once(root / "code_origin.json", {"source_files": {}, "git_commit": "synthetic"})
            audit = offline_audit(root)
            self.assertTrue(audit["mechanical_pass"])
            self.assertEqual(audit["cumulative_requests"], report["source_calls"] + 2)
            self.assertEqual(audit["transport_continuation"]["new_requests"], 2)
            self.assertEqual(audit["transport_continuation"]["continued_count"], 1)
            self.assertEqual(audit["transport_continuation"]["continued_terminal_replayed_count"], 1)
            self.assertEqual(audit["transport_continuation"]["historical_terminal_count"], 0)

            # Both the result label and DAG digest could be rewritten together;
            # the continued acceptance must still match raw stage content.
            item_dir = root / "items" / ("a" * 20)
            dag_path = item_dir / "dag.json"
            changed = read_json(dag_path)
            changed["nodes"][-1]["statement"] = "Fabricated terminal answer."
            dag_path.write_text(json.dumps(changed))
            result_path = item_dir / "result.json"
            changed_result = read_json(result_path)
            changed_result["dag_sha256"] = digest(changed)
            result_path.write_text(json.dumps(changed_result))
            with self.assertRaisesRegex(ValueError, "contradicts raw stage outputs"):
                offline_audit(root)

    def test_continued_rejection_reason_must_match_raw_review(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, outputs = paused_repair(folder)
            root = folder / "continued-reject"
            prepare_partial_transport(source, root, max_new_calls=3,
                max_new_tokens=300000, campaign_token_limit=25_000_000)
            outputs["review_dag"].update(decision="reject", reason="Synthetic reviewer rejection.",
                                         issues=["Synthetic reviewer rejection."])
            client = Client({"repair": checked_plan(), "dependencies": outputs["dependencies"],
                             "review_dag": outputs["review_dag"]}, CHECKED_PROTOCOL)
            conf = Config.load(root / "config.json")
            result = CALIBRIRepairPipeline(root, conf, client, resilient=True).run()["results"][0]
            self.assertEqual(result["status"], "rejected")
            write_once(root / "completion.json", {"status": "processed"})
            write_once(root / "code_origin.json", {"source_files": {}, "git_commit": "synthetic"})
            self.assertEqual(offline_audit(root)["statuses"], {"rejected": 1})
            result_path = root / "items" / ("a" * 20) / "result.json"
            changed = read_json(result_path)
            changed["reason"] = "fabricated rejection reason"
            result_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "terminal reason contradicts"):
                offline_audit(root)
            changed["reason"] = result["reason"]
            changed["status"] = "needs_review"
            result_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "terminal review verdict changed"):
                offline_audit(root)

    def test_continued_validation_failure_cannot_relabel_reason(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, outputs = paused_repair(folder)
            root = folder / "continued-invalid"
            prepare_partial_transport(source, root, max_new_calls=3,
                max_new_tokens=300000, campaign_token_limit=25_000_000)
            client = Client({"repair": checked_plan(), "dependencies": {"wrong": "shape"}},
                            CHECKED_PROTOCOL)
            conf = Config.load(root / "config.json")
            result = CALIBRIRepairPipeline(root, conf, client, resilient=True).run()["results"][0]
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["stage"], "dependencies")
            write_once(root / "completion.json", {"status": "processed"})
            write_once(root / "code_origin.json", {"source_files": {}, "git_commit": "synthetic"})
            self.assertEqual(offline_audit(root)["statuses"], {"needs_review": 1})
            item_dir = root / "items" / ("a" * 20)
            validation_path = item_dir / "dependencies/validation.json"
            validation_path.write_text(json.dumps({"status": "needs_review",
                                                   "reason": "fabricated validation reason"}))
            result_path = item_dir / "result.json"
            changed = read_json(result_path)
            changed["reason"] = "fabricated validation reason"
            result_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "terminal validation differs from raw content"):
                offline_audit(root)

    def test_returned_response_without_output_is_not_retried(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, _ = paused_repair(folder)
            path = source / "items" / ("a" * 20) / "repair/output.json"
            path.unlink()
            record_pause(source, {"item_id": "a" * 20, "status": "paused",
                                  "stage": "dependencies", "reason": "synthetic"}, now())
            with self.assertRaisesRegex(ValueError, "returned or invalid stage"):
                prepare_partial_transport(source, folder / "forbidden",
                    max_new_calls=3, max_new_tokens=300000,
                    campaign_token_limit=25_000_000)

    def test_tamper_or_overbudget_stops_before_api(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            source, outputs = paused_repair(folder)
            with self.assertRaisesRegex(ValueError, "campaign allocation"):
                prepare_partial_transport(source, folder / "over-budget",
                    max_new_calls=600, max_new_tokens=300000,
                    campaign_token_limit=25_000_000)
            root = folder / "continuation"
            prepare_partial_transport(source, root, max_new_calls=3,
                max_new_tokens=300000, campaign_token_limit=25_000_000)
            path = root / "source-run-evidence" / "items" / ("a" * 20) / "dependencies/attempt-00/error.json"
            path.write_text("{}")
            client = Client({"repair": checked_plan(), "dependencies": outputs["dependencies"],
                             "review_dag": outputs["review_dag"]}, CHECKED_PROTOCOL)
            with self.assertRaisesRegex(ValueError, "source evidence changed"):
                CALIBRIRepairPipeline(root, Config.load(root / "config.json"), client).run()
            self.assertFalse(client.calls)


if __name__ == "__main__":
    unittest.main()
