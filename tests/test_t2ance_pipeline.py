"""Protocol routing and source labels; no paid model calls or code execution."""

import unittest
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from dag_builder.calibri_normalize import output_source_field, source_units
from dag_builder.config import Config
from dag_builder.livecodebench_dag import sha256
from dag_builder.stages import prompt, stages_for
from dag_builder.storage import digest, read_json, write_bytes_once, write_once
from dag_builder.t2ance_pipeline import T2ancePipeline, prepare
from run_private_pilot import pipeline_type
from test_calibri_normalize import fixture as graph_fixture
from test_t2ance_source import fixture as source_fixture


def prepared_fixture(root, *, held=False, prompt_version="t2ance-lcb-normalize-v1"):
    item, proposal, dependencies = graph_fixture()
    identity, raw = source_fixture()
    item.update({k: identity[k] for k in ("question_id", "question_title", "platform",
                                         "contest_date", "difficulty")})
    item.update(item_id="a" * 20, row=0, tests_sha256="b" * 64,
                source_content_sha256="c" * 64, reference_origin="t2ance_model_output")
    raw["task_id"] = item["question_id"]
    raw["solution_code"] = item["reference_code"]
    raw["full_response"] = (item["raw_output"] +
        "Reading one line and printing it unchanged preserves the required value for every input.\n"
        "```python\n" + item["reference_code"] + "```\n")
    item["raw_output"] = raw["full_response"]
    raw["problem"]["question_content"] = item["question"]
    raw["problem"]["question_title"] = item["question_title"]
    item["origin"] = {"selected_columns_sha256": digest(raw)}
    source, execution, run = root / "source", root / "execution", root / "run"
    items = [item]
    if held:
        untested = deepcopy(item)
        untested["item_id"] = "d" * 20
        items.append(untested)
    manifest = {"protocol": "t2ance-lcb-source-v1", "items_sha256": digest(items)}
    write_once(source / "items.json", items)
    write_once(source / "t2ance-manifest.json", manifest)
    write_once(source / "source-rows" / (item["item_id"] + ".json"), raw)
    tests = [{"split": s, "index": 0, "inputs": "fixture", "expected": "fixture"}
             for s in ("public", "private")]
    record = {k: item[k] for k in ("item_id", "tests_sha256", "source_content_sha256")}
    record.update(code=item["reference_code"], code_sha256=digest(item["reference_code"]), tests=tests)
    write_once(execution / "execution-input.json", [record])
    write_bytes_once(execution / "verify_livecodebench_reference.py", b"# synthetic\n")
    cpu_manifest = {"protocol": "lcb-reference-seccomp-v1", "planned": 1,
                    "inputs_sha256": digest([record]),
                    "t2ance_manifest_sha256": digest(manifest),
                    "source_candidates": len(items),
                    "sample_ids": [item["item_id"]],
                    "held_without_cpu": [untested["item_id"]] if held else [],
                    "harness_sha256": sha256(execution / "verify_livecodebench_reference.py")}
    write_once(execution / "input-manifest.json", cpu_manifest)
    write_once(execution / "harness-selftest.json", [{"status": s} for s in
        ("passed", "wrong_answer", "passed", "wrong_answer", "passed")])
    result = {k: record[k] for k in ("item_id", "tests_sha256", "source_content_sha256", "code_sha256")}
    result.update(status="passed", tests=[{"status": "passed", "test_sha256": digest(t),
        "index": 0, "split": t["split"]} for t in tests])
    result_path = execution / "results" / (item["item_id"] + ".json")
    write_once(result_path, result)
    write_once(execution / "completion.json", {"policy": "lcb-reference-seccomp-v1",
        "status": "processed", "job_id": "synthetic", "hostname": "synthetic", "executed": 1,
        "passed": 1, "input_manifest_sha256": sha256(execution / "input-manifest.json"),
        "results": {result_path.name: sha256(result_path)}})
    prepare(source, execution, execution / "input-manifest.json", run, limit=1,
            prompt_version=prompt_version)
    from dag_builder.calibri_normalize import REVIEW_CHECKS as extra
    from dag_builder.schemas import REVIEW_CHECKS
    audit = {"decision": "accept", "checks": dict.fromkeys((*REVIEW_CHECKS["review_dag"], *extra), True),
             "issues": [], "reason": "Synthetic audit only."}
    return run, {"normalize": proposal, "dependencies": dependencies, "review_dag": audit}


