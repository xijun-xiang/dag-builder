"""Synthetic lifecycle evidence: reporting must not change acceptance or retry."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.report import overview
from dag_builder.run_status import record_pause
from dag_builder.storage import read_json, write_once


class RunStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.item_id = "a" * 20
        self.directory = self.root / "items" / self.item_id
        write_once(self.root / "items.json", [{"item_id": self.item_id}])
        self.paused = {
            "item_id": self.item_id,
            "status": "paused",
            "stage": "repair",
            "reason": "transient_retries_exhausted",
        }
        self.before = "2026-01-01T00:00:00+00:00"
        self.ended = "2026-01-01T00:01:00+00:00"
        self.after = "2026-01-01T00:02:00+00:00"

    def request(self, index, stamp):
        path = self.directory / "repair" / f"attempt-{index:02d}" / "request.json"
        write_once(path, {"started_at": stamp})

    def row(self):
        return overview(self.root)["items"][0]

    def legacy(self, name="old", result=None, stamp=None):
        write_once(
            self.root / "invocations" / (name + ".json"),
            {"ended_at": stamp or self.ended, "results": [result or self.paused]},
        )

    def test_new_pause_is_visible_without_terminal_verdict(self):
        self.request(0, self.before)
        record_pause(self.root, self.paused, self.ended)
        self.assertEqual(self.row()["status"], "paused")
        self.assertEqual(self.row()["reason"], "transient_retries_exhausted")
        self.assertFalse((self.directory / "result.json").exists())

    def test_new_request_invalidates_pause_and_does_not_resurrect_legacy(self):
        self.request(0, self.before)
        self.legacy()
        record_pause(self.root, self.paused, self.ended)
        self.request(1, self.after)
        self.assertEqual(self.row()["status"], "incomplete")

    def test_new_response_invalidates_pause_even_without_new_request(self):
        self.request(0, self.before)
        record_pause(self.root, self.paused, self.ended)
        write_once(
            self.directory / "repair/attempt-00/response.json",
            {"ended_at": self.after, "body": {}},
        )
        self.assertEqual(self.row()["status"], "incomplete")

    def test_terminal_verdict_wins_without_changing_pause_record(self):
        record_pause(self.root, self.paused, self.ended)
        events = list(self.root.glob("status_events/*/*.json"))
        before = read_json(events[0])
        write_once(
            self.directory / "result.json",
            {"item_id": self.item_id, "status": "repair_rejected"},
        )
        self.assertEqual(self.row()["status"], "repair_rejected")
        self.assertEqual(read_json(events[0]), before)

    def test_latest_pause_event_wins(self):
        self.request(0, self.before)
        record_pause(self.root, self.paused, self.ended)
        self.request(1, self.after)
        newer = dict(self.paused, reason="budget_exhausted")
        record_pause(self.root, newer, "2026-01-01T00:03:00+00:00")
        self.assertEqual(self.row()["reason"], "budget_exhausted")

    def test_legacy_completed_invocation_reports_pause_read_only(self):
        self.request(0, self.before)
        self.legacy()
        self.assertEqual(self.row()["status"], "paused")
        self.assertEqual(self.row()["status_evidence"], "completed_invocation")
        self.assertFalse((self.root / "status_events").exists())
        self.assertFalse((self.directory / "result.json").exists())

    def test_legacy_new_attempt_or_missing_timestamp_is_inconclusive(self):
        self.request(0, self.before)
        self.legacy()
        self.request(1, self.after)
        self.assertEqual(self.row()["status"], "incomplete")

    def test_legacy_unknown_attempt_date_does_not_claim_paused(self):
        self.request(0, None)
        self.legacy()
        self.assertEqual(self.row()["status"], "incomplete")

    def test_latest_legacy_nonpause_invalidates_older_pause(self):
        self.request(0, self.before)
        self.legacy()
        self.legacy("new", dict(self.paused, status="staged"), self.after)
        self.assertEqual(self.row()["status"], "incomplete")

    def test_nonpause_does_not_create_event(self):
        record_pause(self.root, dict(self.paused, status="staged"), self.ended)
        self.assertFalse((self.root / "status_events").exists())
        self.assertEqual(self.row()["status"], "not_started")
