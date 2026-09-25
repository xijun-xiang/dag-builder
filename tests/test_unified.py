"""Synthetic contracts for the common final-data format; no benchmark text."""

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dag_builder.storage import digest
from dag_builder.unified import ROW_FIELDS, convert_file, convert_record, validate_row


SOURCE_HASH = "b" * 64


def nodes(code=False):
    return [
        {"node_id": 1, "kind": "given", "statement": "A premise.", "parents": [],
         "source_field": "question", "source_quote": "A premise.", "justification": "Quoted premise."},
        {"node_id": 2, "kind": "derived", "statement": "A consequence.", "parents": [1],
         "source_field": "solution", "source_quote": "A consequence.", "justification": "Follows from node 1."},
        {"node_id": 3, "kind": "answer", "statement": "    return 1\n" if code else "Choice A.",
         "parents": [2], "source_field": "reference_code" if code else "correct_answer",
         "source_quote": "    return 1\n" if code else "A", "justification": "Terminal result."},
    ]


def gpqa(diagnostic=False):
    source = {"item_id": "gpqa-fixture", "dataset": "fixture/gpqa", "subset": "gpqa_diamond",
              "split": "test", "revision": "fixture-v1", "record_id": "record-0", "row": 0,
              "domain": "fixture science",
              "question": "Which option follows?", "choices": ["one", "two", "three", "four"],
              "gold_answer": "A", "source_content_sha256": "a" * 64}
    graph_nodes = nodes()
    dag = None if diagnostic else {"item_id": source["item_id"], "source": source,
                                   "nodes": graph_nodes, "quality_status": "model_reviewed"}
    candidate = {"nodes": [{k: v for k, v in node.items() if k not in ("parents", "justification")}
                           for node in graph_nodes],
                 "parents": [{"node_id": node["node_id"], "parents": node["parents"]}
                             for node in graph_nodes],
                 "justifications": [{"node_id": node["node_id"], "text": node["justification"]}
                                    for node in graph_nodes]}
    return {"schema_version": "gpqa_all_outcomes_v1", "item_id": source["item_id"],
            "source": source, "status": "model_accepted_diagnostic" if diagnostic else "model_accepted",
            "model_accepted": True, "human_approved": False, "dag": dag, "candidate": candidate}


def humaneval():
    source = {"item_id": "human-fixture", "dataset": "openai/human-eval", "subset": "humaneval",
              "split": "test", "revision": "a" * 40, "task_id": "HumanEval/0", "row": 0,
              "domain": "code",
              "task_type": "humaneval", "question": "def fixture():\n    pass\n",
              "entry_point": "fixture", "canonical_solution": "    return 1\n",
              "source_content_sha256": "c" * 64}
    dag = {"item_id": source["item_id"], "source": source, "nodes": nodes(code=True),
           "quality_status": "model_reviewed_pending_human_review",
           "construction_protocol": "humaneval-reference-v3"}
    return {"schema_version": "humaneval_validation_export_v1", "item_id": source["item_id"],
            "source": source, "dag": dag, "dag_sha256": digest(dag),
            "status": "model_accepted", "model_accepted": True, "human_approved": False}


def livecodebench(io_type="stdin"):
    entry_point = "solve" if io_type == "functional" else None
    source = {"item_id": "lcb-fixture", "question_id": "fixture-0", "dataset": "livecodebench/code_generation_lite",
              "subset": "v6", "split": "test", "revision": "fixture-v1", "row": 0,
              "task_type": "livecodebench", "io_type": io_type, "entry_point": entry_point,
              "domain": "code", "question": "Compute the result.",
              "starter_code": "def solve(x):\n    pass\n" if io_type == "functional" else "",
              "reference_code": "print(1)\n",
              "source_content_sha256": "d" * 64,
              "execution_evidence": {"status": "passed", "result_sha256": "e" * 64}}
    graph_nodes = nodes(code=True)
    graph_nodes[-1]["statement"] = source["reference_code"]
    graph_nodes[-1]["source_quote"] = source["reference_code"]
    dag = {"item_id": source["item_id"], "source": source, "nodes": graph_nodes,
           "formal_eligible": False, "quality_status": "model_reviewed",
           "construction_protocol": "calibri-lcb-normalize-v3"}
    return {"schema_version": "calibri-lcb-v6-model-candidates-v1", "item_id": source["item_id"],
            "question_id": source["question_id"], "source": source, "dag": dag,
            "dag_sha256": digest(dag), "cpu_result_sha256": "e" * 64,
            "model_accepted": True, "human_approved": False, "formal_eligible": False,
            "source_status": "calibri_derived_tested_reference"}


