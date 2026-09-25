"""Synthetic execution proofs and fake API only; no candidate code is run."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_pipeline import CALIBRIPipeline, PROTOCOL, prepare
from dag_builder.calibri_normalize import REVIEW_CHECKS as EXTRA_CHECKS
from dag_builder.config import Config
from dag_builder.livecodebench_dag import sha256
from dag_builder.schemas import REVIEW_CHECKS
from dag_builder.stages import prompt
from dag_builder.storage import digest, read_json, write_bytes_once, write_once
from test_calibri_normalize import fixture


def setup(root):
    item, proposal, dependencies = fixture()
    item.update(item_id="a" * 20, row=0, question_id="synthetic", question_title="Synthetic identity",
                tests_sha256="b" * 64, source_content_sha256="c" * 64)
    item["raw_output"] += "This identity algorithm preserves all input text without changing its value.\n```python\n" + item["reference_code"] + "```\n"
    raw = {"id": item["question_id"], "name": item["question_title"], "prompt": item["question"],
           "program": [item["reference_code"]] * 10, "output": [item["raw_output"]] * 10,
           "is_correct": [True] * 10}
    item["origin"] = {"config": "livecodebench_qwen3", "sample_index": 0, "selected_columns_sha256": digest(raw)}
    source, execution, run = root / "source", root / "execution", root / "run"
    selection = {"selected_ids": [item["item_id"]]}
    manifest = {"protocol": "calibri-lcb-source-v2", "items_sha256": digest([item]),
                "selection_sha256": digest(selection)}
    for name, value in (("items.json", [item]), ("selection.json", selection), ("calibri-manifest.json", manifest)):
        write_once(source / name, value)
    write_once(source / "source-rows/livecodebench_qwen3" / (item["item_id"] + ".json"), raw)
    tests = [{"split": s, "index": 0, "inputs": "fixture", "expected": "fixture"} for s in ("public", "private")]
    record = {k: item[k] for k in ("item_id", "tests_sha256", "source_content_sha256")}
    record.update(code=item["reference_code"], code_sha256=digest(item["reference_code"]), tests=tests)
    write_once(execution / "execution-input.json", [record])
    write_bytes_once(execution / "verify_livecodebench_reference.py", b"# synthetic, not executable\n")
    execution_manifest = {"protocol": "lcb-reference-seccomp-v1", "planned": 1,
        "inputs_sha256": digest([record]), "calibri_manifest_sha256": digest(manifest),
        "harness_sha256": sha256(execution / "verify_livecodebench_reference.py")}
    write_once(execution / "input-manifest.json", execution_manifest)
    write_once(execution / "harness-selftest.json", [{"status": s} for s in
        ("passed", "wrong_answer", "passed", "wrong_answer", "passed")])
    result = {k: record[k] for k in ("item_id", "tests_sha256", "source_content_sha256", "code_sha256")}
    result.update(status="passed", tests=[{"status": "passed", "test_sha256": digest(t),
        "index": 0, "split": t["split"]} for t in tests])
    result_path = execution / "results" / (item["item_id"] + ".json")
    write_once(result_path, result)
    write_once(execution / "completion.json", {"policy": "lcb-reference-seccomp-v1", "status": "processed",
        "job_id": "synthetic", "hostname": "synthetic", "executed": 1, "passed": 1,
        "input_manifest_sha256": sha256(execution / "input-manifest.json"),
        "results": {result_path.name: sha256(result_path)}})
    prepare(source, execution, execution / "input-manifest.json", run)
    audit = {"decision": "accept", "checks": dict.fromkeys((*REVIEW_CHECKS["review_dag"], *EXTRA_CHECKS), True),
             "issues": [], "reason": "Synthetic test only."}
    return source, execution, run, {"normalize": proposal, "dependencies": dependencies, "review_dag": audit}


class Client:
    def __init__(self, outputs):
        self.outputs, self.calls = outputs, []

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in self.outputs if request["messages"][0]["content"] == prompt(s, PROTOCOL, "livecodebench"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.outputs[stage])}}]}


def config(**changes):
    return Config(**{**dict(task_type="livecodebench", prompt_version=PROTOCOL,
        solution_source="calibri_reference_normalization", max_calls=12,
        max_reserved_tokens=1200000, workers=1), **changes})


class CALIBRIPipelineTests(unittest.TestCase):
    def test_three_calls_idempotent_no_hidden_data_or_automatic_release(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, run, outputs = setup(Path(folder).resolve())
            client = Client(outputs)
            for _ in range(2):
                result = CALIBRIPipeline(run, config(), client).run()
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertNotIn("never send this", json.dumps(client.calls))
            self.assertNotIn("tests_sha256", json.dumps(client.calls))
            dag = read_json(run / "items" / ("a" * 20) / "dag.json")
            self.assertFalse(dag["formal_eligible"])
            self.assertTrue(dag["nodes"][-1]["excluded_from_pals"])

    def test_semantic_rejection_not_silently_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, run, outputs = setup(Path(folder).resolve())
            outputs["review_dag"].update(decision="reject", issues=["Synthetic flaw"], reason="Synthetic flaw")
            outputs["review_dag"]["checks"]["dependencies_sufficient"] = False
            client = Client(outputs)
            pipeline = CALIBRIPipeline(run, config(), client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "rejected")
            pipeline.run()
            self.assertEqual(len(client.calls), 3)
            self.assertFalse(list(run.glob("items/*/dag.json")))

    def test_tampering_or_allocation_fails_before_requests(self):
        for target in ("items.json", "evidence/completion.json", "evidence/results/" + "a" * 20 + ".json"):
            with tempfile.TemporaryDirectory() as folder:
                _, _, run, outputs = setup(Path(folder).resolve())
                value = read_json(run / target)
                value = [dict(value[0], raw_output="changed")] if isinstance(value, list) else dict(value, changed=True)
                (run / target).write_text(json.dumps(value))
                client = Client(outputs)
                with self.subTest(target=target), self.assertRaises(ValueError):
                    CALIBRIPipeline(run, config(), client).run()
                self.assertFalse(client.calls)
        with tempfile.TemporaryDirectory() as folder:
            _, _, run, outputs = setup(Path(folder).resolve())
            with self.assertRaisesRegex(ValueError, "allocation"):
                CALIBRIPipeline(run, config(max_calls=13), Client(outputs)).run()

    def test_raw_source_changes_rejected_and_protocol_is_explicit(self):
        with self.assertRaises(ValueError):
            Config(task_type="livecodebench", prompt_version=PROTOCOL)
        with tempfile.TemporaryDirectory() as folder:
            source, execution, _, _ = setup(Path(folder).resolve())
            raw_path = next((source / "source-rows").glob("*/*.json"))
            value = read_json(raw_path)
            value["output"][0] += "tampered"
            raw_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "raw CALIBRI"):
                prepare(source, execution, execution / "input-manifest.json", source.parent / "bad")

    def test_v2_changes_prompt_not_validation_or_shared_stage_prompts(self):
        v2 = "calibri-lcb-normalize-v2"
        config(prompt_version=v2)
        self.assertNotEqual(prompt("normalize", PROTOCOL, "livecodebench"), prompt("normalize", v2, "livecodebench"))
        for stage in ("dependencies", "review_dag"):
            self.assertEqual(prompt(stage, PROTOCOL, "livecodebench"), prompt(stage, v2, "livecodebench"))
        with tempfile.TemporaryDirectory() as folder:
            _, _, run, outputs = setup(Path(folder).resolve())
            with self.assertRaisesRegex(ValueError, "prompt version changed"):
                CALIBRIPipeline(run, config(prompt_version=v2), Client(outputs)).run()

    def test_invalid_normalization_stops_at_first_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, run, outputs = setup(Path(folder).resolve())
            outputs = copy.deepcopy(outputs)
            outputs["normalize"]["steps"][0]["source_refs"] = ["Q9999"]
            client = Client(outputs)
            result = CALIBRIPipeline(run, config(), client).run()
            self.assertEqual(result["results"][0]["status"], "needs_review")
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
