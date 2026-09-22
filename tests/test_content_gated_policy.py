"""User-approved per-response policy never turns bad output into a sample."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.client import APIClient, CallFailure
from dag_builder.config import Config
from dag_builder.pipeline import Pipeline
from dag_builder.response_contract import check_response
from dag_builder.schemas import InvalidOutput, parse_native_solution


class PolicyTests(unittest.TestCase):
    def test_cap_mismatch_warning_only_within_reservation(self):
        request = {"model": "fixture", "max_tokens": 64}
        response = {"model": "fixture", "usage": {"prompt_tokens": 10, "completion_tokens": 70, "total_tokens": 80}}
        check = check_response(request, response, 100, strict=True, content_gated=True)
        self.assertEqual(check["violations"], [])
        self.assertIn("completion_exceeds_requested_max_tokens", check["warnings"])
        self.assertIn("usage_exceeds_reserved_allowance", check_response(
            request, response, 79, strict=True, content_gated=True)["violations"])
        self.assertIn("completion_exceeds_requested_max_tokens", check_response(
            request, response, 100, strict=True)["violations"])
        self.assertNotIn("content_gated_response", Config().to_dict())

    def test_queued_request_never_reaches_transport_after_stop(self):
        event = threading.Event()
        client = APIClient(Config(transport="curl"), "synthetic-secret")
        client.bind_stop_event(event)
        with patch.object(client, "_curl_request") as send:
            failures = []
            def request():
                try:
                    client.complete({})
                except CallFailure as error:
                    failures.append(error.category)
            with client._start_lock:
                worker = threading.Thread(target=request)
                worker.start()
                event.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ["paused"])
        send.assert_not_called()

    def test_native_fields_cannot_substitute_or_duplicate(self):
        for message in ({"content": "Final answer: A"}, {"reasoning_content": "Final answer: A"},
                        {"content": "Final answer: A", "reasoning_content": "Final answer: A"}):
            with self.assertRaises(InvalidOutput):
                parse_native_solution(message)

    def test_content_only_nonstop_and_broken_json_rejected_without_repair(self):
        class Client:
            def __init__(self, finish, content):
                self.finish, self.content = finish, content
            def complete(self, request):
                return {"model": "fixture", "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                        "choices": [{"finish_reason": self.finish, "message": {
                            "content": self.content, "reasoning_content": '{"ok":true}'}}]}
        for finish, content in (("length", '{"ok":true}'), ("stop", '{"ok":'), ("stop", None)):
            with tempfile.TemporaryDirectory() as root:
                runner = Pipeline(Path(root).resolve(), Config(model="fixture", content_gated_response=True), Client(finish, content))
                with self.assertRaises(InvalidOutput):
                    runner.request_stage("fixture", {"item_id": "a"*20}, {},
                                         {"model": "fixture", "max_tokens": 64}, lambda output: None)
                stage = Path(root) / "items" / ("a"*20) / "fixture"
                self.assertFalse((stage / "output.json").exists())
                self.assertTrue((stage / "validation.json").exists())
                self.assertTrue((stage / "attempt-00/response.json").exists())


if __name__ == "__main__":
    unittest.main()
