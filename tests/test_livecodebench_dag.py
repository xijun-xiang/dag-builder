"""Synthetic DAG/API/execution evidence only, never execute generated programs."""
import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.livecodebench_dag import LiveCodeBenchDAGPipeline, verify_execution
from dag_builder.livecodebench_source import normalize_livecodebench, REVISION
from dag_builder.report import render
from dag_builder.schemas import validate_review
from dag_builder.stages import STAGES, prompt, stage_input, validate
from dag_builder.storage import digest, read_json, write_bytes_once, write_once
from test_livecodebench import fixture


def sample_item():
    item = normalize_livecodebench([fixture()], REVISION)[0][0]
    return dict(item, reference_code="class Solution:\n    def identity(self,x):\n        return x\n",
                reference_origin="model_generated_test_verified_candidate",
                reference_execution="passed_frozen_tests_not_exhaustive_proof",
                execution_evidence={"status": "passed", "code_sha256": "0" * 64})


def outputs(item):
    rationale = "The input is an integer. The program returns this same input. Therefore it satisfies the identity specification."
    nodes = [
        {"node_id": 1, "kind": "given", "statement": "The input is an integer.",
         "source_field": "question", "source_quote": "input integer"},
        {"node_id": 2, "kind": "given", "statement": "The program returns this same input.",
         "source_field": "reference_code", "source_quote": "return x"},
        {"node_id": 3, "kind": "derived", "statement": "The program satisfies the identity specification.",
         "source_field": "solution", "source_quote": "Therefore it satisfies the identity specification."},
        {"node_id": 4, "kind": "answer", "statement": item["reference_code"],
         "source_field": "reference_code", "source_quote": "return x"}]
    def review(keys):
        return {"decision": "accept", "checks": dict.fromkeys(keys, True), "issues": [], "reason": "Synthetic fixture only."}
    return {"solve": {"rationale": rationale}, "atomize": {"nodes": nodes},
            "dependencies": {"parents": [{"node_id": i, "parents": ps} for i, ps in enumerate(([], [], [1, 2], [3]), 1)]},
            "justify": {"justifications": [{"node_id": i, "text": "Synthetic inference."} for i in range(1, 5)]},
            "review_solution": review(("answer_correct", "intermediate_correct", "premises_complete", "trace_sufficient",
                                       "root_premises_sound", "reference_behavior_faithful")),
            "review_dag": review(("statements_correct", "faithful_to_solution", "dependencies_sufficient", "dependencies_minimal",
                                  "justifications_complete", "no_new_facts", "root_premises_sound", "reference_behavior_faithful",
                                  "self_contained_statements", "no_invariant_assumed", "code_facts_grounded"))}


class Client:
    def __init__(self, item):
        self.calls, self.outputs = [], outputs(item)

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in STAGES if request["messages"][0]["content"] == prompt(s, "livecodebench-dag-v1", "livecodebench"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.outputs[stage])}}]}


class DAGTests(unittest.TestCase):
    def test_synthetic_six_stage_construction_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            item = sample_item()
            test_result = {"status": "passed", "code_sha256": digest(item["reference_code"]),
                           "tests_sha256": item["tests_sha256"], "source_content_sha256": item["source_content_sha256"],
                           "tests": [{"status": "passed"}]}
            write_once(root / "evidence/execution-completion.json", {"fixture": True})
            write_once(root / "evidence/input-manifest.json", {"fixture": True})
            write_once(root / "evidence" / (item["item_id"] + ".json"), test_result)
            from dag_builder.livecodebench_dag import sha256
            item["execution_evidence"].update(result_sha256=digest(test_result), test_count=1,
                completion_sha256=sha256(root / "evidence/execution-completion.json"),
                manifest_sha256=sha256(root / "evidence/input-manifest.json"))
            selection = {"selected_ids": [item["item_id"]]}
            write_once(root / "items.json", [item])
            write_once(root / "selection.json", selection)
            write_once(root / "execution-provenance.json", {"protocol": "livecodebench-dag-v1",
                       "items_sha256": digest([item]),
                       "selection_sha256": digest(selection),
                       "execution_completion_sha256": sha256(root / "evidence/execution-completion.json"),
                       "dag_allocation": {"max_calls": 140, "max_reserved_tokens": 7000000}})
            config = Config(task_type="livecodebench", prompt_version="livecodebench-dag-v1",
                            solution_source="reference_code_explanation", workers=1, max_calls=140)
            client = Client(item)
            for _ in range(2):
                result = LiveCodeBenchDAGPipeline(root, config, client).run()
            self.assertEqual(len(client.calls), 6)
            self.assertEqual(result["results"][0]["status"], "model_accepted")
            dag = read_json(root / "items" / item["item_id"] / "dag.json")
            self.assertEqual(dag["construction_protocol"], "livecodebench-dag-v1")
            self.assertEqual(dag["nodes"][-1]["statement"], item["reference_code"])
            self.assertNotIn("private_test_cases", json.dumps(client.calls))
            self.assertNotIn("execution_evidence", json.dumps(client.calls))
            self.assertTrue((render(root) / "review.html").is_file())

    def test_missing_execution_gate_fails_before_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config = Config(task_type="livecodebench", prompt_version="livecodebench-dag-v1",
                            solution_source="reference_code_explanation", max_calls=140)
            client = Client(sample_item())
            with self.assertRaises(FileNotFoundError):
                LiveCodeBenchDAGPipeline(root, config, client).run()
            self.assertFalse(client.calls)

    def test_no_positional_references_or_derived_code_citations(self):
        item = sample_item()
        value = outputs(item)
        data = stage_input("atomize", item, value, "reference_code_explanation")
        validate("atomize", value["atomize"], data, prompt_version="livecodebench-dag-v1")
        value["atomize"]["nodes"][2]["statement"] = "By step 2 the property holds."
        with self.assertRaises(ValueError):
            validate("atomize", value["atomize"], data, prompt_version="livecodebench-dag-v1")
        value = outputs(item)
        value["atomize"]["nodes"][1]["kind"] = "derived"
        with self.assertRaises(ValueError):
            validate("atomize", value["atomize"], data, prompt_version="livecodebench-dag-v1")

    def test_incomplete_execution_manifest_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            write_once(root / "input-manifest.json", {"protocol": "lcb-reference-seccomp-v1"})
            write_once(root / "completion.json", {"status": "not_finished"})
            with self.assertRaises(ValueError):
                verify_execution(root, root / "input-manifest.json")


if __name__ == "__main__":
    unittest.main()
