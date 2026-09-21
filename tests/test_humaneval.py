"""Synthetic HumanEval contracts; no paid calls, real questions or code execution."""
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.export import release, trajectory
from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_source import normalize_humaneval, prepare_humaneval
from dag_builder.pipeline import Pipeline
from dag_builder.report import render
from dag_builder.schemas import InvalidOutput, public_question
from dag_builder.stages import STAGES, payload, prompt, stage_input, validate
from dag_builder.storage import digest, read_json, write_once


def source(index=0):
    return {"task_id": f"HumanEval/{index}", "entry_point": "fixture_total",
            "prompt": 'def fixture_total(left, right):\n    """Return the sum of the two list totals."""\n',
            "canonical_solution": "    left_total = sum(left)\n    right_total = sum(right)\n    return left_total + right_total\n",
            "test": "def check(candidate):\n    assert candidate([2], [3]) == 5  # PRIVATE_TEST_SENTINEL\n"}


def fixture_config():
    return Config(task_type="humaneval", prompt_version="humaneval-reference-v1",
                  solution_source="reference_code_explanation", workers=1)


def outputs(item):
    statements = ["The inputs are two lists.", "The first list can be reduced by summation.",
                  "The left total is the sum of the first list.", "The second list can be reduced by summation.",
                  "The right total is the sum of the second list.", "Adding the two totals gives the requested result."]
    nodes = [{"node_id": i, "kind": "given" if i == 1 else "derived", "statement": s,
              "source_field": "solution", "source_quote": s} for i, s in enumerate(statements, 1)]
    nodes.append({"node_id": 7, "kind": "answer", "statement": item["canonical_solution"],
                  "source_field": "reference_code", "source_quote": "return left_total + right_total"})
    parent_lists = [[], [1], [2], [1], [4], [3, 5], [6]]
    def review(keys):
        return {"decision": "accept", "checks": {k: True for k in keys}, "issues": [], "reason": "Synthetic fixture only."}
    return {"solve": {"rationale": " ".join(statements)},
            "review_solution": review(("answer_correct", "intermediate_correct", "premises_complete", "trace_sufficient")),
            "atomize": {"nodes": nodes},
            "dependencies": {"parents": [{"node_id": i, "parents": ps} for i, ps in enumerate(parent_lists, 1)]},
            "justify": {"justifications": [{"node_id": i, "text": "Fixture inference."} for i in range(1, 8)]},
            "review_dag": review(("statements_correct", "faithful_to_solution", "dependencies_sufficient",
                                  "dependencies_minimal", "justifications_complete", "no_new_facts"))}


class Client:
    def __init__(self, item):
        self.calls, self.outputs = [], outputs(item)

    def complete(self, request):
        self.calls.append(request)
        stage = next(s for s in STAGES if request["messages"][0]["content"] == prompt(
            s, "humaneval-reference-v1", "humaneval", "reference_code_explanation"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.outputs[stage])}}],
                "usage": {"total_tokens": 100}}


class HumanEvalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.item = normalize_humaneval([source()], "a" * 40)[0]

    def tearDown(self):
        self.temp.cleanup()

    def pipeline(self, client=None):
        write_once(self.root / "items.json", [self.item])
        write_once(self.root / "selection.json", {"selected_ids": [self.item["item_id"]]})
        return Pipeline(self.root, fixture_config(), client or Client(self.item))

    def test_config_explicit_source_and_bounded_concurrency(self):
        for kwargs in ({"solution_source": "independent_generation"}, {"workers": 7}, {"prompt_version": "v1"}):
            values = fixture_config().to_dict(); values.update(kwargs)
            with self.assertRaises(ValueError):
                Config(**values)
        with self.assertRaises(ValueError):
            Config(solution_source="reference_code_explanation")

    def test_source_is_not_executed_and_tests_are_private(self):
        row = source()
        row["prompt"] = "raise RuntimeError('MUST_NOT_EXECUTE')\n" + row["prompt"]
        item = normalize_humaneval([row], "a" * 40)[0]
        self.assertEqual(item["question"], row["prompt"])
        self.assertNotIn("test", item)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(item))
        self.assertEqual(set(public_question(item)), {"task_type", "question", "entry_point"})

    def test_duplicate_empty_and_bad_entry_rejected(self):
        with self.assertRaises(InvalidOutput):
            normalize_humaneval([source(), source()], "a" * 40)
        for key, value in (("canonical_solution", ""), ("entry_point", "missing"), ("test", "x = 1")):
            row = source(); row[key] = value
            with self.assertRaises(ValueError):
                normalize_humaneval([row], "a" * 40)
        with self.assertRaises(ValueError):
            normalize_humaneval([source()], "main")

    def test_offline_full_source_prepare_gzip_and_immutable_resume(self):
        raw = "".join(json.dumps(source(i)) + "\n" for i in range(164)).encode()
        path = self.root / "source.jsonl.gz"; path.write_bytes(gzip.compress(raw))
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        dest = self.root / "prepared"
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            first = prepare_humaneval(dest, path, "a" * 40, checksum, 3)
            self.assertEqual(first, prepare_humaneval(dest, path, "a" * 40, checksum, 3))
        self.assertEqual(first["candidate_count"], 164)
        self.assertEqual(first["selected_count"], 3)
        with self.assertRaises(FileExistsError):
            prepare_humaneval(dest, path, "a" * 40, checksum, 4)
        with self.assertRaises(ValueError):
            prepare_humaneval(self.root / "bad", path, "a" * 40, "0" * 64)

    def test_partial_or_duplicate_official_cohort_refused(self):
        for rows in ([source()], [source()] * 164):
            path = self.root / "subset.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.assertRaises(ValueError):
                prepare_humaneval(self.root / "partial", path, "a" * 40,
                                  hashlib.sha256(path.read_bytes()).hexdigest(), 1)

    def test_construct_export_and_resume_without_extra_calls(self):
        client = Client(self.item)
        runner = self.pipeline(client)
        self.assertEqual(runner.run()["results"][0]["status"], "model_accepted")
        self.assertEqual(self.pipeline(client).run()["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 6)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(client.calls))
        dag = read_json(self.root / "items" / self.item["item_id"] / "dag.json")
        self.assertEqual(dag["reference_solution"]["answer"], self.item["canonical_solution"])
        self.assertEqual(dag["calculation_check"]["status"], "not_checked")
        rendered = trajectory(dag, "node_only")
        self.assertEqual(rendered["question"], self.item["question"])
        self.assertNotIn(self.item["canonical_solution"], json.dumps(rendered["steps"]))
        self.assertIn("HumanEval", (render(self.root) / "review.html").read_text())
        dest = export_validation(self.root)
        self.assertEqual(dest, export_validation(self.root))
        row = json.loads((dest / "model_accepted.jsonl").read_text())
        self.assertFalse(row["human_approved"])
        self.assertTrue(row["model_accepted"])

    def test_explanation_cannot_rewrite_reference_answer(self):
        data = stage_input("solve", self.item, {})
        with self.assertRaises(InvalidOutput):
            validate("solve", {"rationale": "Some explanation.", "answer": "new code"}, data)
        data = stage_input("atomize", self.item, outputs(self.item))
        changed = deepcopy(outputs(self.item)["atomize"])
        changed["nodes"][-1]["statement"] = "return 0"
        with self.assertRaisesRegex(InvalidOutput, "verbatim"):
            validate("atomize", changed, data)
        changed = deepcopy(outputs(self.item)["atomize"])
        changed["nodes"][0].update(source_field="reference_code", source_quote="sum(left)")
        with self.assertRaisesRegex(InvalidOutput, "reasoning premise"):
            validate("atomize", changed, data)

    def test_bad_semantics_are_not_retried_for_acceptance(self):
        client = Client(self.item)
        client.outputs["review_solution"].update(decision="reject", issues=["Unfaithful"], reason="Unfaithful")
        self.assertEqual(self.pipeline(client).run()["results"][0]["status"], "rejected")
        self.assertEqual(len(client.calls), 2)
        with self.assertRaises(InvalidOutput):
            export_validation(self.root)

    def test_export_refuses_partial_or_tampered_inputs(self):
        runner = self.pipeline()
        runner.run(through="solve")
        with self.assertRaisesRegex(InvalidOutput, "incomplete"):
            export_validation(self.root)
        runner.run()
        items = read_json(self.root / "items.json"); items[0]["question"] += "changed"
        (self.root / "items.json").write_text(json.dumps(items))
        with self.assertRaisesRegex(InvalidOutput, "inputs changed"):
            export_validation(self.root)

    def test_export_connects_to_independent_validation_package(self):
        self.pipeline().run()
        exported = export_validation(self.root) / "model_accepted.jsonl"
        package = Path(__file__).resolve().parents[1] / "pals-validation"
        env = dict(os.environ, PYTHONPATH=str(package / "src"), PYTHONDONTWRITEBYTECODE="1")
        for command in (["prepare", "--benchmark", "humaneval", "--source", str(exported), "--output", str(self.root / "validation")],):
            subprocess.run([sys.executable, "-m", "pals_validation.cli", *command], env=env, check=True, capture_output=True)
        normalized = read_json(self.root / "validation/cases.json")
        self.assertNotIn("canonical_solution", json.dumps(normalized))
        self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(normalized))
        self.assertNotIn("source_quote", json.dumps(normalized))
        self.assertEqual(normalized[0]["question"], self.item["question"])


if __name__ == "__main__":
    unittest.main()
