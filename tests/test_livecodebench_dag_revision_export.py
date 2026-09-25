"""Synthetic full-denominator tests; no benchmark text or credentials."""

import unittest

from dag_builder.livecodebench_dag_revision_export import _merge
from dag_builder.storage import digest


def fixture():
    rows = []
    accepted = []
    canary, rest = {}, {}
    for number in range(175):
        item_id = f"item-{number}"
        if number < 70:
            status = "model_accepted"
            accepted.append({"item_id": item_id, "question_id": str(number)})
        elif number < 128:
            status = "needs_review"
        else:
            status = "no_qualified_source"
        row = {"item_id": item_id, "question_id": str(number),
               "source_tier": "CALIBRI" if 70 <= number < 128 else "none",
               "cpu_status": "passed" if 70 <= number < 128 else "not_submitted",
               "status": status, "reason": "prior"}
        rows.append(row)
        if status != "needs_review":
            continue
        item = {"item_id": item_id, "question_id": str(number),
                "reference_origin": "calibri_model_output",
                "execution_evidence": {"status": "passed", "result_sha256": "a" * 64}}
        result = {"item_id": item_id,
                  "status": "model_accepted" if number == 70 else "needs_review",
                  "reason": "review result"}
        dag = {"source": item, "formal_eligible": False} if number == 70 else None
        origin = {"source_item_sha256": digest(item), "cpu_result_sha256": "a" * 64}
        (canary if number < 78 else rest)[item_id] = (item, result, dag, origin)
    return rows, accepted, canary, rest


class LCBDAGRevisionExportTests(unittest.TestCase):
    def test_exact_8_plus_50_coverage_and_no_status_zero_filling(self):
        rows, accepted, canary, rest = fixture()
        merged, candidates = _merge(rows, accepted, canary, rest,
                                    canary_name="canary", rest_name="rest")
        self.assertEqual(len(merged), 175)
        self.assertEqual(len(candidates), 71)
        self.assertEqual(merged[70]["prior_status"], "needs_review")
        self.assertEqual(merged[70]["status"], "model_accepted")
        self.assertTrue(merged[70]["revision_development_cohort"])
        self.assertEqual(merged[78]["status"], "needs_review")
        self.assertFalse(merged[78]["revision_development_cohort"])
        self.assertEqual(merged[128]["status"], "no_qualified_source")
        self.assertNotIn("revision_run", merged[128])

    def test_missing_or_overlapping_revision_row_fails_closed(self):
        rows, accepted, canary, rest = fixture()
        removed = rest.pop("item-78")
        with self.assertRaisesRegex(ValueError, "8\\+50 revision cohort"):
            _merge(rows, accepted, canary, rest,
                   canary_name="canary", rest_name="rest")
        rest["item-78"] = removed
        rest["item-70"] = canary["item-70"]
        with self.assertRaisesRegex(ValueError, "8\\+50 revision cohort"):
            _merge(rows, accepted, canary, rest,
                   canary_name="canary", rest_name="rest")


if __name__ == "__main__":
    unittest.main()
