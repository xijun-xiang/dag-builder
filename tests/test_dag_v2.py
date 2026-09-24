"""Regression tests for the DeepSeek DAG v2 protocol fixes."""

import unittest

from dag_builder.config import Config
from dag_builder.schemas import InvalidOutput, validate_nodes, validate_review
from dag_builder.stages import STAGES, THINKING_STAGES, prompt, stages_for
from dag_builder.validation import validate_parents


class DagV2Tests(unittest.TestCase):
    def test_versions_route_without_changing_v1(self):
        mmlu = Config(prompt_version="mmlu-thinking-v2", thinking="enabled")
        gsm8k = Config(task_type="gsm8k", prompt_version="gsm8k-v2")
        self.assertEqual(stages_for(mmlu), THINKING_STAGES)
        self.assertEqual(stages_for(gsm8k), STAGES)
        self.assertNotEqual(
            prompt("atomize", "mmlu-thinking-v2"),
            prompt("atomize", "mmlu-thinking-v1"),
        )
        self.assertNotEqual(
            prompt("atomize", "gsm8k-v2", "gsm8k"),
            prompt("atomize", "gsm8k-v1", "gsm8k"),
        )
        self.assertNotEqual(
            prompt("dependencies", "gsm8k-v2", "gsm8k"),
            prompt("dependencies", "gsm8k-v1", "gsm8k"),
        )

    def test_review_prompts_require_string_issues(self):
        for version, task in (
            ("mmlu-thinking-v2", "mmlu"),
            ("gsm8k-v2", "gsm8k"),
        ):
            with self.subTest(version=version):
                content = prompt("review_dag", version, task)
                self.assertIn("JSON array of strings", content)
                self.assertIn("Do not return issue objects", content)

    def test_mmlu_choice_source_is_exact_and_not_answer_authority(self):
        value = {
            "nodes": [
                {
                    "node_id": 1,
                    "kind": "derived",
                    "statement": "The computed speed is 3 m/s.",
                    "source_field": "solution",
                    "source_quote": "speed is 3 m/s",
                },
                {
                    "node_id": 2,
                    "kind": "answer",
                    "statement": "The result matches option A, 3 m/s.",
                    "source_field": "choice_A",
                    "source_quote": "3 m/s",
                },
            ]
        }
        validate_nodes(
            value,
            "What is the speed?",
            "The speed is 3 m/s, so select A.",
            extra_sources={
                "choice_A": "3 m/s",
                "choice_B": "2 m/s",
                "choice_C": "6 m/s",
                "choice_D": "12 m/s",
            },
        )
        value["nodes"][1]["source_quote"] = "option A"
        with self.assertRaises(InvalidOutput):
            validate_nodes(
                value,
                "What is the speed?",
                "The speed is 3 m/s, so select A.",
                extra_sources={"choice_A": "3 m/s"},
            )

    def test_issue_objects_remain_invalid(self):
        value = {
            "decision": "reject",
            "checks": {
                "statements_correct": True,
                "faithful_to_solution": True,
                "dependencies_sufficient": False,
                "dependencies_minimal": True,
                "justifications_complete": True,
                "no_new_facts": True,
            },
            "issues": [{"node_id": 2, "evidence": "missing parent"}],
            "reason": "A dependency is missing.",
        }
        with self.assertRaises(InvalidOutput):
            validate_review(value, "review_dag")

    def test_transitively_redundant_direct_parent_is_rejected(self):
        nodes = [
            {"node_id": 1, "kind": "given"},
            {"node_id": 2, "kind": "given"},
            {"node_id": 3, "kind": "derived"},
            {"node_id": 4, "kind": "answer"},
        ]
        redundant = {
            "parents": [
                {"node_id": 1, "parents": []},
                {"node_id": 2, "parents": []},
                {"node_id": 3, "parents": [1, 2]},
                {"node_id": 4, "parents": [2, 3]},
            ]
        }
        with self.assertRaisesRegex(
            InvalidOutput, "transitively redundant direct dependency"
        ):
            validate_parents(redundant, nodes)

        minimal = {
            "parents": [
                {"node_id": 1, "parents": []},
                {"node_id": 2, "parents": []},
                {"node_id": 3, "parents": [1, 2]},
                {"node_id": 4, "parents": [3]},
            ]
        }
        validate_parents(minimal, nodes)


if __name__ == "__main__":
    unittest.main()
