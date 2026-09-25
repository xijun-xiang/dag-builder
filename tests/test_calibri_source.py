"""Synthetic CALIBRI fixtures. Never download data or execute candidate code."""

import copy
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder import calibri_source as source
from dag_builder.storage import digest, read_json, write_once


def fixture():
    code = "print(input())"
    item = {"question_id": "abc999_a", "question_title": "Identity", "question": "Return the input.",
            "io_type": "stdin", "entry_point": None}
    prose = "The task asks for the supplied text unchanged. Reading the text and returning the same value meets this identity specification."
    row = {"id": item["question_id"], "name": item["question_title"], "prompt": item["question"],
           "program": [code] * 10, "output": [prose + "\n```python\n" + code + "\n```"] * 10,
           "is_correct": [True] * 10}
    return item, row


class CALIBRISourceTests(unittest.TestCase):
    def test_full_selection_preserves_pilot_and_accounts_for_every_item(self):
        item, row = fixture()
        originals = [{**item, "item_id": f"{n:020x}", "question_id": f"q{n}"} for n in range(175)]
        frozen = originals[:5]
        selection = {"seed": 20260922, "selected_ids": [i["item_id"] for i in frozen]}
        rows = [{**row, "id": f"q{n}", "model": "synthetic",
                 "is_correct": [n != 2] * 10} for n in range(1055)]

        def fake_rows(cache, name, columns):
            return rows if "/train-" in name else []

        with tempfile.TemporaryDirectory() as folder, patch.object(source, "read_rows", side_effect=fake_rows):
            root = Path(folder).resolve()
            write_once(root / "original/source/normalized.json", originals)
            write_once(root / "original/items.json", frozen)
            write_once(root / "original/selection.json", selection)
            write_once(root / "original/prepared-manifest.json", {
                "items_sha256": digest(frozen), "selection_sha256": digest(selection)})
            source.prepare(root / "cache", root / "original", root / "pilot")
            audit = source.prepare(root / "cache", root / "original", root / "full", full=True)
            self.assertEqual(audit["selected_candidate_count"], 174)
            self.assertEqual(len(audit["excluded_full_cohort"]), 1)
            full = {i["item_id"]: i for i in read_json(root / "full/items.json")}
            for chosen in read_json(root / "pilot/items.json"):
                self.assertEqual(chosen, full[chosen["item_id"]])
            self.assertEqual(len(list((root / "full/source-rows").glob("*/*.json"))), 350)

    def test_identity_and_aligned_labels(self):
        item, row = fixture()
        self.assertTrue(all(x["candidate"] for x in source.inspect_row(row, item)))
        for key, bad_value in (("id", "other"), ("name", "wrong"), ("prompt", "different"),
                               ("program", ["x"]), ("is_correct", [1] * 10)):
            bad = copy.deepcopy(row)
            bad[key] = bad_value
            with self.subTest(key=key), self.assertRaises(ValueError):
                source.inspect_row(bad, item)

    def test_test_pass_is_not_enough_for_reasoning_candidate(self):
        item, row = fixture()
        row["output"][0] = "```python\nprint(input())\n```"
        row["program"][1] = ""
        row["is_correct"][2] = False
        row["output"][3] = ""
        row["program"][4] = "print('unrelated')"
        row["program"][5] = "def broken("
        rows = source.inspect_row(row, item)
        self.assertFalse(any(x["candidate"] for x in rows[:6]))
        self.assertTrue(all(x["candidate"] for x in rows[6:]))
        self.assertIn("program_not_verbatim_in_output", rows[4]["reasons"])

    def test_functional_entry_point_checked_without_execution(self):
        item, row = fixture()
        item.update(io_type="functional", entry_point="solve")
        self.assertFalse(any(x["candidate"] for x in source.inspect_row(row, item)))

    def test_unclosed_fence_not_repaired(self):
        self.assertEqual(source.prose_without_fences("hello\n```python\n" + "x" * 200), "")
        self.assertEqual(source.prose_without_fences(None), "")
        self.assertEqual(source.prose_without_fences("a\n```python\nx\n```\nb"), "a\n\nb")

    def test_inline_channel_fence_keeps_actual_reasoning(self):
        item, row = fixture()
        row["output"][0] = row["output"][0].replace("\n```python", "assistantfinal code```python")
        self.assertTrue(source.inspect_row(row, item)[0]["candidate"])

    def test_verified_download_and_no_refetch(self):
        payload = b"synthetic public bytes"
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(source.FILES, {"fixture/data.parquet": (len(payload), expected)}):
            root = Path(tmp).resolve()
            with patch.object(source, "urlopen", return_value=io.BytesIO(payload)) as network:
                target = source.fetch_file(root, "fixture/data.parquet")
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(source.fetch_file(root, "fixture/data.parquet"), target)
                self.assertEqual(network.call_count, 1)
            target.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                source.fetch_file(root, "fixture/data.parquet")

    def test_bad_download_is_not_promoted(self):
        payload = b"good"
        expected = hashlib.sha256(payload).hexdigest()
        for raw in (b"bad!", b"too-long", b"x"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                with patch.dict(source.FILES, {"fixture/data.parquet": (4, expected)}), \
                     patch.object(source, "urlopen", return_value=io.BytesIO(raw)), self.assertRaises(ValueError):
                    source.fetch_file(root, "fixture/data.parquet")
                self.assertFalse((root / "fixture/data.parquet").exists())
                self.assertEqual(list((root / "fixture").iterdir()), [])

    def test_unknown_file_or_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(ValueError):
                source.fetch_file(root, "../../unapproved")
            target = root / "target"
            target.write_bytes(b"x")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                source.verify_file(link, 1, hashlib.sha256(b"x").hexdigest())


if __name__ == "__main__":
    unittest.main()
