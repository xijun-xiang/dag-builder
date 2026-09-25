"""Synthetic cohort tests for immutable answer-backward release overlay."""

import unittest

from dag_builder.lcb_answer_backward_export import _merge
from dag_builder.storage import digest


def fixture():
    rows, accepted = [], []
    for index in range(175):
        item_id = f"item-{index}"
        status = "model_accepted" if index < 89 else "needs_review"
        row = {"item_id": item_id, "question_id": str(index), "status": status,
               "cpu_status": "passed", "source_tier": "CALIBRI",
               "revision_run": "lcb-dag-revision58-rest50-v3",
               "revision_result_sha256": digest({"old": index})}
        rows.append(row)
        if index < 89:
            accepted.append({"item_id": item_id, "question_id": str(index)})
    reviewed = {}
    for index in range(89, 100):
        item_id = f"item-{index}"
        item = {"item_id": item_id, "question_id": str(index),
                "execution_evidence": {"status": "passed", "result_sha256": "a" * 64}}
        accepted_now = index == 89
        dag = {"source": item, "formal_eligible": False} if accepted_now else None
        result = {"item_id": item_id,
                  "status": "model_accepted" if accepted_now else "needs_review",
                  "reason": "new review"}
        if dag is not None:
            result["dag_sha256"] = digest(dag)
        reviewed[item_id] = (item, result, dag, {"previous_result": {"old": index}})
    return rows, accepted, reviewed


class AnswerBackwardExportTests(unittest.TestCase):
    def test_only_new_acceptance_changes_status_and_denominator(self):
        rows, accepted, reviewed = fixture()
        flow, candidates = _merge(rows, accepted, reviewed, run_name="new-run")
        self.assertEqual(len(flow), 175)
        self.assertEqual(len(candidates), 90)
        self.assertEqual(flow[89]["status"], "model_accepted")
        self.assertEqual(flow[90]["status"], "needs_review")
        self.assertEqual(flow[90]["answer_backward_model_status"], "needs_review")
        fresh = next(record for record in candidates if record["item_id"] == "item-89")
        self.assertEqual(fresh["schema_version"],
                         "lcb-v6-dag-revision-answerbackward-candidates-v1")
        self.assertFalse(fresh["human_approved"])
        self.assertFalse(fresh["formal_eligible"])

    def test_wrong_cohort_and_prior_result_fail_closed(self):
        rows, accepted, reviewed = fixture()
        rows[89]["revision_run"] = "wrong-run"
        with self.assertRaisesRegex(ValueError, "cohort"):
            _merge(rows, accepted, reviewed, run_name="new-run")
        rows[89]["revision_run"] = "lcb-dag-revision58-rest50-v3"
        reviewed["item-89"][3]["previous_result"]["old"] = -1
        with self.assertRaisesRegex(ValueError, "source or prior"):
            _merge(rows, accepted, reviewed, run_name="new-run")


if __name__ == "__main__":
    unittest.main()
