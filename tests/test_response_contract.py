"""Synthetic responses only. Control failures never become scientific rejects."""

import tempfile
import unittest
import test_builder
from pathlib import Path
from unittest.mock import patch
import json
import ssl

from dag_builder.client import APIClient, CallFailure
from dag_builder.config import Config
from dag_builder.contract_probe import probe_contract
from dag_builder.pipeline import Pipeline
from dag_builder.response_contract import check_response, reported_tokens
from dag_builder.stages import payload, stage_input
from dag_builder.storage import write_once
from test_builder import FakeClient


def request():
    return {"model": "fixture", "max_tokens": 64, "thinking": {"type": "disabled"}}


def response():
    return {"model": "fixture", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}


class ResponseContractTests(unittest.TestCase):
    def test_tls_compatibility_does_not_disable_verification(self):
        self.assertNotIn("tls_max_version", Config().to_dict())
        with self.assertRaises(ValueError):
            Config(tls_max_version="TLSv1.0")
        config = Config(tls_max_version="TLSv1.2")
        client = APIClient(config, "synthetic-test-secret")
        with patch("dag_builder.client.build_opener") as opener, patch("dag_builder.client.HTTPSHandler") as handler:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(response()).encode()
            client.complete(payload("solve", {}, config))
        context = handler.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_transport_does_not_drop_control_fields(self):
        config = Config(thinking="disabled", reasoning_effort="none")
        p = payload("solve", {}, config)
        client = APIClient(config, "synthetic-test-secret")
        with patch("dag_builder.client.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(response()).encode()
            client.complete(p)
        sent = opener.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(sent.data), p)
        self.assertEqual(sent.full_url, config.base_url + "chat/completions")

    def test_valid_and_legacy_config_serialization(self):
        self.assertFalse(check_response(request(), response(), 200, strict=True)["violations"])
        self.assertNotIn("strict_response_contract", Config().to_dict())
        self.assertTrue(Config(strict_response_contract=True).to_dict()["strict_response_contract"])
        with self.assertRaises(ValueError):
            Config(strict_response_contract="true")

    def test_none_requires_disabled_and_is_serialized(self):
        for mode in (None, "enabled"):
            with self.assertRaises(ValueError):
                Config(thinking=mode, reasoning_effort="none")
        config = Config(thinking="disabled", reasoning_effort="none")
        p = payload("solve", {}, config)
        self.assertEqual(p["reasoning_effort"], "none")
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertEqual(p["temperature"], 0)

    def test_max_output_enforced_even_below_total_allowance(self):
        r = response()
        r["usage"].update(completion_tokens=65, total_tokens=75)
        self.assertIn("completion_exceeds_requested_max_tokens", check_response(request(), r, 10000)["violations"])

    def test_missing_bad_usage_and_model_rejected_in_strict_mode(self):
        for value in (None, {}, {"total_tokens": True}, {"total_tokens": -1}, {"total_tokens": "15"}):
            r = dict(response(), usage=value)
            self.assertTrue(check_response(request(), r, 200, strict=True)["violations"])
        r = dict(response(), model="other")
        self.assertIn("response_model_mismatch", check_response(request(), r, 200, strict=True)["violations"])

    def test_reasoning_text_or_reported_tokens_rejected(self):
        for text, details in (("hidden", {}), (None, {"reasoning_tokens": 1})):
            r = response()
            r["choices"][0]["message"]["reasoning_content"] = text
            r["usage"]["completion_tokens_details"] = details
            self.assertIn("reasoning_returned_when_disabled", check_response(request(), r, 200)["violations"])

    def test_accounting_uses_larger_total_and_preserves_unknown_allowance(self):
        r = response()
        r["usage"]["total_tokens"] = 3
        self.assertEqual(reported_tokens(r), 15)
        self.assertIn("inconsistent_usage_total", check_response(request(), r, 10)["violations"])
        self.assertEqual(check_response(request(), {}, 100)["accounted_tokens"], 100)


class PipelineContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_builder.BuilderTests()
        self.fixture.setUp()
        self.root, self.item = self.fixture.root, self.fixture.item

    def tearDown(self):
        self.fixture.tearDown()

    def test_violation_stops_before_output_and_never_retries_or_resumes(self):
        for resilient in (False, True):
            root = self.root / str(resilient)
            write_once(root / "items.json", [self.item])
            write_once(root / "selection.json", {"selected_ids": [self.item["item_id"]]})
            client = FakeClient()
            original = client.complete

            def bad(p):
                r = original(p)
                r["usage"] = {"total_tokens": 1000000}
                return r

            client.complete = bad
            config = Config(workers=1)
            runner = Pipeline(root, config, client, resilient=resilient)
            result = runner.run()
            self.assertTrue(result["global_stop"])
            self.assertEqual(result["results"][0]["reason"], "response_contract_violation")
            self.assertEqual(result["accounted_tokens"], 1000000)
            stage = root / "items" / self.item["item_id"] / "solve"
            self.assertFalse((stage / "output.json").exists())
            self.assertTrue((stage / "attempt-00/response.json").exists())
            self.assertTrue((stage / "attempt-00/contract_check-v1.json").exists())
            resumed = Pipeline(root, config, client, resilient=resilient).run()
            self.assertTrue(resumed["paused"])
            self.assertEqual(resumed["accounted_tokens"], 1000000)
            self.assertEqual(len(client.calls), 1)

    def test_cached_response_checked_even_without_budget_restore(self):
        for resilient in (False, True):
            runner = Pipeline(self.root / str(resilient), Config(), FakeClient(), resilient=resilient)
            p = payload("solve", stage_input("solve", self.item, {}), runner.config)
            directory = runner.root / "items" / self.item["item_id"] / "solve"
            runner._reserve(directory / "attempt-00", p)
            write_once(directory / "attempt-00/response.json", {"body": {"usage": {"total_tokens": 1000000}}})
            with self.assertRaises(CallFailure):
                runner._call(directory, p)
            self.assertEqual(len(runner.client.calls), 0)


