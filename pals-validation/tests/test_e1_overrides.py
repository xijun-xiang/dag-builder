"""Optional MMLU psychology E1 edge selection cannot rewrite DAG data."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pals_validation.io import digest, read, sha256
from pals_validation.prepare import prepare


SOURCE_ID = "cais/mmlu:high_school_psychology:test:7"
SOURCE_RECORD_SHA = "c" * 64


def fixture():
    statements = [
        (1, "knowledge", [], "An initial premise."),
        (2, "derived", [1], "First consequence of the initial premise."),
        (3, "derived", [2], "Second consequence of the first."),
        (4, "knowledge", [], "An independent premise."),
        (5, "derived", [4], "Consequence of the independent premise."),
        (6, "answer", [3, 5], "Choice A."),
    ]
    nodes = [
        {"node_id": i, "kind": kind, "statement": statement, "parents": parents,
         "source_field": "solution", "source_quote": statement,
         "justification": "Fixture dependency."}
        for i, kind, parents, statement in statements
    ]
    return {
        "schema_version": "pals_dag_unified_v1", "item_id": "fixture-psych-7",
        "benchmark": "mmlu",
        "problem": {"question": "Which inference follows?", "domain": "high_school_psychology",
                    "choices": ["Choice A", "Choice B", "Choice C", "Choice D"],
                    "entry_point": None},
        "answer": {"kind": "choice", "value": "A"},
        "dag": {"schema_version": "pals_step_dag_v1", "nodes": nodes,
                "nodes_sha256": digest(nodes)},
        "review": {"source_status": "model_accepted", "model_accepted": True,
                   "human_approved": False,
                   "quality_status": "model_reviewed_pending_human_review",
                   "construction_protocol": None},
        "provenance": {"dataset": "cais/mmlu", "subset": "high_school_psychology",
                       "split": "test", "revision": "fixture", "source_row": 7,
                       "source_id": SOURCE_ID,
                       "source_content_sha256": "a" * 64,
                       "source_schema_version": "fixture-v1", "source_file_sha256": "b" * 64,
                       "source_record_sha256": SOURCE_RECORD_SHA,
                       "source_dag_sha256": "d" * 64},
    }


def manifest(edge, source_id=SOURCE_ID, source_sha=SOURCE_RECORD_SHA, seed=20260915):
    return {
        "schema_version": "pals_mmlu_psych_e1_break_overrides_v1",
        "benchmark": "mmlu", "selection_seed": seed,
        "cohort": [{"item_id": "fixture-psych-7", "source_id": source_id,
                    "source_record_sha256": source_sha}],
        "overrides": [{"item_id": "fixture-psych-7",
                       "parent_id": edge[0], "target_id": edge[1]}],
    }


class E1OverrideTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "accepted.jsonl"
        self.source.write_text(json.dumps(fixture()) + "\n", encoding="utf-8")
        prepare(self.source, self.root / "default", benchmark="mmlu")
        self.original = read(self.root / "default/selection.json")[0]
        old = tuple(self.original["forest_break"]["violated_edges"][0])
        self.edge = (4, 5) if old == (2, 3) else (2, 3)
        self.overrides = self.root / "overrides.json"

    def _write(self, data):
        self.overrides.write_text(json.dumps(data), encoding="utf-8")

    def _prepare(self, data, output="changed", benchmark="mmlu"):
        self._write(data)
        return prepare(self.source, self.root / output, benchmark=benchmark,
                       e1_break_overrides=self.overrides)

    def test_changes_only_forest_break_and_records_complete_provenance(self):
        result = self._prepare(manifest(self.edge))
        selected = read(self.root / "changed/selection.json")[0]
        self.assertEqual(result["protocol"],
                         "mmlu-unified-validation-v1+mmlu-psych-e1-break-overrides-v1")
        self.assertEqual(selected["forest_break_before_override"], self.original["forest_break"])
        self.assertEqual(selected["forest_break_override_edge"], list(self.edge))
        self.assertEqual(selected["forest_break"]["violated_edges"], [list(self.edge)])
        self.assertEqual(selected["orders"]["legal"], self.original["orders"]["legal"])
        self.assertEqual(selected["orders"]["forest_baseline"],
                         self.original["orders"]["forest_baseline"])
        self.assertEqual(read(self.root / "changed/cases.json"),
                         read(self.root / "default/cases.json"))
        self.assertEqual(result["source_sha256"], sha256(self.source))
        provenance = result["e1_break_overrides"]
        self.assertEqual(provenance["manifest_sha256"], sha256(self.overrides))
        self.assertEqual(provenance["cohort_questions"], 1)
        self.assertEqual(provenance["changed_forest_breaks"], 1)
        self.assertIn("e1_overrides.py", provenance["code_sha256"])
        self.assertIn("prepare.py", provenance["code_sha256"])
        self.assertNotIn("e1_break_overrides", read(self.root / "default/manifest.json"))

    def test_rejects_identity_tampering_and_invalid_edges_before_output(self):
        invalid = [
            manifest(self.edge, source_sha="d" * 64),
            manifest(self.edge, source_id="cais/mmlu:high_school_psychology:test:8"),
            manifest(self.edge, seed=7),
            manifest((1, 2)),  # fixed first node may not move
            manifest((2, 5)),  # not a DAG edge
        ]
        for i, value in enumerate(invalid):
            with self.subTest(i=i), self.assertRaises(ValueError):
                self._prepare(value, output=f"bad-{i}")
            self.assertFalse((self.root / f"bad-{i}").exists())
        changed = deepcopy(manifest(self.edge))
        changed["cohort"].append(changed["cohort"][0])
        with self.assertRaises(ValueError):
            self._prepare(changed, output="duplicate")
        with self.assertRaises(ValueError):
            self._prepare(manifest(self.edge), output="wrong-benchmark", benchmark="gsm8k")
        self.overrides.write_text('{"schema_version":"x","schema_version":"y"}',
                                  encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            prepare(self.source, self.root / "duplicate-key", benchmark="mmlu",
                    e1_break_overrides=self.overrides)


if __name__ == "__main__":
    unittest.main()