class UnifiedTests(unittest.TestCase):
    def test_machine_schema_matches_exporter_keys(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas/pals-dag-unified-v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(set(schema["required"]), ROW_FIELDS)
        self.assertEqual(set(schema["properties"]), ROW_FIELDS)
        self.assertEqual(set(schema["$defs"]["node"]["required"]), set(nodes()[0]))

    def test_both_benchmarks_have_the_same_fixed_shape(self):
        for source, benchmark in ((gpqa(), "gpqa_diamond"), (humaneval(), "humaneval")):
            row = convert_record(source, benchmark, SOURCE_HASH)
            self.assertEqual(set(row), ROW_FIELDS)
            self.assertEqual(set(row["dag"]["nodes"][0]), set(row["dag"]["nodes"][1]))
            validate_row(row)
        self.assertEqual(convert_record(humaneval(), "humaneval", SOURCE_HASH)["problem"]["choices"], None)

    def test_livecodebench_stdin_and_functional_use_same_schema(self):
        for io_type in ("stdin", "functional"):
            source = livecodebench(io_type)
            row = convert_record(source, "livecodebench_v6", SOURCE_HASH)
            validate_row(row)
            self.assertEqual(row["problem"]["entry_point"], source["source"]["entry_point"])
            self.assertEqual("Starter code:" in row["problem"]["question"], io_type == "functional")
            self.assertEqual(row["answer"], {"kind": "code", "value": "print(1)\n"})
            self.assertFalse(row["review"]["human_approved"])
            self.assertEqual(set(row["dag"]["nodes"][0]), set(nodes()[0]))

    def test_t2ance_is_explicitly_stratified_and_cannot_masquerade_as_calibri(self):
        source = livecodebench()
        source["schema_version"] = "lcb-v6-source-stratified-candidates-v1"
        source["source_status"] = "t2ance_derived_tested_reference"
        source["source"]["reference_origin"] = "t2ance_model_output"
        source["dag"]["source"] = source["source"]
        source["dag"]["construction_protocol"] = "t2ance-lcb-normalize-v1"
        source["dag"]["normalization"] = {"protocol": "t2ance-lcb-normalize-v1"}
        source["dag_sha256"] = digest(source["dag"])
        row = convert_record(source, "livecodebench_v6", SOURCE_HASH)
        self.assertEqual(row["review"]["source_status"], "t2ance_derived_tested_reference")
        wrong = deepcopy(source)
        wrong["schema_version"] = "calibri-lcb-v6-model-candidates-v1"
        with self.assertRaisesRegex(ValueError, "source protocol"):
            convert_record(wrong, "livecodebench_v6", SOURCE_HASH)
        wrong = deepcopy(source)
        wrong["dag"]["construction_protocol"] = "calibri-lcb-normalize-v3"
        wrong["dag_sha256"] = digest(wrong["dag"])
        with self.assertRaisesRegex(ValueError, "t2ance protocol"):
            convert_record(wrong, "livecodebench_v6", SOURCE_HASH)

    def test_t2ance_v4_release_keeps_source_and_protocol_explicit(self):
        source = livecodebench()
        source["schema_version"] = "lcb-v6-t2ance-v4-candidates-v1"
        source["source_status"] = "t2ance_derived_tested_reference"
        source["source"]["reference_origin"] = "t2ance_model_output"
        source["dag"]["source"] = source["source"]
        source["dag"]["construction_protocol"] = "t2ance-lcb-normalize-v4"
        source["dag"]["normalization"] = {"protocol": "t2ance-lcb-normalize-v4"}
        source["dag_sha256"] = digest(source["dag"])
        row = convert_record(source, "livecodebench_v6", SOURCE_HASH)
        validate_row(row)
        self.assertEqual(row["review"]["source_status"],
                         "t2ance_derived_tested_reference")
        wrong = deepcopy(source)
        wrong["dag"]["normalization"]["protocol"] = "t2ance-lcb-normalize-v3"
        wrong["dag_sha256"] = digest(wrong["dag"])
        with self.assertRaisesRegex(ValueError, "t2ance protocol"):
            convert_record(wrong, "livecodebench_v6", SOURCE_HASH)
        wrong = deepcopy(source)
        wrong["dag"]["construction_protocol"] = "t2ance-lcb-normalize-v3"
        wrong["dag"]["normalization"]["protocol"] = "t2ance-lcb-normalize-v3"
        wrong["dag_sha256"] = digest(wrong["dag"])
        with self.assertRaisesRegex(ValueError, "t2ance protocol"):
            convert_record(wrong, "livecodebench_v6", SOURCE_HASH)
        wrong = deepcopy(source)
        wrong["schema_version"] = "lcb-v6-source-stratified-candidates-v1"
        with self.assertRaisesRegex(ValueError, "t2ance protocol"):
            convert_record(wrong, "livecodebench_v6", SOURCE_HASH)

    def test_revision_candidate_keeps_source_tier_and_normalization_protocol(self):
        for tier, origin, normalization in (
            ("calibri_derived_tested_reference", "calibri_model_output",
             "calibri-lcb-normalize-v1"),
            ("t2ance_derived_tested_reference", "t2ance_model_output",
             "t2ance-lcb-normalize-v4"),
        ):
            source = livecodebench()
            source["schema_version"] = "lcb-v6-dag-revision-candidates-v1"
            source["source_status"] = tier
            source["source"]["reference_origin"] = origin
            source["dag"]["source"] = source["source"]
            source["dag"]["construction_protocol"] = "lcb-dag-revision-v3"
            source["dag"]["normalization"] = {"protocol": normalization}
            source["dag_sha256"] = digest(source["dag"])
            row = convert_record(source, "livecodebench_v6", SOURCE_HASH)
            self.assertEqual(row["review"]["construction_protocol"],
                             "lcb-dag-revision-v3")
            wrong = deepcopy(source)
            wrong["dag"]["normalization"]["protocol"] = "t2ance-lcb-normalize-v1"
            wrong["dag_sha256"] = digest(wrong["dag"])
            if origin == "t2ance_model_output":
                with self.assertRaisesRegex(ValueError, "t2ance protocol"):
                    convert_record(wrong, "livecodebench_v6", SOURCE_HASH)

    def test_livecodebench_rejects_unpassed_or_tampered_source(self):
        source = livecodebench()
        source["source"]["execution_evidence"]["status"] = "failed"
        source["dag"]["source"] = source["source"]
        source["dag_sha256"] = digest(source["dag"])
        with self.assertRaisesRegex(ValueError, "execution"):
            convert_record(source, "livecodebench_v6", SOURCE_HASH)
        source = livecodebench()
        source["dag"]["nodes"][-1]["statement"] = "print(2)\n"
        source["dag_sha256"] = digest(source["dag"])
        with self.assertRaisesRegex(ValueError, "answer/code"):
            convert_record(source, "livecodebench_v6", SOURCE_HASH)
        source = livecodebench()
        source["cpu_result_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "execution"):
            convert_record(source, "livecodebench_v6", SOURCE_HASH)

    def test_livecodebench_release_has_unified_jsonl_and_viewer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "accepted_candidates.jsonl"
            payload = (json.dumps(livecodebench(), ensure_ascii=False) + "\n").encode("utf-8")
            source.write_bytes(payload)
            manifest = convert_file(source, "livecodebench_v6",
                                    hashlib.sha256(payload).hexdigest(), root / "unified")
            self.assertEqual(manifest["records"], 1)
            row = json.loads((root / "unified/pals_dag_unified_v1.jsonl").read_text())
            self.assertEqual(row["review"]["source_status"], "calibri_derived_tested_reference")
            self.assertEqual(row["benchmark"], "livecodebench_v6")
            self.assertIn('id="cohort-data"', (root / "unified/pals_dag_unified_v1.html").read_text())

    def test_diagnostic_graph_keeps_its_original_review_status(self):
        row = convert_record(gpqa(diagnostic=True), "gpqa_diamond", SOURCE_HASH)
        self.assertEqual(row["review"]["source_status"], "model_accepted_diagnostic")
        self.assertIsNone(row["provenance"]["source_dag_sha256"])
        self.assertEqual(row["dag"]["nodes"][1]["parents"], [1])
        self.assertEqual(row["dag"]["nodes"][1]["justification"], "Follows from node 1.")

    def test_rejects_tampered_graph_or_source(self):
        bad = humaneval()
        bad["dag"]["nodes"][1]["parents"] = [3]
        bad["dag_sha256"] = digest(bad["dag"])
        with self.assertRaisesRegex(ValueError, "parents"):
            convert_record(bad, "humaneval", SOURCE_HASH)
        bad = gpqa()
        bad["model_accepted"] = False
        with self.assertRaisesRegex(ValueError, "model-accepted"):
            convert_record(bad, "gpqa_diamond", SOURCE_HASH)

    def test_real_jsonl_lf_not_unicode_line_separator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = gpqa()
            source["source"]["question"] = "Line one\u2028line two"
            source["dag"]["source"] = deepcopy(source["source"])
            payload = (json.dumps(source, ensure_ascii=False) + "\n").encode("utf-8")
            path = root / "fixture.jsonl"
            path.write_bytes(payload)
            manifest = convert_file(path, "gpqa_diamond", hashlib.sha256(payload).hexdigest(), root / "new")
            self.assertEqual(manifest["records"], 1)
            output = root / "new" / "pals_dag_unified_v1.jsonl"
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), manifest["unified_sha256"])
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").split("\n") if line]
            html = (root / "new" / "pals_dag_unified_v1.html").read_text(encoding="utf-8")
            embedded = html.split('<script id="cohort-data" type="application/json">', 1)[1].split('</script>', 1)[0]
            self.assertEqual(json.loads(embedded)["rows"], rows)
            self.assertEqual(json.loads(embedded)["schema_version"], "pals_dag_unified_view_v1")
            with self.assertRaisesRegex(ValueError, "already exists"):
                convert_file(path, "gpqa_diamond", hashlib.sha256(payload).hexdigest(), root / "new")


if __name__ == "__main__":
    unittest.main()