class ProbeTests(unittest.TestCase):
    def test_exact_configured_cap_and_high_are_not_silently_reduced(self):
        class Client:
            def __init__(self):
                self.calls = []

            def complete(self, p):
                self.calls.append(p)
                r = response()
                r["model"] = p["model"]
                if "19999" in p["messages"][0]["content"]:
                    r["choices"][0]["finish_reason"] = "length"
                    r["usage"] = {"prompt_tokens": 100, "completion_tokens": 32768, "total_tokens": 32868}
                return r

        with tempfile.TemporaryDirectory() as path:
            client = Client()
            config = Config(thinking="enabled", reasoning_effort="high", max_tokens=32768,
                            max_calls=2, max_reserved_tokens=100000, response_format="json_object")
            result = probe_contract(Path(path).resolve(), config, client, configured_max_tokens=True)
            self.assertTrue(result["passed"])
            self.assertEqual(result["actual_request_max_tokens"], 32768)
            self.assertEqual(len(client.calls), 2)
            self.assertGreater(result["reserved_tokens"], 65536)
            for p in client.calls:
                self.assertEqual(p["max_tokens"], 32768)
                self.assertEqual(p["reasoning_effort"], "high")
                self.assertEqual(p["response_format"], {"type": "json_object"})
                self.assertEqual(p["thinking"], {"type": "enabled"})
                self.assertNotIn("temperature", p)

    def test_exact_cap_still_honors_smaller_total_budget(self):
        with tempfile.TemporaryDirectory() as path:
            client = FakeClient()
            config = Config(thinking="enabled", reasoning_effort="high", max_tokens=32768, max_reserved_tokens=1000)
            result = probe_contract(Path(path).resolve(), config, client, configured_max_tokens=True)
            self.assertEqual(result["attempt_count"], 0)
            self.assertFalse(result["passed"])
            self.assertEqual(result["results"][0]["reason"], "budget_exhausted")

    def test_enabled_thinking_probe_keeps_reasoning_separate_and_bounded(self):
        class Client:
            def __init__(self):
                self.calls = []

            def complete(self, p):
                self.calls.append(p)
                r = response()
                r["model"] = p["model"]
                r["choices"][0]["message"]["reasoning_content"] = "Private reasoning; not formal JSON."
                if "9999" in p["messages"][0]["content"]:
                    r["choices"][0]["finish_reason"] = "length"
                return r

        with tempfile.TemporaryDirectory() as path:
            client = Client()
            config = Config(thinking="enabled", reasoning_effort="low", max_tokens=32768)
            self.assertTrue(probe_contract(Path(path).resolve(), config, client)["passed"])
            self.assertEqual(len(client.calls), 2)
            for p in client.calls:
                self.assertEqual(p["max_tokens"], 4096)
                self.assertNotIn("temperature", p)
                self.assertEqual(p["thinking"], {"type": "enabled"})

    def test_probe_preserves_smaller_call_and_output_budgets(self):
        class Client:
            calls = 0

            def complete(self, p):
                self.calls += 1
                self.last_request = p
                r = response()
                r["model"] = p["model"]
                return r

        with tempfile.TemporaryDirectory() as path:
            client = Client()
            config = Config(task_type="gpqa", prompt_version="gpqa-reference-v1",
                            thinking="disabled", max_calls=1, max_tokens=32)
            result = probe_contract(Path(path).resolve(), config, client)
            self.assertFalse(result["passed"])
            self.assertEqual(client.calls, 1)
            self.assertEqual(client.last_request["max_tokens"], 32)
            self.assertEqual(result["results"][-1]["reason"], "budget_exhausted")

    def test_probe_two_calls_cap_truncation_expected_and_resume_free(self):
        class Client:
            calls = 0

            def complete(self, p):
                self.calls += 1
                r = response()
                r["model"] = p["model"]
                if "9999" in p["messages"][0]["content"]:
                    r["choices"][0]["finish_reason"] = "length"
                return r

        with tempfile.TemporaryDirectory() as path:
            path = Path(path).resolve()
            client = Client()
            config = Config(thinking="disabled", reasoning_effort="none")
            self.assertTrue(probe_contract(path, config, client)["passed"])
            self.assertTrue(probe_contract(path, config, client)["passed"])
            self.assertEqual(client.calls, 2)

    def test_probe_timeout_not_retried(self):
        with tempfile.TemporaryDirectory() as path:
            path = Path(path).resolve()
            client = FakeClient(failure=CallFailure("uncertain_remote_state"))
            result = probe_contract(path, Config(thinking="disabled"), client)
            self.assertFalse(result["passed"])
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
