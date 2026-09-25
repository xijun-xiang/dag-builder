"""Stopped-run continuation tests use synthetic items and a fake API client."""

import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_continuation import audit_completed, prepare_continuation, verify_continuation
from dag_builder.calibri_pipeline import CALIBRIPipeline, prepare
from dag_builder.calibri_repair import CHECKED_PROTOCOL, prepare_repair
from dag_builder.config import Config
from dag_builder.stages import prompt
from dag_builder.storage import read_json, write_bytes_once, write_once
from test_calibri_pipeline import config, setup

V2 = "calibri-lcb-normalize-v2"
V3 = "calibri-lcb-normalize-v3"


class Client:
    def __init__(self, outputs, version):
        self.outputs, self.version, self.calls = outputs, version, []

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in self.outputs
                     if request["messages"][0]["content"] == prompt(s, self.version, "livecodebench"))
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(self.outputs[stage])}}]}


def stopped_fixture(root, *, raw_decision="accept"):
    source, execution, _, outputs = setup(root)
    old = root / "old-v2"
    prepare(source, execution, execution / "input-manifest.json", old, prompt_version=V2)
    output = dict(outputs["review_dag"])
    output["checks"] = dict(output["checks"])
    del output["checks"]["no_invariant_assumed"]
    if raw_decision == "reject":
        output.update(decision="reject", issues=["Synthetic rejected dependency"],
                      reason="Synthetic rejected dependency")
    outputs["review_dag"] = output
    result = CALIBRIPipeline(old, config(prompt_version=V2), Client(outputs, V2)).run()
    assert result["results"][0]["status"] == "needs_review"
    requests = list(old.glob("items/*/*/attempt-*/request.json"))
    write_once(old / "operator-stop-20260923-schema.json", {
        "action": "SIGTERM exact verified worker process group; synthetic test",
        "requests_recorded": len(requests),
        "reserved_tokens": sum(read_json(p)["reserved_tokens"] for p in requests),
        "pending_at_stop": [],
    })
    write_once(old / "code_origin.json", read_json(old / "implementation.json"))
    write_once(old / "launch.json", {"host": "local", "pid": 12345})
    write_once(old / "worker-start.json", {"pid": 12345})
    return old, outputs


