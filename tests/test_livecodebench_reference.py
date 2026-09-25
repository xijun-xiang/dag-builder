"""Synthetic responses only: no real benchmark, API calls or code execution."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dag_builder.config import Config
from dag_builder.livecodebench_reference import LiveCodeBenchReferencePipeline, public_input, validate_reference
from dag_builder.livecodebench_source import REVISION, normalize_livecodebench
from dag_builder.pipeline import Pipeline
from dag_builder.report import render
from dag_builder.storage import digest, read_json, write_once
from test_livecodebench import fixture


class Client:
    def __init__(self, output, finish="stop"):
        self.calls, self.output, self.finish = [], output, finish

    def complete(self, request):
        self.calls.append(request)
        return {"model": request["model"], "choices": [{"finish_reason": self.finish,
                "message": {"content": json.dumps(self.output), "reasoning_content": "DO_NOT_PARSE"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.item = normalize_livecodebench([fixture()], REVISION)[0][0]
        self.config = Config(task_type="livecodebench", prompt_version="livecodebench-reference-v1",
                             workers=1, strict_response_contract=True)
        self.value = {"language": "python3", "code": "class Solution:\n    def identity(self, x):\n        return x\n",
                      "rationale": "The returned integer is the given integer; constant time and space."}
        selection = {"selected_ids": [self.item["item_id"]]}
        write_once(self.root / "items.json", [self.item])
        write_once(self.root / "selection.json", selection)
        write_once(self.root / "prepared-manifest.json", {"items_sha256": digest([self.item]),
                                                         "selection_sha256": digest(selection)})

    def tearDown(self):
        self.temp.cleanup()

    def test_one_candidate_resume_without_resampling(self):
        client = Client(self.value)
        for _ in range(2):
            result = LiveCodeBenchReferencePipeline(self.root, self.config, client).run()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["results"][0]["status"], "reference_candidate")
        raw = json.dumps(client.calls)
        self.assertNotIn("private_test_cases", raw)
        self.assertNotIn("tests_sha256", raw)
        self.assertNotIn("DO_NOT_PARSE", json.dumps(read_json(self.root / "items" / self.item["item_id"] / "reference.json")))
        self.assertTrue((render(self.root) / "review.html").is_file())

    def test_truncation_is_preserved_and_never_repaired(self):
        client = Client(self.value, "length")
        for _ in range(2):
            result = LiveCodeBenchReferencePipeline(self.root, self.config, client).run()
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertEqual(len(client.calls), 1)

    def test_no_implicit_dag_generation_or_base_pipeline(self):
        client = Client(self.value)
        with self.assertRaises(ValueError):
            Pipeline(self.root, self.config, client)
        with self.assertRaises(ValueError):
            LiveCodeBenchReferencePipeline(self.root, self.config, client).run(through="review_dag")
        self.assertFalse(client.calls)

    def test_source_drift_stops_before_api(self):
        client = Client(self.value)
        path = self.root / "items.json"
        path.write_text(json.dumps([dict(self.item, question="changed")]))
        with self.assertRaises(ValueError):
            LiveCodeBenchReferencePipeline(self.root, self.config, client).run()
        self.assertFalse(client.calls)

    def test_opaque_extra_field_not_transmitted(self):
        item = dict(self.item, private_test_cases="SECRET", unknown="SECRET")
        self.assertNotIn("SECRET", json.dumps(public_input(item)))

    def test_invalid_program_contract_rejected(self):
        for code in ("```python\nx=1\n```", "class Solution:", "def identity(x): return x",
                     "class Solution:\n def wrong(self,x): return x"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_reference(dict(self.value, code=code), self.item)

    def test_budget_stops_without_api(self):
        client = Client(self.value)
        config = replace(self.config, max_reserved_tokens=1)
        result = LiveCodeBenchReferencePipeline(self.root, config, client).run()
        self.assertTrue(result["paused"])
        self.assertFalse(client.calls)


if __name__ == "__main__":
    unittest.main()
