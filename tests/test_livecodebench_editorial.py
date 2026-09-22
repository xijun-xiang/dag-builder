"""Synthetic source/graph/API fixtures only; no network or reference execution."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.livecodebench_editorial import CHECKS, EditorialPilot, PROTOCOL, validate_audit, validate_editorial, validate_graph
from dag_builder.schemas import REVIEW_CHECKS
from dag_builder.storage import digest, read_json, write_once


def fixture():
    source = {"question_id": "abc396_a", "question_title": "Synthetic identity",
              "url": "https://atcoder.jp/contests/abc396/editorial/1",
              "official_index_url": "https://atcoder.jp/contests/abc396/tasks/abc396_a/editorial",
              "official_label_observed": True, "task_match_reviewed": True,
              "capture_method": "synthetic", "capture_note": "not real evidence",
              "editorial": "Returning the input solves the identity task.", "reference_code": "print(input())"}
    return {"item_id": "a" * 20, "row": 0, "task_type": "livecodebench", "platform": "atcoder",
            "question_id": source["question_id"], "question_title": source["question_title"],
            "question": "Return the input text.", "io_type": "stdin", "entry_point": None, "starter_code": "",
            "official_editorial": source["editorial"], "reference_code": source["reference_code"],
            "editorial_source": source, "reference_origin": "official_editorial", "reference_execution": "not_executed"}


def graph(item):
    return {"nodes": [
        {"node_id": 1, "kind": "given", "statement": "The task requires returning the supplied text.",
         "source_field": "question", "source_quote": "Return the input text.",
         "support_type": "source_supported", "parents": [], "justification": "Task requirement."},
        {"node_id": 2, "kind": "derived", "statement": "Returning the supplied text meets the task requirement.",
         "source_field": "editorial", "source_quote": item["official_editorial"],
         "support_type": "source_supported", "parents": [1], "justification": "Identity specification."},
        {"node_id": 3, "kind": "answer", "statement": item["reference_code"],
         "source_field": "reference_code", "source_quote": item["reference_code"],
         "support_type": "source_supported", "parents": [2], "justification": "Code attachment, not a reasoning step."}]}


def audit():
    return {"decision": "accept", "checks": dict.fromkeys((*REVIEW_CHECKS["review_dag"], *CHECKS), True),
            "issues": [], "reason": "Synthetic fixture only."}


class Client:
    def __init__(self, item):
        self.item, self.calls = item, []

    def complete(self, request):
        self.calls.append(request)
        data = json.loads(request["messages"][1]["content"])
        result = audit() if "candidate" in data else graph(self.item)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}


class EditorialTests(unittest.TestCase):
    def test_source_identity_and_category(self):
        item = fixture()
        validate_editorial(item)
        for key, value in (("url", "https://atcoder.jp.evil.example/contests/abc396/editorial/1"),
                           ("question_title", "another problem"), ("task_match_reviewed", False)):
            bad = copy.deepcopy(item)
            bad["editorial_source"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_editorial(bad)

    def test_graph_requires_real_quotes_and_supplement_disclosure(self):
        item = fixture()
        validate_graph(graph(item), item)
        for key, value in (("source_quote", "invented quotation"), ("support_type", "official_gold"),
                           ("parents", [2]), ("statement", "By step 1 the claim holds.")):
            bad = graph(item)
            bad["nodes"][1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_graph(bad, item)
        bad = graph(item)
        bad["nodes"][0]["support_type"] = "supplementary"
        with self.assertRaisesRegex(ValueError, "given facts"):
            validate_graph(bad, item)

    def test_audit_does_not_accept_missing_checks(self):
        good = audit()
        validate_audit(good)
        for key in CHECKS:
            bad = copy.deepcopy(good)
            del bad["checks"][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_audit(bad)

    def test_protocol_is_explicit_and_not_a_formal_release(self):
        with self.assertRaises(ValueError):
            Config(task_type="livecodebench", prompt_version=PROTOCOL)
        with tempfile.TemporaryDirectory() as folder:
            root, item = Path(folder).resolve(), fixture()
            selection, sources = {"selected_ids": [item["item_id"]]}, [item["editorial_source"]]
            write_once(root / "items.json", [item])
            write_once(root / "selection.json", selection)
            write_once(root / "sources.json", sources)
            write_once(root / "editorial-manifest.json", {"protocol": PROTOCOL, "items_sha256": digest([item]),
                "selection_sha256": digest(selection), "source_snapshots_sha256": digest(sources)})
            config = Config(task_type="livecodebench", prompt_version=PROTOCOL, solution_source="editorial_grounded_pilot",
                            max_calls=12, max_reserved_tokens=600000)
            client = Client(item)
            pipeline = EditorialPilot(root, config, client)
            pipeline.run()
            pipeline.run()
            self.assertEqual(len(client.calls), 2)
            directory = root / "items" / item["item_id"]
            self.assertEqual(read_json(directory / "result.json")["status"], "editorial_candidate")
            self.assertFalse((directory / "dag.json").exists())
            self.assertFalse(read_json(directory / "editorial-candidate.json")["formal_eligible"])
            self.assertNotIn("private_test_cases", json.dumps(client.calls))
            self.assertNotIn("tests_sha256", json.dumps(client.calls))


if __name__ == "__main__":
    unittest.main()
