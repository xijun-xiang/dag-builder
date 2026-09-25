"""Offline cross-subject MMLU contracts; never call a paid API."""

import hashlib
import json
import sys
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.mmlu_campaign import prepare_all, run_all
from dag_builder.mmlu_catalog import MMLU_SUBJECTS
from dag_builder.mmlu_export import export_subject, export_campaign
from dag_builder.source import normalize, select
from dag_builder.stages import THINKING_STAGES, payload, prompt, stages_for
from dag_builder.storage import digest, read_json, write_once
from dag_builder.unified import convert_record
from test_builder import DAG_REVIEW, JUSTIFICATIONS, NODES, PARENTS, REVIEW, SOLUTION


class MMLUAllTests(unittest.TestCase):
    def test_catalog_and_general_protocol_cover_57_without_physics_fallback(self):
        self.assertEqual(len(MMLU_SUBJECTS), 57)
        self.assertIn("world_religions", MMLU_SUBJECTS)
        config = Config(prompt_version="mmlu-general-thinking-v1", thinking="enabled")
        self.assertEqual(stages_for(config), THINKING_STAGES)
        for stage in THINKING_STAGES:
            self.assertTrue(prompt(stage, config.prompt_version).strip())
            self.assertNotIn("physics question", prompt(stage, config.prompt_version))
        with self.assertRaises(ValueError):
            Config(prompt_version="mmlu-general-thinking-v1")
        request = payload("solve", {"question": {"question": "Which claim follows?",
                     "choices": ("one", "two", "three", "four")}}, config)
        self.assertNotIn("response_format", request)
        self.assertNotIn("temperature", request)
        self.assertNotIn("reference_answer", request["messages"][1]["content"])

    def test_standalone_pals_reader_has_the_same_subject_set(self):
        location = Path(__file__).resolve().parents[1] / "pals-validation" / "src"
        sys.path.insert(0, str(location))
        try:
            from pals_validation.unified import MMLU_SUBSETS
            self.assertEqual(MMLU_SUBSETS, set(MMLU_SUBJECTS))
        finally:
            sys.path.remove(str(location))

    def test_full_selection_keeps_exclusions_and_does_not_replenish(self):
        items = normalize([
            {"question": "Which claim follows?", "choices": ["a", "b", "c", "d"], "answer": 0},
            {"question": "Which claim follows?", "choices": ["a", "b", "c", "d"], "answer": 0},
            {"question": "See figure below.", "choices": ["a", "b", "c", "d"], "answer": 1},
        ], "a" * 40, "formal_logic", "test")
        chosen, manifest = select(items, None, 42)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(manifest["candidate_count"], 3)
        self.assertEqual(len(manifest["excluded"]), 2)
        self.assertTrue(manifest["scope"].startswith("all source-eligible"))

    def test_campaign_prepares_every_subject_with_distinct_roots(self):
        with tempfile.TemporaryDirectory() as temp, patch(
                "dag_builder.mmlu_campaign.prepare") as prepared:
            prepared.side_effect = lambda root, revision, subset, split, count, seed, dataset, source: {
                "candidate_count": 2, "eligible_count": 2,
                "selected_count": 2 if count is None else count,
                "selected_ids": [subset], "excluded": []}
            result = prepare_all(Path(temp).resolve() / "campaign", "a" * 40)
            self.assertEqual(result["subject_count"], 57)
            self.assertEqual(result["selected_count"], 114)
            self.assertEqual(prepared.call_count, 57)
            self.assertEqual({call.args[2] for call in prepared.call_args_list}, set(MMLU_SUBJECTS))

    def test_offline_parquet_campaign_prepares_all_57_without_network(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("optional source dependency pyarrow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            source = base / "parquets"
            for subset in MMLU_SUBJECTS:
                directory = source / subset
                directory.mkdir(parents=True)
                pq.write_table(pa.Table.from_pylist([{
                    "question": f"Which option is correct in {subset}?",
                    "choices": ["first", "second", "third", "fourth"],
                    "answer": 0, "subject": subset,
                }]), directory / "test-00000-of-00001.parquet")
            with patch("dag_builder.source.urlopen", side_effect=AssertionError("network used")):
                result = prepare_all(base / "campaign", "a" * 40,
                                     count_per_subject=1, source_dir=source)
            self.assertEqual(result["selected_count"], 57)
            self.assertEqual(len({read_json(base / "campaign" / "subjects" / subject / "items.json")[0]["item_id"]
                                  for subject in MMLU_SUBJECTS}), 57)

    def test_full_export_retains_57_rejections_without_fabricating_eligible_graphs(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("optional source dependency pyarrow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            sources = base / "sources"
            for subset in MMLU_SUBJECTS:
                directory = sources / subset
                directory.mkdir(parents=True)
                pq.write_table(pa.Table.from_pylist([{
                    "question": f"Which option is correct in {subset}?",
                    "choices": ["first", "second", "third", "fourth"],
                    "answer": 0, "subject": subset,
                }]), directory / "test-00000-of-00001.parquet")
            campaign = base / "campaign"
            prepare_all(campaign, "a" * 40, count_per_subject=1, source_dir=sources)
            for subset in MMLU_SUBJECTS:
                root = campaign / "subjects" / subset
                item = read_json(root / "items.json")[0]
                selection = read_json(root / "selection.json")
                write_once(root / "run_config.json", {
                    "task_type": "mmlu", "prompt_version": "mmlu-general-thinking-v1",
                    "model": "fixture-model"})
                write_once(root / "implementation.json", {"code_sha256": "a" * 64})
                write_once(root / "inputs_manifest.json", {
                    "items_sha256": digest([item]), "selection_sha256": digest(selection)})
                write_once(root / "items" / item["item_id"] / "result.json", {
                    "item_id": item["item_id"], "status": "rejected", "stage": "solve",
                    "reason": "fixture_answer_mismatch"})
            result = export_campaign(campaign, base / "export")
            self.assertEqual(result["subject_count"], 57)
            self.assertEqual(result["candidate_count"], 57)
            self.assertEqual(result["model_accepted"], 0)
            self.assertEqual(result["pals_structural_eligible"], 0)
            self.assertIsNone(result["unified"])
            self.assertEqual(len((base / "export/all_outcomes.jsonl").read_text().splitlines()), 57)

    def test_paid_campaign_checks_protocol_and_worst_case_budget_before_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "campaign"
            root.mkdir(mode=0o700)
            write_once(root / "campaign_manifest.json", {
                "protocol": "mmlu-57-source-selection-v1",
                "subjects": {subject: {"selected_ids": [subject]}
                             for subject in MMLU_SUBJECTS}})
            for subject in ("econometrics", "formal_logic"):
                write_once(root / "subjects" / subject / "selection.json",
                           {"selected_ids": [subject]})
            config = Config(prompt_version="mmlu-general-thinking-v1", thinking="enabled",
                            max_calls=60, max_reserved_tokens=3000000)
            with patch("dag_builder.pipeline.Pipeline") as runner:
                with self.assertRaisesRegex(ValueError, "ceilings"):
                    run_all(root, config, object(), ["econometrics", "formal_logic"],
                            119, 6000000)
                runner.assert_not_called()
                runner.return_value.run.return_value = {
                    "paused": False, "attempt_count": 0, "reserved_tokens": 0,
                    "results": [{"status": "model_accepted"}]}
                result = run_all(root, config, object(), ["econometrics", "formal_logic"],
                                 120, 6000000, limit_per_subject=1)
                self.assertFalse(result["paused"])
                self.assertEqual(runner.call_count, 2)

    def test_mmlu_export_is_source_bound_and_keeps_all_outcomes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "input"
            raw = b"fixture-parquet-bytes"
            source_dir = root / "source"
            source_dir.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            (source_dir / "original.parquet").write_bytes(raw)
            write_once(source_dir / "provenance.json", {
                "dataset": "cais/mmlu", "config": "business_ethics", "split": "test",
                "revision": "a" * 40, "sha256": hashlib.sha256(raw).hexdigest()})
            source_items = normalize([
                {"question": "A premise. Which option follows?", "choices": ["one", "two", "three", "four"], "answer": 0},
                {"question": "Another question?", "choices": ["one", "two", "three", "four"], "answer": 1},
                {"question": "A recall question?", "choices": ["one", "two", "three", "four"], "answer": 0},
                {"question": "A held-out source item?", "choices": ["one", "two", "three", "four"], "answer": 0},
            ], "a" * 40, "business_ethics", "test")
            items = source_items[:3]
            write_once(root / "items.json", items)
            write_once(source_dir / "normalized.json", source_items)
            write_once(root / "selection.json", {
                "selected_ids": [i["item_id"] for i in items],
                "candidate_count": len(source_items), "eligible_count": len(source_items),
                "selected_count": len(items),
                "excluded": []})
            selection = read_json(root / "selection.json")
            write_once(root / "run_config.json", {
                "task_type": "mmlu", "prompt_version": "mmlu-general-thinking-v1",
                "model": "fixture-model"})
            write_once(root / "implementation.json", {"code_sha256": "a" * 64})
            write_once(root / "inputs_manifest.json", {
                "items_sha256": digest(items), "selection_sha256": digest(selection)})
            nodes = [{**node, "parents": PARENTS["parents"][i]["parents"],
                      "justification": JUSTIFICATIONS["justifications"][i]["text"]}
                     for i, node in enumerate(NODES)]
            dag = {"item_id": items[0]["item_id"], "source": items[0],
                   "reference_solution": SOLUTION, "nodes": nodes,
                   "solution_review": REVIEW, "dag_review": DAG_REVIEW,
                   "quality_status": "model_reviewed_pending_human_review",
                   "construction_protocol": "mmlu-general-thinking-v1"}
            first = root / "items" / items[0]["item_id"]
            second = root / "items" / items[1]["item_id"]
            third = root / "items" / items[2]["item_id"]
            write_once(first / "dag.json", dag)
            write_once(first / "result.json", {"item_id": items[0]["item_id"],
                       "status": "model_accepted", "stage": "review_dag",
                       "dag_sha256": digest(dag)})
            write_once(second / "result.json", {"item_id": items[1]["item_id"],
                       "status": "rejected", "stage": "solve", "reason": "answer_label_mismatch"})
            short_dag = deepcopy(dag)
            short_dag["source"] = items[2]
            short_dag["item_id"] = items[2]["item_id"]
            short_dag["nodes"] = [deepcopy(nodes[0]), deepcopy(nodes[-1])]
            short_dag["nodes"][-1]["node_id"] = 2
            short_dag["nodes"][-1]["parents"] = [1]
            write_once(third / "dag.json", short_dag)
            write_once(third / "result.json", {"item_id": items[2]["item_id"],
                       "status": "model_accepted", "stage": "review_dag",
                       "dag_sha256": digest(short_dag)})
            candidate = {"schema_version": "mmlu_model_candidates_v1", "item_id": items[0]["item_id"],
                         "source": items[0], "dag": dag, "dag_sha256": digest(dag),
                         "status": "model_accepted", "model_accepted": True, "human_approved": False}
            unified = convert_record(candidate, "mmlu", "b" * 64)
            self.assertEqual(unified["problem"]["domain"], "business_ethics")
            self.assertEqual(unified["review"]["human_approved"], False)
            export = export_subject(root, base / "export")
            self.assertEqual(export["selected"], 3)
            self.assertEqual(export["candidate_count"], 4)
            self.assertEqual(export["not_selected"], 1)
            self.assertEqual(export["model_accepted"], 2)
            self.assertEqual(export["pals_structural_eligible"], 1)
            self.assertEqual(export["status_counts"], {
                "model_accepted": 2, "rejected": 1, "not_selected": 1})
            self.assertEqual(export["unified"]["records"], 1)
            self.assertEqual(len((base / "export/all_outcomes.jsonl").read_text().splitlines()), 4)
            self.assertEqual(len((base / "export/model_accepted.jsonl").read_text().splitlines()), 2)
            self.assertEqual(len((base / "export/pals_eligible_candidates.jsonl").read_text().splitlines()), 1)
            location = Path(__file__).resolve().parents[1] / "pals-validation" / "src"
            sys.path.insert(0, str(location))
            try:
                from pals_validation.data import normalize as normalize_pals
                record = json.loads((base / "export/unified/pals_dag_unified_v1.jsonl")
                                    .read_text().splitlines()[0])
                case = normalize_pals(record, "mmlu")
                self.assertEqual(case["subset"], "business_ethics")
                self.assertEqual(len(case["steps"]), 4)
            finally:
                sys.path.remove(str(location))
            self.assertEqual(read_json(base / "export/manifest.json"), export)


if __name__ == "__main__":
    unittest.main()
