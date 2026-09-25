import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.livecodebench_source import (
    REVISION, SOURCE_SHA256, normalize_livecodebench, prepare_livecodebench, select_canary,
)
from dag_builder.storage import digest


def fixture(index=0, io_type="functional", difficulty="easy"):
    return {"question_id": str(index), "question_title": "Synthetic identity",
            "question_content": "Return the input integer.", "platform": "leetcode",
            "contest_id": "fixture", "contest_date": "2025-02-01T00:00:00",
            "difficulty": difficulty, "starter_code": "class Solution: pass" if io_type == "functional" else "",
            "public_test_cases": json.dumps([{"input": "7", "output": "7", "testtype": io_type}]),
            "private_test_cases": "OPAQUE_PRIVATE_SENTINEL",
            "metadata": json.dumps({"func_name": "identity"} if io_type == "functional" else {})}


class LiveCodeBenchSourceTests(unittest.TestCase):
    def test_private_tests_are_separate_and_never_decoded(self):
        with patch("pickle.loads", side_effect=AssertionError("must not unpickle")):
            items, tests = normalize_livecodebench([fixture()], REVISION)
        self.assertNotIn("OPAQUE_PRIVATE", json.dumps(items))
        self.assertNotIn("private_test_cases", items[0])
        self.assertEqual(items[0]["tests_sha256"], digest(tests[items[0]["item_id"]]))
        self.assertEqual(items[0]["reference_execution"], "not_executed")

    def test_io_types(self):
        items, _ = normalize_livecodebench([fixture(0), fixture(1, "stdin")], REVISION)
        self.assertEqual([i["io_type"] for i in items], ["functional", "stdin"])

    def test_duplicate_rejected_but_cross_platform_ids_distinct(self):
        with self.assertRaises(ValueError):
            normalize_livecodebench([fixture(), fixture()], REVISION)
        other = dict(fixture(), platform="codeforces")
        items, _ = normalize_livecodebench([fixture(), other], REVISION)
        self.assertNotEqual(items[0]["item_id"], items[1]["item_id"])

    def test_bad_source_fields_fail_closed(self):
        for change in ({"metadata": '{"func_name":"not valid"}'}, {"difficulty": "unknown"},
                       {"public_test_cases": "[]"}, {"contest_date": "unknown"},
                       {"starter_code": None}, {"platform": "unknown"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_livecodebench([dict(fixture(), **change)], REVISION)

    def test_mismatch_type_and_mutable_revision_rejected(self):
        bad = dict(fixture(), metadata="{}")
        with self.assertRaises(ValueError):
            normalize_livecodebench([bad], REVISION)
        with self.assertRaises(ValueError):
            normalize_livecodebench([fixture()], "main")

    def test_stratification_and_order_invariance(self):
        rows = [fixture(i, ("functional", "stdin")[i % 2], ("easy", "medium", "hard")[i % 3])
                for i in range(30)]
        items, _ = normalize_livecodebench(rows, REVISION)
        a = select_canary(items, 5, 20260922)
        b = select_canary(list(reversed(copy.deepcopy(items))), 5, 20260922)
        self.assertEqual(a, b)
        self.assertEqual({i["io_type"] for i in a}, {"functional", "stdin"})
        self.assertEqual({i["difficulty"] for i in a}, {"easy", "medium", "hard"})
        self.assertEqual(len({i["item_id"] for i in select_canary(items, 30, 1)}), 30)

    def test_pinned_file_hash_required_before_any_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.jsonl"
            path.write_text(json.dumps(fixture()) + "\n")
            root = Path(tmp) / "out"
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                prepare_livecodebench(root, path, REVISION, SOURCE_SHA256)
            self.assertFalse(root.exists())
            with self.assertRaisesRegex(ValueError, "pinned"):
                prepare_livecodebench(root, path, REVISION, "a" * 64)


if __name__ == "__main__":
    unittest.main()