class ContinuationTests(unittest.TestCase):
    def test_fixed_graph_one_new_review_replays_old_stages_and_counts_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            old, outputs = stopped_fixture(root)
            new = root / "continue"
            prepared = prepare_continuation(old, new)
            item_id = read_json(new / "items.json")[0]["item_id"]
            self.assertEqual(prepared["actions"][item_id], "one_fixed_graph_schema_review")
            self.assertTrue((new / "items" / item_id / "legacy_review_dag/attempt-00/response.json").exists())
            self.assertFalse((new / "items" / item_id / "result.json").exists())
            self.assertEqual(len(verify_continuation(new, Config.load(new / "config.json"))), 1)
            outputs["review_dag"]["checks"]["no_invariant_assumed"] = True
            client = Client(outputs, V3)
            result = CALIBRIPipeline(new, Config.load(new / "config.json"), client,
                                     resilient=True).run()
            self.assertEqual([r["status"] for r in result["results"]], ["model_accepted"])
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["attempt_count"], 4)  # old three plus one review
            dag = read_json(new / "items" / item_id / "dag.json")
            self.assertEqual(dag["recovery_provenance"]["mode"],
                             "one_fixed_graph_schema_review")
            self.assertFalse(dag["formal_eligible"])
            self.assertEqual(len(verify_continuation(new, Config.load(new / "config.json"))), 1)
            import dag_builder
            package = Path(dag_builder.__file__).parent
            code = read_json(new / "implementation.json")
            write_once(new / "code_origin.json", code)
            for name in code["source_files"]:
                write_bytes_once(new / "controller/code/dag_builder" / name,
                                 (package / name).read_bytes())
            write_once(new / "completion.json", {"status": "processed"})
            audited = audit_completed(new)
            self.assertEqual(audited["statuses"], {"model_accepted": 1})
            self.assertEqual(audited["campaign_requests"], 4)

    def test_raw_reject_is_not_reaudited_and_evidence_tamper_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            old, outputs = stopped_fixture(root, raw_decision="reject")
            new = root / "continue"
            prepared = prepare_continuation(old, new)
            item_id = read_json(new / "items.json")[0]["item_id"]
            self.assertEqual(prepared["actions"][item_id], "carry_terminal_result")
            client = Client(outputs, V3)
            result = CALIBRIPipeline(new, Config.load(new / "config.json"), client,
                                     resilient=True).run()
            self.assertEqual(result["results"][0]["status"], "needs_review")
            self.assertEqual(client.calls, [])
            path = new / "parent-evidence/items" / item_id / "review_dag/attempt-00/response.json"
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "stopped-run evidence changed"):
                verify_continuation(new, Config.load(new / "config.json"))

    def test_carried_terminal_review_audits_with_original_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            old, outputs = stopped_fixture(root, raw_decision="reject")
            new = root / "continue"
            prepare_continuation(old, new)
            client = Client(outputs, V3)
            result = CALIBRIPipeline(new, Config.load(new / "config.json"), client,
                                     resilient=True).run()
            self.assertEqual(result["results"][0]["status"], "needs_review")
            self.assertEqual(client.calls, [])
            import dag_builder
            package = Path(dag_builder.__file__).parent
            code = read_json(new / "implementation.json")
            write_once(new / "code_origin.json", code)
            for name in code["source_files"]:
                write_bytes_once(new / "controller/code/dag_builder" / name,
                                 (package / name).read_bytes())
            write_once(new / "completion.json", {"status": "processed"})
            self.assertEqual(audit_completed(new)["statuses"], {"needs_review": 1})

    def test_graceful_stop_flag_blocks_new_paid_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            _, _, run, outputs = setup(root)
            write_once(run / "operator-stop-request.json", {"reason": "test"})
            client = Client(outputs, "calibri-lcb-normalize-v1")
            result = CALIBRIPipeline(run, config(), client, resilient=True).run()
            self.assertTrue(result["paused"])
            self.assertEqual(client.calls, [])

    def test_completed_v3_dependency_failure_can_enter_one_checked_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source, execution, _, outputs = setup(root)
            old = root / "old-v2"
            prepare(source, execution, execution / "input-manifest.json", old,
                    prompt_version=V2)
            outputs["dependencies"] = {**outputs["dependencies"], "answer_parents": []}
            result = CALIBRIPipeline(old, config(prompt_version=V2), Client(outputs, V2)).run()
            self.assertEqual(result["results"][0]["stage"], "dependencies")
            requests = list(old.glob("items/*/*/attempt-*/request.json"))
            write_once(old / "operator-stop-20260923-schema.json", {
                "action": "SIGTERM exact verified worker process group; synthetic test",
                "requests_recorded": len(requests),
                "reserved_tokens": sum(read_json(p)["reserved_tokens"] for p in requests),
                "pending_at_stop": [],
            })
            write_once(old / "code_origin.json", read_json(old / "implementation.json"))
            write_once(old / "launch.json", {"host": "local", "pid": 12345})
            write_once(old / "worker-start.json", {"pid": 12345})
            new = root / "continue"
            prepare_continuation(old, new)
            CALIBRIPipeline(new, Config.load(new / "config.json"), Client(outputs, V3),
                            resilient=True).run()
            import dag_builder
            package = Path(dag_builder.__file__).parent
            code = read_json(new / "implementation.json")
            write_once(new / "code_origin.json", code)
            for name in code["source_files"]:
                write_bytes_once(new / "controller/code/dag_builder" / name,
                                 (package / name).read_bytes())
            write_once(new / "completion.json", {"status": "processed"})
            self.assertTrue(audit_completed(new)["mechanical_pass"])
            selection = prepare_repair(new, root / "repair", prompt_version=CHECKED_PROTOCOL,
                                       first_pass=True)
            self.assertEqual(selection["selected_count"], 1)


if __name__ == "__main__":
    unittest.main()
