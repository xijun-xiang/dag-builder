"""One-pass structural pruning is exact; semantic acceptance stays separate."""

import copy
import unittest

from dag_builder.calibri_normalize import normalize
from dag_builder.config import Config
from dag_builder.lcb_answer_backward_review import canonicalize
from dag_builder.stages import prompt, stages_for
from test_calibri_normalize import fixture


def graph_case():
    item, proposal, dependencies = fixture()
    item["reference_origin"] = "calibri_model_output"
    proposal["steps"].append({
        "kind": "given", "statement": "The code prints a line.",
        "source_refs": ["C0001"], "support_type": "source_supported",
        "normalization_note": "An incidental code operation."})
    dependencies["dependencies"].append({
        "node_id": 3, "parents": [], "justification": "Code observation, not needed."})
    return item, proposal, dependencies


class LCBAnswerBackwardReviewTests(unittest.TestCase):
    def test_only_non_ancestors_deleted_and_no_edge_invented(self):
        item, proposal, dependencies = graph_case()
        normalized = normalize(proposal, item)
        graph = canonicalize(normalized, dependencies, item)
        self.assertEqual([node["original_node_id"] for node in graph["nodes"]],
                         [1, 2, 4])
        self.assertEqual([node["parents"] for node in graph["nodes"]],
                         [[], [1], [2]])
        self.assertEqual(graph["transformation"]["new_edges"], 0)
        self.assertEqual([row["original_node_id"] for row in graph[
            "transformation"]["discarded_nodes"]], [3])
        self.assertEqual(graph["nodes"][-1]["statement"], item["reference_code"])

    def test_incorrect_or_missing_parent_is_not_silently_repaired(self):
        item, proposal, dependencies = graph_case()
        normalized = normalize(proposal, item)
        broken = copy.deepcopy(dependencies)
        broken["dependencies"][1]["parents"] = []
        with self.assertRaisesRegex(ValueError, "no declared premise"):
            canonicalize(normalized, broken, item)

    def test_protocol_requires_fresh_semantic_review(self):
        config = Config(task_type="livecodebench",
                        prompt_version="lcb-answer-backward-review-v1",
                        solution_source="reference_dag_revision")
        self.assertEqual(stages_for(config), ("review_dag",))
        review_prompt = prompt("review_dag", config.prompt_version,
                               config.task_type, config.solution_source)
        self.assertIn("NOT an accepted reference DAG", review_prompt)
        with self.assertRaisesRegex(ValueError, "dedicated source label"):
            Config(task_type="livecodebench",
                   prompt_version="lcb-answer-backward-review-v1",
                   solution_source="independent_generation")


if __name__ == "__main__":
    unittest.main()
