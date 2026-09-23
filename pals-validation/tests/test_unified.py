"""Unified release input remains usable without importing dag-builder."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pals_validation.data import normalize
from pals_validation.io import digest
from pals_validation.prepare import prepare
from pals_validation.protocol import parse_step, question, system_prompt
from pals_validation.run import init_run, worker


def row(benchmark):
    code = benchmark in ("humaneval", "livecodebench_v6")
    nodes = [
        {"node_id": 1, "kind": "given", "statement": "A premise.", "parents": [],
         "source_field": "question", "source_quote": "A premise.", "justification": "Given."},
        {"node_id": 2, "kind": "derived", "statement": "A result.", "parents": [1],
         "source_field": "solution", "source_quote": "A result.", "justification": "Derived."},
        {"node_id": 3, "kind": "answer", "statement": "    return 1\n" if code else "Choice A.",
         "parents": [2], "source_field": "reference_code" if code else "correct_answer",
         "source_quote": "    return 1\n" if code else "A", "justification": "Answer."},
    ]
    return {"schema_version": "pals_dag_unified_v1", "item_id": "fixture",
            "benchmark": benchmark,
            "problem": {"question": "A premise.", "domain": "code" if code else "science",
                        "choices": None if code else ["one", "two", "three", "four"],
                        "entry_point": "fixture" if benchmark == "humaneval" else None},
            "answer": {"kind": "code" if code else "choice",
                       "value": "    return 1\n" if code else "A"},
            "dag": {"schema_version": "pals_step_dag_v1", "nodes": nodes,
                    "nodes_sha256": digest(nodes)},
            "review": {"source_status": "model_accepted", "model_accepted": True,
                       "human_approved": False, "quality_status": None,
                       "construction_protocol": None},
            "provenance": {"dataset": "fixture", "subset": "v6" if benchmark == "livecodebench_v6" else benchmark, "split": "test",
                           "revision": "v1", "source_row": 0,
                           "source_id": "HumanEval/0" if benchmark == "humaneval" else "lcb-0" if code else "record-0",
                           "source_content_sha256": "a" * 64,
                           "source_schema_version": "fixture-v1", "source_file_sha256": "b" * 64,
                           "source_record_sha256": "c" * 64,
                           "source_dag_sha256": "d" * 64 if code else None}}


class UnifiedInputTests(unittest.TestCase):
    def test_both_benchmarks_use_the_same_record_keys(self):
        gpqa = normalize(row("gpqa_diamond"), "gpqa")
        human = normalize(row("humaneval"), "humaneval")
        self.assertEqual(gpqa["choices"], ["one", "two", "three", "four"])
        self.assertEqual(human["task_id"], "HumanEval/0")
        self.assertEqual(human["steps"][1]["parents"], [1])
        self.assertNotIn("source_quote", human["steps"][0])
        self.assertEqual(len(gpqa["steps"]), 2)

    def test_rejects_hash_tampering_or_wrong_benchmark(self):
        changed = deepcopy(row("humaneval"))
        changed["dag"]["nodes"][1]["statement"] = "Changed."
        with self.assertRaisesRegex(ValueError, "hash"):
            normalize(changed, "humaneval")
        with self.assertRaisesRegex(ValueError, "benchmark"):
            normalize(row("humaneval"), "gpqa")

    def test_gpqa_diagnostic_preserves_historical_scoring_shape(self):
        diagnostic = row("gpqa_diamond")
        diagnostic["review"]["source_status"] = "model_accepted_diagnostic"
        prepared = normalize(diagnostic, "gpqa")
        self.assertTrue(all("justification" not in step for step in prepared["steps"]))
        self.assertTrue(all("justification" in step for step in diagnostic["dag"]["nodes"]))

    def test_livecodebench_unified_is_direct_prepare_input(self):
        record = row("livecodebench_v6")
        case = normalize(record, "livecodebench")
        self.assertEqual(case["io_type"], "stdin")
        self.assertEqual(case["task_type"], "livecodebench")
        self.assertEqual(question(case), "A premise.")
        self.assertIn("programming problem", system_prompt(case))
        self.assertFalse(parse_step("print(1)</step>", "livecodebench")["valid"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "accepted.jsonl"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            manifest = prepare(source, root / "prepared", benchmark="livecodebench")
            self.assertEqual(manifest["protocol"], "livecodebench-validation-v1")
            self.assertEqual(manifest["questions"], 1)
            self.assertTrue((root / "prepared" / "jobs.json").is_file())
            config = Path(__file__).resolve().parents[1] / "configs/mock.json"
            for experiment in ("e1", "e2"):
                run = root / (experiment + "-mock")
                init_run(root / "prepared", config, run, experiment, 1)
                worker(run, 0)
                self.assertTrue((run / "workers" / "0" / "completion.json").exists())

    def test_livecodebench_functional_entry_and_hash_guard(self):
        record = row("livecodebench_v6")
        record["problem"]["entry_point"] = "solve"
        case = normalize(record, "livecodebench")
        self.assertEqual(case["io_type"], "functional")
        changed = deepcopy(record)
        changed["dag"]["nodes"][0]["statement"] = "tampered"
        with self.assertRaisesRegex(ValueError, "hash"):
            normalize(changed, "livecodebench")


if __name__ == "__main__":
    unittest.main()
