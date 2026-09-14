"""Native reasoning protocol tests. All model responses are offline fixtures."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dag_builder.config import Config
from dag_builder.pipeline import Pipeline
from dag_builder.schemas import InvalidOutput, parse_native_solution
from dag_builder.source import normalize
from dag_builder.stages import (
    STAGES,
    THINKING_STAGES,
    payload,
    prompt,
    stage_input,
    stages_for,
)
from dag_builder.storage import read_json, write_once
from test_builder import OUTPUTS, QUESTION, RATIONALE, SOLUTION


def native_config():
    return Config(
        prompt_version="mmlu-thinking-v1",
        thinking="enabled",
        reasoning_effort="high",
        response_format="json_object",
        workers=1,
    )


class NativeClient:
    def __init__(self, reasoning=RATIONALE, answer="A", structured=None):
        self.reasoning, self.answer = reasoning, answer
        self.structured = deepcopy(SOLUTION if structured is None else structured)
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        stage = next(
            s
            for s in THINKING_STAGES
            if prompt(s, "mmlu-thinking-v1") == request["messages"][0]["content"]
        )
        if stage == "solve":
            content = f"A concise fixture explanation.\nFinal answer: {self.answer}"
        else:
            content = json.dumps(
                self.structured if stage == "structure_solution" else OUTPUTS[stage]
            )
        return {
            "model": "test-fixture-only",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": self.reasoning,
                        "content": content,
                    },
                }
            ],
            "usage": {"total_tokens": 100},
        }


class ThinkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.item = normalize(
            [
                {
                    "question": QUESTION,
                    "choices": ["3 m/s", "2 m/s", "6 m/s", "12 m/s"],
                    "answer": 0,
                }
            ],
            "a" * 40,
            "high_school_physics",
            "test",
        )[0]
        write_once(self.root / "items.json", [self.item])
        write_once(
            self.root / "selection.json", {"selected_ids": [self.item["item_id"]]}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_config_requires_enabled_thinking(self):
        for mode in (None, "disabled"):
            with self.assertRaises(ValueError):
                Config(prompt_version="mmlu-thinking-v1", thinking=mode)
        with self.assertRaises(ValueError):
            Config(reasoning_effort="high")
        with self.assertRaises(ValueError):
            Config(
                task_type="gsm8k", prompt_version="mmlu-thinking-v1", thinking="enabled"
            )

    def test_existing_task_stage_lists_unchanged(self):
        self.assertEqual(stages_for(Config()), STAGES)
        self.assertEqual(
            stages_for(Config(task_type="gsm8k", prompt_version="gsm8k-v1")), STAGES
        )
        self.assertEqual(stages_for(native_config()), THINKING_STAGES)

    def test_labeled_choices_no_answer_leak_or_json_in_native_solve(self):
        data = stage_input("solve", self.item, {})
        request = payload("solve", data, native_config())
        question = json.loads(request["messages"][1]["content"])["question"]
        self.assertEqual(question["choices"], dict(zip("ABCD", self.item["choices"])))
        self.assertEqual(set(question), {"question", "choices"})
        self.assertIsInstance(data["question"]["choices"], tuple)
        self.assertNotIn("temperature", request)
        self.assertNotIn("response_format", request)
        self.assertEqual(request["thinking"], {"type": "enabled"})
        self.assertEqual(request["reasoning_effort"], "high")

    def test_native_parser_preserves_both_fields(self):
        message = {
            "reasoning_content": "  native trace\n",
            "content": "Explanation.\nFinal answer: D\n",
        }
        value = parse_native_solution(message)
        self.assertEqual(value["answer"], "D")
        self.assertEqual(value["reasoning_content"], message["reasoning_content"])
        self.assertEqual(value["final_response"], message["content"])

    def test_native_parser_never_falls_back_to_content(self):
        for reasoning in (None, "", "  ", []):
            with self.assertRaises(InvalidOutput):
                parse_native_solution(
                    {
                        "reasoning_content": reasoning,
                        "content": "A long explanation.\nFinal answer: A",
                    }
                )

    def test_native_parser_rejects_ambiguous_or_nonterminal_answer(self):
        for content in (
            "Answer A",
            "Final answer: A\nFinal answer: B",
            "Final answer: A\nBut perhaps B",
            '{"answer":"A"}',
        ):
            with self.assertRaises(InvalidOutput):
                parse_native_solution(
                    {"reasoning_content": RATIONALE, "content": content}
                )

    def test_full_native_pipeline_and_resume(self):
        client = NativeClient()
        runner = Pipeline(self.root, native_config(), client)
        result = runner.run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 7)
        dag = read_json(self.root / "items" / self.item["item_id"] / "dag.json")
        self.assertEqual(dag["native_solution"]["reasoning_content"], RATIONALE)
        self.assertEqual(dag["reference_solution"], SOLUTION)
        for request in client.calls[1:]:
            self.assertEqual(request["response_format"], {"type": "json_object"})
        structure_input = json.loads(client.calls[1]["messages"][1]["content"])
        self.assertNotIn("reference_answer", structure_input)
        review_input = json.loads(client.calls[2]["messages"][1]["content"])
        self.assertIn("native_solution", review_input)
        self.assertEqual(review_input["solution"], SOLUTION)
        runner.run()
        self.assertEqual(len(client.calls), 7)

    def test_wrong_answer_stops_before_structuring(self):
        client = NativeClient(answer="B")
        result = Pipeline(self.root, native_config(), client).run()
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(len(client.calls), 1)

    def test_structuring_cannot_change_native_answer(self):
        solution = deepcopy(SOLUTION)
        solution["answer"] = "B"
        client = NativeClient(structured=solution)
        result = Pipeline(self.root, native_config(), client).run()
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertEqual(result["results"][0]["stage"], "structure_solution")
        self.assertEqual(len(client.calls), 2)

    def test_absent_native_field_is_review_failure_and_response_retained(self):
        client = NativeClient(reasoning=None)
        result = Pipeline(self.root, native_config(), client).run()
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(
            (
                self.root
                / "items"
                / self.item["item_id"]
                / "solve/attempt-00/response.json"
            ).exists()
        )

    def test_solve_barrier_and_then_full_resume(self):
        client = NativeClient()
        runner = Pipeline(self.root, native_config(), client)
        result = runner.run(through="solve")
        self.assertEqual(result["results"][0]["status"], "stage_complete")
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(
            (self.root / "items" / self.item["item_id"] / "result.json").exists()
        )
        runner.run()
        self.assertEqual(len(client.calls), 7)


if __name__ == "__main__":
    unittest.main()
