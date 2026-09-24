"""GSM8K/MMLU unified inputs keep their distinct answer and prompt contracts."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pals_validation.data import normalize
from pals_validation.io import digest, read
from pals_validation.prepare import prepare
from pals_validation.protocol import question, system_prompt
from pals_validation.run import init_run, worker


def row(benchmark="gsm8k", subset="main"):
    nodes = [
        {"node_id": 1, "kind": "given", "statement": "There are two boxes.", "parents": [],
         "source_field": "question", "source_quote": "two boxes", "justification": "Given."},
        {"node_id": 2, "kind": "derived", "statement": "Each box has three items, so there are six.",
         "parents": [1], "source_field": "solution", "source_quote": "2 * 3 = 6",
         "justification": "Multiply the counts."},
        {"node_id": 3, "kind": "answer", "statement": "The answer is six.", "parents": [2],
         "source_field": "solution", "source_quote": "6", "justification": "Result."},
    ]
    gsm8k = benchmark == "gsm8k"
    dataset = "openai/gsm8k" if gsm8k else "cais/mmlu"
    return {"schema_version": "pals_dag_unified_v1", "item_id": "fixture", "benchmark": benchmark,
            "problem": {"question": "There are two boxes of three items. How many?",
                        "domain": "grade_school_math" if gsm8k else subset,
                        "choices": None if gsm8k else ["6", "5", "4", "3"],
                        "entry_point": None},
            "answer": {"kind": "text" if gsm8k else "choice", "value": "6" if gsm8k else "A"},
            "dag": {"schema_version": "pals_step_dag_v1", "nodes": nodes, "nodes_sha256": digest(nodes)},
            "review": {"source_status": "model_accepted", "model_accepted": True,
                       "human_approved": False, "quality_status": "model_reviewed_pending_human_review",
                       "construction_protocol": None},
            "provenance": {"dataset": dataset, "subset": subset, "split": "test",
                           "revision": "fixture", "source_row": 7,
                           "source_id": f"{dataset}:{subset}:test:7",
                           "source_content_sha256": "a" * 64,
                           "source_schema_version": "fixture-v1", "source_file_sha256": "b" * 64,
                           "source_record_sha256": "c" * 64,
                           "source_dag_sha256": "d" * 64}}


class UnifiedReasoningTests(unittest.TestCase):
    def test_gsm8k_has_open_answer_prompt_and_no_answer_node_in_steps(self):
        record = row()
        case = normalize(record, "gsm8k")
        self.assertEqual(case["task_type"], "gsm8k")
        self.assertEqual(case["choices"], None)
        self.assertEqual(question(case), record["problem"]["question"])
        self.assertEqual([node["node_id"] for node in case["steps"]], [1, 2])
        self.assertEqual(system_prompt(case), system_prompt({"task_type": "gpqa"}))

    def test_mmlu_has_labeled_choices_and_subset_provenance(self):
        for subset in ("abstract_algebra", "elementary_mathematics",
                       "high_school_psychology", "human_sexuality"):
            with self.subTest(subset=subset):
                case = normalize(row("mmlu", subset), "mmlu")
                self.assertEqual(case["domain"], subset)
                self.assertIn("A. 6", question(case))
                self.assertIn("D. 3", question(case))
                self.assertNotIn("The answer is six.", question(case))

    def test_rejects_answer_source_and_graph_mismatches(self):
        for benchmark, subset in (("gsm8k", "main"), ("mmlu", "sociology")):
            with self.subTest(benchmark=benchmark):
                base = row(benchmark, subset)
                for field, bad in (("split", "train"), ("source_id", "wrong"),
                                   ("dataset", "wrong")):
                    changed = deepcopy(base)
                    changed["provenance"][field] = bad
                    with self.assertRaises(ValueError):
                        normalize(changed, benchmark)
                changed = deepcopy(base)
                changed["answer"]["kind"] = "choice" if benchmark == "gsm8k" else "text"
                with self.assertRaises(ValueError):
                    normalize(changed, benchmark)
                changed = deepcopy(base)
                changed["review"]["source_status"] = "unreviewed"
                with self.assertRaises(ValueError):
                    normalize(changed, benchmark)
                changed = deepcopy(base)
                changed["dag"]["nodes"][1]["statement"] = "Tampered."
                with self.assertRaisesRegex(ValueError, "hash"):
                    normalize(changed, benchmark)
                changed = deepcopy(base)
                changed["dag"]["nodes"][1], changed["dag"]["nodes"][2] = (
                    changed["dag"]["nodes"][2], changed["dag"]["nodes"][1])
                changed["dag"]["nodes_sha256"] = digest(changed["dag"]["nodes"])
                with self.assertRaises(ValueError):
                    normalize(changed, benchmark)
        with self.assertRaisesRegex(ValueError, "benchmark"):
            normalize(row("mmlu", "sociology"), "gsm8k")
        changed = row("mmlu", "sociology")
        changed["problem"]["choices"] = changed["problem"]["choices"][:3]
        with self.assertRaises(ValueError):
            normalize(changed, "mmlu")
        with self.assertRaises(ValueError):
            normalize(row("mmlu", "unlisted_subset"), "mmlu")

    def test_prepare_and_mock_e1_e2_are_independent_protocols(self):
        config = Path(__file__).resolve().parents[1] / "configs/mock.json"
        for benchmark, subset, protocol, policy in (
                ("gsm8k", "main", "gsm8k-unified-validation-v1", "none_open_answer"),
                ("mmlu", "sociology", "mmlu-unified-validation-v1", "always_included_in_v1")):
            with self.subTest(benchmark=benchmark), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "accepted.jsonl"
                source.write_text(json.dumps(row(benchmark, subset)) + "\n", encoding="utf-8")
                manifest = prepare(source, root / "prepared", benchmark=benchmark)
                self.assertEqual(manifest["protocol"], protocol)
                self.assertEqual(manifest["question_choices"], policy)
                self.assertEqual(manifest["questions"], 1)
                jobs = read(root / "prepared/jobs.json")
                self.assertTrue(any(job["kind"] == "e1" for job in jobs))
                self.assertTrue(any(job["kind"] == "e2" for job in jobs))
                for experiment in ("e1", "e2"):
                    run = root / (experiment + "-mock")
                    init_run(root / "prepared", config, run, experiment, 1)
                    worker(run, 0)
                    self.assertEqual(read(run / "workers/0/completion.json")["status"], "complete")


if __name__ == "__main__":
    unittest.main()
