"""The E2 quality gate filters positions without changing frozen anchors."""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/qualify_coworker_e2.py"
SPEC = importlib.util.spec_from_file_location("qualify_coworker_e2", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def case_and_anchor():
    steps = [
        {"node_id": 1, "kind": "given", "source_field": "question",
         "statement": "The quantity is five.", "parents": []},
        {"node_id": 2, "kind": "derived", "source_field": "solution",
         "statement": "Twice five is ten.", "parents": [1]},
        {"node_id": 3, "kind": "derived", "source_field": "solution",
         "statement": "Adding one to ten yields eleven.", "parents": [2]},
        {"node_id": 4, "kind": "derived", "source_field": "solution",
         "statement": "Eleven doubled is twenty-two.", "parents": [3]},
    ]
    case = {"question": "How many after multiplying and adding?", "steps": steps}
    anchor = {"target_id": 3, "deleted_id": 2, "prefix_ids": [1, 2]}
    return case, anchor


class E2QualityTests(unittest.TestCase):
    def test_internal_solution_step_passes_unchanged_anchor(self):
        case, anchor = case_and_anchor()
        snapshot = repr((case, anchor))
        evidence, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertEqual(reasons, [])
        self.assertEqual(evidence["prefix_ids"], [1, 2])
        self.assertEqual(repr((case, anchor)), snapshot)

    def test_question_parent_and_terminal_target_are_not_primary(self):
        case, anchor = case_and_anchor()
        case["steps"][1]["kind"] = "given"
        case["steps"][1]["source_field"] = "question"
        _, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertIn("deleted_parent_not_solution_reasoning_or_knowledge", reasons)
        case, anchor = case_and_anchor()
        anchor["target_id"] = 4
        anchor["deleted_id"] = 3
        _, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertIn("reference_target_has_no_nonanswer_child", reasons)

    def test_literal_repeat_and_option_conclusion_are_detected(self):
        case, anchor = case_and_anchor()
        case["steps"][2]["statement"] = case["steps"][1]["statement"]
        _, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertIn("target_literal_parent_repeat", reasons)
        case["steps"][2]["statement"] = "The correct option is B."
        _, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertIn("target_explicit_option_conclusion", reasons)
        case["steps"][2]["statement"] = "The observed effect is a rising score in the final period."
        case["choices"] = ["a rising score in the final period", "falling score", "none", "all"]
        _, reasons = MODULE.anchor_reasons(case, anchor)
        self.assertIn("target_overlaps_long_choice", reasons)

    def test_prompt_fingerprint_ignores_trailing_period_not_math_operators(self):
        base = {"problem": {"question": "The question?", "choices": [
            "Adler's theory.", "x + 4", "x - 4", "other",
        ]}}
        variant = {"problem": {"question": " the question ", "choices": [
            "Adler's theory", "x + 4", "x - 4", "other",
        ]}}
        self.assertEqual(MODULE.prompt_fingerprint(base),
                         MODULE.prompt_fingerprint(variant))
        variant["problem"]["choices"][2] = "x + 4"
        self.assertNotEqual(MODULE.prompt_fingerprint(base),
                            MODULE.prompt_fingerprint(variant))

    def test_jsonl_is_one_object_per_line(self):
        rows = [{"a": 1}, {"b": "β"}]
        self.assertEqual(MODULE.read_jsonl(MODULE.encode_jsonl(rows)), rows)
        with self.assertRaisesRegex(ValueError, "newline-terminated"):
            MODULE.read_jsonl(b'{"a":1}')

    def test_review_requires_all_and_only_strict_psych_questions(self):
        assessments = {"one": {"reasons": []}, "two": {"reasons": ["old"]}}
        by_item = {"one": {"source_id": "psych:1", "package": "mmlu_psych_social"},
                   "two": {"source_id": "psych:2", "package": "mmlu_psych_social"}}
        review = {"protocol": "assistant_score_blind_psych_e2_review_v1",
                  "items": {"psych:1": {"decision": "uncertain", "reason": "Thin restatement"}}}
        counts = MODULE.apply_score_blind_review(assessments, by_item, review)
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(assessments["two"]["reasons"], ["old"])
        self.assertIn("assistant_blind_review_uncertain", assessments["one"]["reasons"][0])
        review["items"]["psych:2"] = {"decision": "retain", "reason": "Valid"}
        with self.assertRaisesRegex(ValueError, "exactly"):
            MODULE.apply_score_blind_review(assessments, by_item, review)


if __name__ == "__main__":
    unittest.main()
