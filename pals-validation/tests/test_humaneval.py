"""Self-contained fixtures; PALS must not depend on the parent builder package."""
import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pals_validation.analyze import analyze
from pals_validation.data import normalize
from pals_validation.io import digest, read
from pals_validation.prepare import prepare
from pals_validation.protocol import SYSTEM, parse_step, question, system_prompt
from pals_validation.run import init_run, worker
from test_validation import fixture as gpqa_fixture


def fixture():
    record = gpqa_fixture()
    source = {"item_id": "fixture", "dataset": "openai/human-eval", "revision": "a" * 40,
              "task_id": "HumanEval/0", "task_type": "humaneval", "subset": "humaneval",
              "question": 'def fixture_fn(x):\n    """Synthetic input, preserve indentation."""\n',
              "entry_point": "fixture_fn", "canonical_solution": "    return 'PRIVATE_ANSWER_SENTINEL'\n",
              "test": "PRIVATE_TEST_SENTINEL", "row": 0}
    record.update(schema_version="humaneval_validation_export_v1", source=source)
    dag = record["dag"]
    dag.update(source=copy.deepcopy(source), item_id="fixture", construction_protocol="humaneval-reference-v1")
    for n in dag["nodes"]:
        n["source_field"] = "solution"
        n["source_quote"] = "PRIVATE_CITATION_SENTINEL"
        n["justification"] = "PRIVATE_JUSTIFICATION_SENTINEL"
    dag["nodes"][-1].update(statement=source["canonical_solution"], source_field="reference_code")
    for name, keys in {"solution_review": ("answer_correct", "intermediate_correct", "premises_complete", "trace_sufficient"),
                       "dag_review": ("statements_correct", "faithful_to_solution", "dependencies_sufficient",
                                      "dependencies_minimal", "justifications_complete", "no_new_facts")}.items():
        dag[name] = {"decision": "accept", "issues": [], "checks": {k: True for k in keys}}
    record["dag_sha256"] = digest(dag)
    return record


class HumanEvalValidationTests(unittest.TestCase):
    def test_explicit_benchmark_and_source_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            normalize(fixture())
        with self.assertRaises(ValueError):
            normalize(gpqa_fixture(), "humaneval")
        with self.assertRaises(ValueError):
            normalize(fixture(), "unknown")

    def test_accepted_export_hash_required(self):
        r = fixture(); r["dag"]["nodes"][0]["statement"] += "edited"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            normalize(r, "humaneval")

    def test_failed_review_cannot_be_spoofed_with_top_level_accept(self):
        r = fixture(); r["dag"]["dag_review"]["checks"]["dependencies_minimal"] = False
        r["dag_sha256"] = digest(r["dag"])
        with self.assertRaisesRegex(ValueError, "Unaccepted"):
            normalize(r, "humaneval")

    def test_private_fields_dropped_and_prompt_preserved(self):
        r = fixture(); c = normalize(r, "humaneval")
        self.assertNotIn("PRIVATE_", json.dumps(c))
        self.assertEqual(question(c), r["source"]["question"])
        self.assertNotIn("A.", question(c))
        self.assertIn("natural language", system_prompt(c))
        self.assertEqual(system_prompt(normalize(gpqa_fixture())), SYSTEM)

    def test_code_or_frame_in_reference_step_rejected(self):
        for s in ("```python\nreturn x\n```", "hello</step>"):
            r = fixture(); r["dag"]["nodes"][0]["statement"] = s
            r["dag_sha256"] = digest(r["dag"])
            with self.assertRaises(ValueError):
                normalize(r, "humaneval")

    def test_natural_language_contract(self):
        for text in ("```python\nreturn x\n```</step>", "def f(x):\n    return x</step>", "return sum(xs)</step>"):
            self.assertFalse(parse_step(text, "humaneval")["valid"])
        text = "The running sum accounts for all previously visited elements.</step>"
        self.assertTrue(parse_step(text, "humaneval")["valid"])
        self.assertTrue(parse_step("return the computed total to satisfy the specification.</step>", "humaneval")["valid"])

    def test_one_step_retained_as_inapplicable_not_fabricated(self):
        r = fixture()
        r["dag"]["nodes"] = [r["dag"]["nodes"][0], r["dag"]["nodes"][-1]]
        r["dag"]["nodes"][-1]["parents"] = [1]
        r["dag_sha256"] = digest(r["dag"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"; source.write_text(json.dumps(r) + "\n")
            m = prepare(source, root / "prepared", benchmark="humaneval")
            self.assertEqual(m["questions"], 1)
            self.assertEqual(m["counts"]["e2"], 0)
            self.assertEqual(read(root / "prepared/jobs.json"), [])

    def test_e1_e2_end_to_end_and_no_private_inference_inputs(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            source = root / "source.jsonl"; source.write_text(json.dumps(fixture()) + "\n")
            prepared = root / "prepared"
            manifest = prepare(source, prepared, benchmark="humaneval")
            self.assertEqual(manifest["protocol"], "humaneval-validation-v1")
            self.assertEqual(manifest["counts"]["fair_pair"], 1)
            for filename in ("cases.json", "jobs.json"):
                self.assertNotIn("PRIVATE_", (prepared / filename).read_text())
            cfg = Path(__file__).resolve().parents[1] / "configs/mock.json"
            for experiment in ("e1", "e2"):
                run = root / experiment
                init_run(prepared, cfg, run, experiment, 2)
                worker(run, 0); worker(run, 1)
                self.assertEqual(worker(run, 0)["status"], "already_complete")
                result = analyze(run, root / (experiment + "-analysis"))
                self.assertFalse(result["scientific_evidence"])
                if experiment == "e2":
                    self.assertEqual(result["complete_questions"], 1)
                else:
                    self.assertEqual(len(result["paired_questions"]["fair_break_minus_legal"]), 1)


if __name__ == "__main__":
    unittest.main()
