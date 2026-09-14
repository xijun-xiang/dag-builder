"""Offline all-outcome/export and first-pass recovery tests."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.campaign_continuation import continue_construction
from dag_builder.campaign_export import export_campaign, outcome, record, render_html
from dag_builder.config import Config
from dag_builder.pipeline import Pipeline
from dag_builder.revision_source import seed_candidate
from dag_builder.storage import read_json, run_lock, write_once


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "private"

    def prepare_continuation_fixture(self):
        config = Config(task_type="gpqa", prompt_version="gpqa-reference-v1", workers=4)
        write_once(self.root / "construction/config.json", config.to_dict())
        write_once(
            self.root / "revision-config.json",
            Config(
                task_type="gpqa", prompt_version="gpqa-revision-v1", workers=4
            ).to_dict(),
        )
        for name in (
            "campaign.json",
            "items.json",
            "pilot_history.json",
            "construction/items.json",
            "construction/selection.json",
        ):
            write_once(self.root / name, {})
        write_once(
            self.root / "construction/items/a/review_solution/attempt-00/request.json",
            {"reserved_tokens": 123, "payload": {}},
        )
        write_once(
            self.root / "construction/items/b/result.json", {"status": "rejected"}
        )
        return config

    def test_gpqa_32_workers_are_explicitly_bounded(self):
        self.assertEqual(
            Config(
                task_type="gpqa", prompt_version="gpqa-reference-v1", workers=32
            ).workers,
            32,
        )
        with self.assertRaises(ValueError):
            Config(task_type="gpqa", prompt_version="gpqa-reference-v1", workers=33)
        with self.assertRaises(ValueError):
            Config(workers=32)

    def test_campaign_continuation_inherits_unknowns_and_budget(self):
        config = self.prepare_continuation_fixture()
        destination = self.root.parent / "continuation"
        manifest = continue_construction(self.root, destination)
        new_config = Config.load(destination / "construction/config.json")
        self.assertEqual(new_config.workers, 32)
        self.assertEqual(new_config.max_calls, config.max_calls)
        self.assertEqual(manifest["imported_attempts"], 1)
        self.assertEqual(len(manifest["unknown_requests"]), 1)
        runner = Pipeline(destination / "construction", new_config, object())
        runner._restore_budget()
        self.assertEqual((runner.calls, runner.reserved_tokens), (1, 123))
        self.assertEqual(
            read_json(destination / "construction/items/b/result.json")["status"],
            "rejected",
        )
        self.assertEqual(Config.load(self.root / "construction/config.json").workers, 4)

    def test_campaign_continuation_refuses_running_or_late_phase(self):
        self.prepare_continuation_fixture()
        destination = self.root.parent / "continuation"
        with run_lock(self.root), self.assertRaises(BlockingIOError):
            continue_construction(self.root, destination)
        write_once(self.root / "revision/items.json", [])
        with self.assertRaises(ValueError):
            continue_construction(self.root, destination)
        self.assertFalse(destination.exists())

    def test_first_pass_partial_candidate_is_preserved(self):
        write_once(self.root / "atomize/output.json", {"nodes": [{"node_id": 1}]})
        candidate, _ = seed_candidate(self.root)
        self.assertEqual(
            candidate, {"nodes": [{"node_id": 1}], "parents": [], "justifications": []}
        )

    def test_first_pass_edges_and_justifications_are_preserved(self):
        write_once(self.root / "atomize/output.json", {"nodes": [{"node_id": 1}]})
        write_once(
            self.root / "dependencies/output.json",
            {"parents": [{"node_id": 1, "parents": []}]},
        )
        write_once(
            self.root / "justify/output.json",
            {"justifications": [{"node_id": 1, "text": "Given"}]},
        )
        candidate, _ = seed_candidate(self.root)
        self.assertEqual(candidate["parents"][0]["parents"], [])
        self.assertEqual(candidate["justifications"][0]["text"], "Given")

    def test_failed_latest_outcome_is_not_hidden_by_old_acceptance(self):
        item = {"item_id": "a"}
        first = {
            "result": {"status": "model_accepted"},
            "dag": {"nodes": []},
            "candidate": None,
        }
        last = {
            "result": {"status": "revision_source_disputed", "reason": "Disputed"},
            "dag": None,
            "candidate": None,
        }
        row = record(item, [first, last], {}, {"a"})
        self.assertFalse(row["model_accepted"])
        self.assertIsNone(row["dag"])
        self.assertEqual(len(row["history"]), 2)

    def test_entry_review_is_included_not_dropped(self):
        row = record({"item_id": "a"}, [], {"a": "possible missing image"}, set())
        self.assertEqual(row["status"], "source_entry_review")
        self.assertFalse(row["human_approved"])

    def test_export_escapes_untrusted_script_text(self):
        html = render_html([{"source": "</script><script>alert(1)</script>"}], {})
        self.assertNotIn("</script><script>alert(1)", html)
        self.assertIn("\\u003c/script>", html)

    def test_paused_phase_is_visible(self):
        write_once(
            self.root / "phase-result.json",
            {"results": [{"item_id": "a", "status": "paused", "reason": "transport"}]},
        )
        event = outcome(self.root, "a")
        self.assertEqual(event["result"]["status"], "paused")
        self.assertIsNone(outcome(self.root, "b"))

    def test_full_export_keeps_all_198_rows_and_empty_accepted_file(self):
        items = [
            {
                "item_id": f"{i:020x}",
                "question": "Synthetic",
                "choices": ["a", "b", "c", "d"],
                "official_explanation": "Synthetic explanation",
                "gold_answer": "A",
                "domain": "Physics",
            }
            for i in range(198)
        ]
        write_once(self.root / "items.json", items)
        write_once(self.root / "pilot_history.json", {})
        write_once(
            self.root / "campaign.json",
            {
                "source_entry_reviews": {
                    items[0]["item_id"]: "possible missing visual"
                },
                "pilot_ids": [],
            },
        )
        path, summary = export_campaign(self.root, "test")
        self.assertEqual(summary["total"], 198)
        self.assertEqual(summary["counts"], {"source_entry_review": 1, "pending": 197})
        self.assertEqual(
            len((Path(path) / "all_results.jsonl").read_text().splitlines()), 198
        )
        self.assertEqual((Path(path) / "model_accepted.jsonl").read_text(), "")
        self.assertFalse(summary["complete"])
