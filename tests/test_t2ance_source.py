"""Synthetic source checks only; candidate programs are never executed."""

import copy
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder import t2ance_source as source


def fixture():
    code = "print(input())"
    prose = ("The output must be exactly the supplied text. Reading one line and "
             "printing it unchanged preserves the required value for every input.")
    item = {"question_id": "abc999_a", "question_title": "Identity",
            "question": "Return the supplied text.", "platform": "atcoder",
            "contest_date": "2025-01-01T00:00:00", "difficulty": "easy",
            "starter_code": "", "io_type": "stdin", "entry_point": None}
    row = {"task_id": item["question_id"], "solution_idx": 0,
           "solution_code": code, "full_response": prose + "\n```python\n" + code + "\n```",
           "passed": True, "num_passed": 4, "num_tests": 4,
           "problem": {"question_id": item["question_id"],
                       "question_title": item["question_title"],
                       "question_content": item["question"],
                       "platform": item["platform"],
                       "contest_date": item["contest_date"],
                       "difficulty": item["difficulty"],
                       "starter_code": item["starter_code"]}}
    return item, row


class T2anceSourceTests(unittest.TestCase):
    def test_cpu_canary_is_score_blind_stratified_and_frozen(self):
        groups = (("functional", "hard", 13), ("functional", "medium", 10),
                  ("stdin", "hard", 26), ("stdin", "medium", 11))
        items = [{"item_id": f"item-{io}-{difficulty}-{n}", "io_type": io,
                  "difficulty": difficulty}
                 for io, difficulty, count in groups for n in range(count)]
        chosen = source.canary_seven(items)
        self.assertEqual(len(chosen), 7)
        self.assertEqual([sum((item["io_type"], item["difficulty"]) == group
                              for item in chosen) for group in source.CANARY_SEVEN_QUOTAS],
                         [2, 1, 3, 1])
        self.assertEqual(chosen, source.canary_seven(items))
        with self.assertRaises(ValueError):
            source.canary_seven(items[:-1])

    def test_valid_passed_candidate_and_no_code_execution(self):
        item, row = fixture()
        calls = []
        result = source.inspect_candidate(row, item, calls.append)
        self.assertTrue(result["candidate"])
        self.assertEqual(calls, [row["solution_code"]])
        self.assertGreaterEqual(result["prose_chars"], 100)

    def test_pass_flag_does_not_replace_complete_test_count(self):
        item, row = fixture()
        row["num_passed"] = 3
        result = source.inspect_candidate(row, item, lambda code: None)
        self.assertFalse(result["candidate"])
        self.assertIn("upstream_tests_not_all_passed", result["reasons"])

    def test_code_only_and_unclosed_fence_fail(self):
        item, row = fixture()
        row["full_response"] = "```python\n" + row["solution_code"] + "\n```"
        self.assertIn("insufficient_noncode_text",
                      source.inspect_candidate(row, item, lambda code: None)["reasons"])
        row["full_response"] += "\n```unfinished"
        self.assertEqual(source.explanation_without_code(
            row["full_response"], row["solution_code"]), "")

    def test_exact_question_identity_required(self):
        item, row = fixture()
        for key in ("question_title", "question_content", "platform", "contest_date",
                    "difficulty", "starter_code"):
            bad = copy.deepcopy(row)
            bad["problem"][key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                source.inspect_candidate(bad, item, lambda code: None)

    def test_functional_entry_point_and_static_policy(self):
        item, row = fixture()
        item.update(io_type="functional", entry_point="solve")
        self.assertFalse(source.inspect_candidate(row, item, lambda code: None)["candidate"])
        item["io_type"], item["entry_point"] = "stdin", None
        def reject(code):
            raise ValueError("unreviewed import")
        self.assertIn("invalid_or_unsupported_program:unreviewed import",
                      source.inspect_candidate(row, item, reject)["reasons"])

    def test_pin_verification_and_no_refetch(self):
        payload = b"parquet fixture"
        name = "data/lcb/fixture.parquet"
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            source.FILES, {name: (len(payload), hashlib.sha256(payload).hexdigest())}):
            root = Path(folder).resolve()
            with patch.object(source, "urlopen", return_value=io.BytesIO(payload)) as network:
                target = source.fetch_file(root, name)
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(source.fetch_file(root, name), target)
                self.assertEqual(network.call_count, 1)
            target.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                source.verify_file(target, name)


if __name__ == "__main__":
    unittest.main()
