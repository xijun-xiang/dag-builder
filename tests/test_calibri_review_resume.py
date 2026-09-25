"""Resume only empty truncated audits, without regenerating the graph."""

import tempfile
import unittest
from pathlib import Path

from dag_builder.calibri_repair import CALIBRIRepairPipeline, CHECKED_PROTOCOL
from dag_builder.calibri_review_resume import CALIBRIReviewResume, audit_completed, candidate_input, prepare, verify
from dag_builder.config import Config
from dag_builder.stages import prompt
from dag_builder.storage import read_json, write_once
from test_calibri_pipeline import config
from test_calibri_repair import Client
from test_calibri_repair_plan import prepare_revision


class EmptyLengthClient(Client):
    def complete(self, request):
        result = super().complete(request)
        if request["messages"][0]["content"] == prompt("review_dag", CHECKED_PROTOCOL, "livecodebench"):
            return {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "unfinished"}}]}
        return result


def exhausted_review(folder):
    _, _, parent, outputs = prepare_revision(folder)
    client = EmptyLengthClient(outputs, CHECKED_PROTOCOL)
    result = CALIBRIRepairPipeline(parent, config(prompt_version=CHECKED_PROTOCOL, max_calls=6, max_tokens=32768), client).run()
    assert result["results"][0]["stage"] == "review_dag"
    assert result["results"][0]["status"] == "needs_review"
    write_once(parent / "completion.json", {"status": "processed"})
    root = folder / "review-resume"
    prepare(parent, root)
    return parent, root, outputs


class ReviewResumeTests(unittest.TestCase):
    def test_one_call_same_graph_budget_and_idempotence(self):
        with tempfile.TemporaryDirectory() as name:
            parent, root, outputs = exhausted_review(Path(name).resolve())
            conf = Config.load(root / "config.json")
            self.assertEqual(conf.max_tokens, 65536)
            self.assertEqual(conf.max_calls, 1)
            self.assertEqual(read_json(root / "calibri-normalization-manifest.json")["prior_calls"], 8)
            items = verify(root, conf)
            old_data = read_json(parent / "items" / items[0]["item_id"] / "review_dag/input.json")["input"]
            self.assertEqual(candidate_input(root / "candidate-evidence" / items[0]["item_id"], items[0]), old_data)
            client = Client({"review_dag": outputs["review_dag"]}, CHECKED_PROTOCOL)
            pipeline = CALIBRIReviewResume(root, conf, client)
            self.assertEqual(pipeline.run()["results"][0]["status"], "model_accepted")
            pipeline.run()
            self.assertEqual(len(client.calls), 1)
            dag = read_json(root / "items" / items[0]["item_id"] / "dag.json")
            self.assertEqual(dag["nodes"], old_data["candidate"]["nodes"])
            self.assertFalse(dag["formal_eligible"])
            self.assertFalse(list((root / "items").glob("*/repair")))
            self.assertFalse(list((root / "items").glob("*/dependencies")))
            write_once(root / "completion.json", {"status": "processed"})
            self.assertTrue(audit_completed(root)["fixed_candidate_unchanged"])
            with self.assertRaisesRegex(ValueError, "one review continuation"):
                prepare(root, root.parent / "not-another-retry")

    def test_semantic_rejection_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as name:
            _, _, parent, outputs = prepare_revision(Path(name).resolve())
            outputs["review_dag"].update(decision="reject", issues=["A semantic flaw."], reason="A semantic flaw.")
            outputs["review_dag"]["checks"]["dependencies_sufficient"] = False
            CALIBRIRepairPipeline(parent, config(prompt_version=CHECKED_PROTOCOL, max_calls=6, max_tokens=32768),
                                  Client(outputs, CHECKED_PROTOCOL)).run()
            write_once(parent / "completion.json", {"status": "processed"})
            with self.assertRaisesRegex(ValueError, "exactly one"):
                prepare(parent, parent.parent / "forbidden-retry")

    def test_tampered_candidate_fails_before_call(self):
        with tempfile.TemporaryDirectory() as name:
            _, root, outputs = exhausted_review(Path(name).resolve())
            target = root / "candidate-evidence" / ("a" * 20) / "dependencies/output.json"
            target.write_text('{}')
            client = Client({"review_dag": outputs["review_dag"]}, CHECKED_PROTOCOL)
            with self.assertRaisesRegex(ValueError, "candidate evidence changed"):
                CALIBRIReviewResume(root, Config.load(root / "config.json"), client).run()
            self.assertFalse(client.calls)


if __name__ == "__main__":
    unittest.main()