class Client:
    def __init__(self, outputs, prompt_version="t2ance-lcb-normalize-v1"):
        self.outputs, self.calls, self.prompt_version = outputs, [], prompt_version

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in self.outputs if request["messages"][0]["content"] ==
                     prompt(s, self.prompt_version, "livecodebench"))
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(self.outputs[stage])}}]}


class T2ancePipelineTests(unittest.TestCase):
    def test_explicit_source_protocol_required(self):
        config = Config(task_type="livecodebench", prompt_version="t2ance-lcb-normalize-v1",
                        solution_source="t2ance_reference_normalization")
        self.assertEqual(stages_for(config), ("normalize", "dependencies", "review_dag"))
        self.assertIs(pipeline_type(Path("/private/tmp/t2ance-absent"), config), T2ancePipeline)
        self.assertIn("t2ance", prompt("normalize", config.prompt_version, config.task_type))
        self.assertIn("no_invariant_assumed",
                      prompt("review_dag", config.prompt_version, config.task_type))
        with self.assertRaises(ValueError):
            Config(task_type="livecodebench", prompt_version="t2ance-lcb-normalize-v1")
        with self.assertRaises(ValueError):
            Config(task_type="livecodebench", prompt_version="calibri-lcb-normalize-v3",
                   solution_source="t2ance_reference_normalization")

    def test_output_label_is_source_specific_and_calibri_unchanged(self):
        item = {"question": "Identity problem", "raw_output": "Read and print input.",
                "reference_code": "print(input())", "reference_origin": "t2ance_model_output"}
        self.assertEqual(output_source_field(item), "t2ance_output")
        self.assertIn("t2ance_output", {u["source_field"] for u in source_units(item)})
        item["reference_origin"] = "calibri_model_output"
        self.assertEqual(output_source_field(item), "calibri_output")
        self.assertIn("calibri_output", {u["source_field"] for u in source_units(item)})
        item["reference_origin"] = "unknown"
        with self.assertRaises(ValueError):
            source_units(item)

    def test_cpu_gated_three_stage_dag_and_tamper_check(self):
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve())
            config = Config(task_type="livecodebench", prompt_version="t2ance-lcb-normalize-v1",
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            client = Client(outputs)
            result = T2ancePipeline(run, config, client).run()
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            self.assertEqual(len(client.calls), 3)
            self.assertNotIn("tests_sha256", json.dumps(client.calls))
            dag = read_json(run / "items" / ("a" * 20) / "dag.json")
            self.assertFalse(dag["formal_eligible"])
            self.assertEqual(dag["normalization"]["protocol"], "t2ance-lcb-normalize-v1")
            self.assertIn("t2ance_output", {n["source_field"] for n in dag["nodes"]})
            self.assertIn("t2ance-derived", dag["limitation"])
            value = read_json(run / "evidence/completion.json")
            value["changed"] = True
            (run / "evidence/completion.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                T2ancePipeline(run, config, Client(outputs)).run()

    def test_unexecuted_source_candidate_is_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            run, _ = prepared_fixture(Path(folder).resolve(), held=True)
            selection = read_json(run / "selection.json")
            self.assertEqual(selection["source_candidates"], 2)
            self.assertEqual(selection["cpu_passed"], 1)
            self.assertEqual(selection["selected_ids"], ["a" * 20])
            self.assertIn({"item_id": "d" * 20,
                           "reason": "not_independently_cpu_tested"}, selection["excluded"])

    def test_v2_uses_a_new_frozen_protocol_without_changing_v1(self):
        version = "t2ance-lcb-normalize-v2"
        with tempfile.TemporaryDirectory() as folder:
            run, outputs = prepared_fixture(Path(folder).resolve(), prompt_version=version)
            config = Config(task_type="livecodebench", prompt_version=version,
                            solution_source="t2ance_reference_normalization", workers=1,
                            max_calls=24, max_reserved_tokens=2000000)
            self.assertEqual(stages_for(config), ("normalize", "dependencies", "review_dag"))
            self.assertIn("minimal", prompt("normalize", version, "livecodebench"))
            self.assertIn("redundant", prompt("dependencies", version, "livecodebench"))
            client = Client(outputs, version)
            result = T2ancePipeline(run, config, client).run()
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            dag = read_json(run / "items" / ("a" * 20) / "dag.json")
            self.assertEqual(dag["normalization"]["protocol"], version)
            self.assertEqual(dag["construction_protocol"], version)
            self.assertEqual(read_json(run / "t2ance-normalization-manifest.json")["protocol"], version)


if __name__ == "__main__":
    unittest.main()
