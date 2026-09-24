"""No network or credentials required. Fake outputs are test fixtures only."""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.client import APIClient, CallFailure, NoRedirect, load_key
from dag_builder.config import Config
from dag_builder.export import release, trajectory
from dag_builder.math_answers import (
    extract_gsm8k_answer,
    numeric_answers_equivalent,
)
from dag_builder.pipeline import Pipeline, answer_matches
from dag_builder.report import overview, render
from dag_builder.schemas import (
    InvalidOutput,
    parse_object,
    validate_math_solution,
    validate_nodes,
    validate_review,
)
from dag_builder.source import (
    canonicalize_gsm8k_rationale,
    normalize,
    normalize_gsm8k,
    select,
)
from dag_builder.stages import STAGES, payload, prompt, stage_input
from dag_builder.storage import digest, read_json, run_lock, write_once
from dag_builder.validation import validate_justifications, validate_parents

QUESTION = "A body moves 6 m in 2 s at constant speed. What is its speed?"
RATIONALE = "The distance is 6 m and the time is 2 s. Speed equals distance divided by time. The speed is 3 m/s. Therefore option A is correct."
SOLUTION = {
    "answer": "A",
    "rationale": RATIONALE,
    "estimated_difficulty": {"level": "low", "reason": "one division"},
}
NODES = [
    {
        "node_id": 1,
        "kind": "given",
        "statement": "The distance is 6 m.",
        "source_field": "solution",
        "source_quote": "The distance is 6 m",
    },
    {
        "node_id": 2,
        "kind": "given",
        "statement": "The time is 2 s.",
        "source_field": "solution",
        "source_quote": "the time is 2 s",
    },
    {
        "node_id": 3,
        "kind": "knowledge",
        "statement": "Speed equals distance divided by time.",
        "source_field": "solution",
        "source_quote": "Speed equals distance divided by time.",
    },
    {
        "node_id": 4,
        "kind": "derived",
        "statement": "The speed is 3 m/s.",
        "source_field": "solution",
        "source_quote": "The speed is 3 m/s.",
    },
    {
        "node_id": 5,
        "kind": "answer",
        "statement": "Option A is correct.",
        "source_field": "solution",
        "source_quote": "option A is correct.",
    },
]
PARENTS = {
    "parents": [
        {"node_id": i, "parents": p}
        for i, p in enumerate([[], [], [], [1, 2, 3], [4]], 1)
    ]
}
JUSTIFICATIONS = {
    "justifications": [
        {"node_id": i, "text": "Explicit test-only explanation."} for i in range(1, 6)
    ]
}
REVIEW = {
    "decision": "accept",
    "checks": {
        "answer_correct": True,
        "intermediate_correct": True,
        "premises_complete": True,
        "trace_sufficient": True,
    },
    "issues": [],
    "reason": "All fixture checks pass.",
}
DAG_REVIEW = {
    "decision": "accept",
    "checks": {
        k: True
        for k in (
            "statements_correct",
            "faithful_to_solution",
            "dependencies_sufficient",
            "dependencies_minimal",
            "justifications_complete",
            "no_new_facts",
        )
    },
    "issues": [],
    "reason": "Fixture only.",
}
OUTPUTS = dict(
    zip(
        STAGES,
        [SOLUTION, REVIEW, {"nodes": NODES}, PARENTS, JUSTIFICATIONS, DAG_REVIEW],
    )
)


class FakeClient:
    def __init__(self, outputs=None, failure=None, finish_reason="stop"):
        self.outputs = outputs or OUTPUTS
        self.failure, self.finish_reason = failure, finish_reason
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        if self.failure:
            raise self.failure
        from dag_builder.stages import prompt

        stage = next(
            s for s in STAGES if request["messages"][0]["content"] == prompt(s)
        )
        return {
            "model": "test-fixture-only",
            "choices": [
                {
                    "finish_reason": self.finish_reason,
                    "message": {"content": json.dumps(self.outputs[stage])},
                }
            ],
            "usage": {"total_tokens": 100},
        }


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.item = normalize(
            [
                {
                    "question": QUESTION,
                    "choices": ["3 m/s", "2 m/s", "6 m/s", "12 m/s"],
                    "answer": 0,
                    "subject": "high_school_physics",
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

    def run_pipeline(self, client=None, config=None, **kwargs):
        return Pipeline(
            self.root, config or Config(workers=1), client or FakeClient(), **kwargs
        ).run()

    def test_full_pipeline_and_resume_no_new_calls(self):
        client = FakeClient()
        first = self.run_pipeline(client)
        self.assertEqual(first["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 6)
        second = self.run_pipeline(client)
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(second["attempt_count"], 6)

    def test_solve_has_no_gold_label(self):
        data = stage_input("solve", self.item, {})
        self.assertEqual(set(data), {"question"})
        self.assertEqual(set(data["question"]), {"question", "choices"})
        self.assertNotIn("gold_answer", json.dumps(payload("solve", data, Config())))

    def test_json_mode_is_explicit_and_does_not_change_prompt(self):
        data = stage_input("solve", self.item, {})
        plain = payload("solve", data, Config())
        structured = payload("solve", data, Config(response_format="json_object"))
        self.assertEqual(structured.pop("response_format"), {"type": "json_object"})
        self.assertEqual(plain, structured)

    def test_unsupported_response_format_rejected(self):
        with self.assertRaises(ValueError):
            Config(response_format="json_schema")

    def test_wrong_answer_stops_without_repair(self):
        outputs = deepcopy(OUTPUTS)
        outputs["solve"]["answer"] = "B"
        client = FakeClient(outputs)
        result = self.run_pipeline(client)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(len(client.calls), 1)

    def test_semantic_rejection_stops_annotation(self):
        outputs = deepcopy(OUTPUTS)
        outputs["review_solution"].update(
            decision="reject", issues=["wrong calculation"]
        )
        outputs["review_solution"]["checks"]["intermediate_correct"] = False
        client = FakeClient(outputs)
        result = self.run_pipeline(client)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(len(client.calls), 2)

    def test_accept_with_unresolved_issues_is_invalid(self):
        value = deepcopy(REVIEW)
        value["issues"] = ["uncertain"]
        with self.assertRaises(InvalidOutput):
            validate_review(value, "review_solution")

    def test_accept_with_missing_check_is_invalid(self):
        value = deepcopy(REVIEW)
        del value["checks"]["premises_complete"]
        with self.assertRaises(InvalidOutput):
            validate_review(value, "review_solution")

    def test_truncated_response_never_accepted(self):
        result = self.run_pipeline(FakeClient(finish_reason="length"))
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertEqual(result["attempt_count"], 1)

    def test_unknown_source_quote_rejected(self):
        value = {"nodes": deepcopy(NODES)}
        value["nodes"][0]["source_quote"] = "invented statement"
        with self.assertRaises(InvalidOutput):
            validate_nodes(value, QUESTION, RATIONALE)

    def test_boolean_node_id_rejected(self):
        value = {"nodes": deepcopy(NODES)}
        value["nodes"][0]["node_id"] = True
        with self.assertRaises(InvalidOutput):
            validate_nodes(value, QUESTION, RATIONALE)

    def test_self_future_and_unknown_parents_rejected(self):
        for bad in (4, 5, 999, True):
            value = deepcopy(PARENTS)
            value["parents"][3]["parents"] = [bad]
            with self.subTest(bad=bad), self.assertRaises(InvalidOutput):
                validate_parents(value, NODES)

    def test_duplicate_parent_rejected(self):
        value = deepcopy(PARENTS)
        value["parents"][3]["parents"] = [1, 1, 2, 3]
        with self.assertRaises(InvalidOutput):
            validate_parents(value, NODES)

    def test_dead_end_rejected(self):
        value = deepcopy(PARENTS)
        value["parents"][3]["parents"] = [1, 3]
        with self.assertRaises(InvalidOutput):
            validate_parents(value, NODES)

    def test_empty_justification_rejected(self):
        value = deepcopy(JUSTIFICATIONS)
        value["justifications"][0]["text"] = ""
        with self.assertRaises(InvalidOutput):
            validate_justifications(value, NODES)

    def test_invalid_json_no_substring_salvage(self):
        for content in (
            'preface {"answer":"A"}',
            "[]",
            "{invalid}",
            '{"a":1} trailing',
            '{"a":1,"a":2}',
            '{"a":NaN}',
        ):
            with self.subTest(content=content), self.assertRaises(InvalidOutput):
                parse_object(content)

    def test_exact_json_fence_supported(self):
        self.assertEqual(parse_object('```json\n{"ok":true}\n```'), {"ok": True})

    def test_immutable_artifacts(self):
        path = self.root / "test.json"
        write_once(path, {"v": 1})
        write_once(path, {"v": 1})
        with self.assertRaises(FileExistsError):
            write_once(path, {"v": 2})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_output_tampering_detected_on_resume(self):
        self.run_pipeline()
        path = self.root / "items" / self.item["item_id"] / "solve" / "output.json"
        changed = read_json(path)
        changed["rationale"] = "tampered but nonempty"
        path.write_text(json.dumps(changed))  # Deliberate corruption fixture.
        with self.assertRaises(FileExistsError):
            self.run_pipeline()

    def test_changed_config_rejected_in_existing_run(self):
        self.run_pipeline()
        with self.assertRaises(FileExistsError):
            self.run_pipeline(config=Config(workers=1, temperature=0.5))

    def test_call_budget_pauses_not_rejects(self):
        client = FakeClient()
        result = self.run_pipeline(client, Config(workers=1, max_calls=1))
        self.assertTrue(result["paused"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["results"][0]["status"], "paused")

    def test_token_budget_prevents_call(self):
        client = FakeClient()
        result = self.run_pipeline(client, Config(workers=1, max_reserved_tokens=1))
        self.assertEqual(len(client.calls), 0)
        self.assertTrue(result["paused"])

    def test_restore_budget_uses_actual_usage_above_reservation(self):
        attempt = self.root / "items" / self.item["item_id"] / "solve/attempt-00"
        write_once(
            attempt / "request.json",
            {"payload": {}, "reserved_tokens": 100},
        )
        write_once(
            attempt / "response.json",
            {"body": {"usage": {"total_tokens": 150}}},
        )
        runner = Pipeline(
            self.root,
            Config(workers=1, max_reserved_tokens=1_000),
            FakeClient(),
        )
        runner._restore_budget()
        self.assertEqual((runner.calls, runner.reserved_tokens), (1, 150))
        self.assertFalse(runner._stop.is_set())

    def test_cached_overage_cannot_bypass_global_budget_on_resume(self):
        class OverReportingClient(FakeClient):
            def complete(self, request):
                response = super().complete(request)
                response["usage"]["total_tokens"] = 100_000
                return response

        config = Config(workers=1, max_reserved_tokens=50_000)
        first_client = OverReportingClient()
        first = self.run_pipeline(first_client, config)
        self.assertTrue(first["global_stop"])
        self.assertEqual(first["reserved_tokens"], 100_000)
        self.assertEqual(len(first_client.calls), 1)

        resumed_client = OverReportingClient()
        resumed = self.run_pipeline(resumed_client, config)
        self.assertTrue(resumed["global_stop"])
        self.assertEqual(resumed["reserved_tokens"], 100_000)
        self.assertEqual(len(resumed_client.calls), 0)

    def test_uncertain_timeout_is_not_retried(self):
        client = FakeClient(failure=CallFailure("uncertain_remote_state"))
        self.run_pipeline(client)
        self.run_pipeline(client)
        self.assertEqual(len(client.calls), 1)

    def test_isolation_continues_other_item_without_retry(self):
        client = FakeClient(failure=CallFailure("uncertain_remote_state"))
        runner = Pipeline(
            self.root, Config(workers=1), client, isolate_uncertain_failures=True
        )
        first = runner.process(self.item)
        self.assertEqual(first["status"], "paused")
        self.assertFalse(runner._stop.is_set())
        self.assertEqual(runner.process(self.item)["status"], "paused")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(runner._new_uncertain_failures, 1)
        other = dict(self.item, item_id="b" * 20)
        runner.client = FakeClient()
        self.assertEqual(runner.process(other)["status"], "model_accepted")

    def test_isolation_circuit_breaker_after_three_new_errors(self):
        client = FakeClient(failure=CallFailure("uncertain_remote_state"))
        runner = Pipeline(
            self.root, Config(workers=1), client, isolate_uncertain_failures=True
        )
        for index in range(4):
            runner.process(dict(self.item, item_id=f"{index:020x}"))
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(runner._stop.is_set())

    def test_isolation_does_not_hide_pending_items(self):
        result = self.run_pipeline(
            FakeClient(failure=CallFailure("uncertain_remote_state")),
            isolate_uncertain_failures=True,
        )
        self.assertTrue(result["paused"])
        self.assertFalse(result["global_stop"])
        self.assertEqual(result["new_uncertain_failures"], 1)

    def test_isolation_still_stops_auth_and_budget_failures(self):
        client = FakeClient(failure=CallFailure("authentication", 401))
        result = self.run_pipeline(client, isolate_uncertain_failures=True)
        self.assertTrue(result["global_stop"])
        self.assertEqual(len(client.calls), 1)
        runner = Pipeline(
            self.root / "budget",
            Config(max_calls=1),
            FakeClient(),
            isolate_uncertain_failures=True,
        )
        self.assertEqual(runner.process(self.item)["status"], "paused")
        self.assertTrue(runner._stop.is_set())

    def test_auth_failure_requires_explicit_safe_retry(self):
        bad = FakeClient(failure=CallFailure("authentication", 401))
        self.run_pipeline(bad)
        good = FakeClient()
        result = self.run_pipeline(good)
        self.assertEqual(len(good.calls), 0)
        result = self.run_pipeline(good, retry_safe_failures=True)
        self.assertEqual(result["results"][0]["status"], "model_accepted")

    def test_resilient_recovers_cached_unknown_without_overwriting(self):
        failed = FakeClient(failure=CallFailure("uncertain_remote_state"))
        self.run_pipeline(failed)
        error_file = (
            self.root / "items" / self.item["item_id"] / "solve/attempt-00/error.json"
        )
        original = error_file.read_bytes()
        good = FakeClient()
        runner = Pipeline(self.root, Config(workers=1), good, resilient=True)
        with patch.object(runner._stop, "wait", return_value=False) as wait:
            result = runner.run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        self.assertEqual(error_file.read_bytes(), original)
        self.assertEqual(result["attempt_count"], 7)
        wait.assert_called_once_with(10)
        self.assertTrue(
            (error_file.parent.parent / "attempt-01/response.json").exists()
        )

    def test_resilient_exhausts_per_stage_not_whole_batch_or_resume_budget(self):
        client = FakeClient(failure=CallFailure("uncertain_remote_state"))
        runner = Pipeline(self.root, Config(workers=1), client, resilient=True)
        with patch.object(runner._stop, "wait", return_value=False) as wait:
            result = runner.run()
        self.assertFalse(result["global_stop"])
        self.assertTrue(result["paused"])
        self.assertEqual([x.args[0] for x in wait.call_args_list], [10, 30, 60])
        self.assertEqual(len(client.calls), 4)
        resumed = Pipeline(self.root, Config(workers=1), client, resilient=True)
        self.assertTrue(resumed.run()["paused"])
        self.assertEqual(len(client.calls), 4)
        resumed.client = FakeClient()
        self.assertEqual(
            resumed.process(dict(self.item, item_id="f" * 20))["status"],
            "model_accepted",
        )

    def test_resilient_retries_rate_limit_and_uses_first_returned_response(self):
        good = FakeClient()
        runner = Pipeline(self.root, Config(workers=1), good, resilient=True)
        original = good.complete
        count = 0

        def flaky(request):
            nonlocal count
            count += 1
            if count <= 2:
                raise CallFailure("rate_limit", 429)
            return original(request)

        with (
            patch.object(good, "complete", side_effect=flaky),
            patch.object(runner._stop, "wait", return_value=False),
        ):
            self.assertEqual(runner.run()["results"][0]["status"], "model_accepted")
        self.assertEqual(count, 8)

    def test_resilient_does_not_retry_semantic_or_truncated_output(self):
        client = FakeClient(finish_reason="length")
        result = self.run_pipeline(client, resilient=True)
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertEqual(len(client.calls), 1)

    def test_resilient_authentication_still_global_stop(self):
        client = FakeClient(failure=CallFailure("authentication", 401))
        result = self.run_pipeline(client, resilient=True)
        self.assertTrue(result["global_stop"])
        self.assertEqual(len(client.calls), 1)

    def test_resilient_retry_respects_restored_budget(self):
        client = FakeClient(failure=CallFailure("uncertain_remote_state"))
        runner = Pipeline(
            self.root, Config(workers=1, max_calls=1), client, resilient=True
        )
        with patch.object(runner._stop, "wait", return_value=False):
            result = runner.run()
        self.assertTrue(result["global_stop"])
        self.assertEqual(len(client.calls), 1)

    def test_resilient_recovers_orphan_without_changing_request(self):
        runner = Pipeline(self.root, Config(workers=1), FakeClient(), resilient=True)
        request = payload("solve", stage_input("solve", self.item, {}), runner.config)
        directory = self.root / "items" / self.item["item_id"] / "solve"
        runner._reserve(directory / "attempt-00", request)
        saved = (directory / "attempt-00/request.json").read_bytes()
        with patch.object(runner._stop, "wait", return_value=False):
            result = runner.run()
        self.assertEqual(result["attempt_count"], 7)
        self.assertEqual((directory / "attempt-00/request.json").read_bytes(), saved)
        self.assertTrue((directory / "attempt-01/response.json").exists())

    def test_transport_partial_response_is_safe_retryable_error(self):
        from http.client import IncompleteRead

        client = APIClient(Config(), "fake-secret-key")
        with patch("dag_builder.client.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.side_effect = IncompleteRead(
                b"fake-secret-key"
            )
            with self.assertRaises(CallFailure) as caught:
                client.complete({})
        self.assertEqual(caught.exception.category, "uncertain_remote_state")
        self.assertEqual(caught.exception.transport_kind, "IncompleteRead")
        self.assertNotIn("fake-secret-key", str(caught.exception))

    def test_rate_limit_retry_bounded(self):
        client = FakeClient(failure=CallFailure("rate_limit", 429))
        with patch("dag_builder.pipeline.time.sleep"):
            result = self.run_pipeline(client)
        self.assertTrue(result["paused"])
        self.assertEqual(len(client.calls), 3)

    def test_no_release_without_human_approval(self):
        self.run_pipeline()
        write_once(self.root / "human.json", [])
        with self.assertRaises(InvalidOutput):
            release(self.root, self.root / "human.json")

    def test_release_binds_human_review_to_exact_dag(self):
        self.run_pipeline()
        dag = read_json(self.root / "items" / self.item["item_id"] / "dag.json")
        review = {
            "item_id": self.item["item_id"],
            "decision": "accept",
            "reviewer": "test-only",
            "reason": "fixture approval",
            "dag_sha256": digest(dag),
        }
        write_once(self.root / "human.json", [review])
        target = release(self.root, self.root / "human.json")
        self.assertEqual(read_json(target / "manifest.json")["released_count"], 1)

    def test_stale_human_review_rejected(self):
        self.run_pipeline()
        write_once(
            self.root / "human.json",
            [
                {
                    "item_id": self.item["item_id"],
                    "decision": "accept",
                    "reviewer": "test",
                    "reason": "fixture",
                    "dag_sha256": "outdated",
                }
            ],
        )
        with self.assertRaises(InvalidOutput):
            release(self.root, self.root / "human.json")

    def test_export_separates_answer_and_metadata(self):
        self.run_pipeline()
        dag = read_json(self.root / "items" / self.item["item_id"] / "dag.json")
        one, two = (
            trajectory(dag, "node_only"),
            trajectory(dag, "justification_plus_node"),
        )
        self.assertEqual(len(one["steps"]), 4)
        self.assertEqual(one["answer"], "A. 3 m/s")
        self.assertNotEqual(one["trajectory_id"], two["trajectory_id"])
        self.assertNotIn("gold_answer", one["question"])
        self.assertNotIn("review", str(one))

    def test_reports_escape_model_html(self):
        item_path = self.root / "items" / self.item["item_id"] / "solve" / "output.json"
        write_once(item_path, {"rationale": "<script>alert(1)</script>"})
        target = render(self.root)
        content = (target / "review.html").read_text()
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_reports_count_all_selected(self):
        summary = overview(self.root)
        self.assertEqual(summary["counts"], {"not_started": 1})
        self.assertEqual(summary["selected"], 1)

    def test_report_title_matches_dataset(self):
        mmlu_report = render(self.root)
        self.assertIn("MMLU DAG 构造试点审查", (mmlu_report / "review.html").read_text())

        gsm_item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "f" * 40,
            "main",
            "test",
        )[0]
        gsm_root = self.root / "gsm8k-report"
        write_once(gsm_root / "items.json", [gsm_item])
        write_once(gsm_root / "selection.json", {"selected_ids": [gsm_item["item_id"]]})
        gsm_report = render(gsm_root)
        content = (gsm_report / "review.html").read_text()
        self.assertIn("GSM8K DAG 构造审查", content)
        self.assertNotIn("MMLU DAG 构造试点审查", content)

    def test_credential_file_permissions(self):
        path = self.root / "credential"
        path.write_text("fake-test-value\n")
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            load_key("ABSENT_TEST_ENV", path)
        path.chmod(0o600)
        self.assertEqual(load_key("ABSENT_TEST_ENV", path), "fake-test-value")

    def test_missing_credential_error_is_safe(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(CallFailure) as caught,
        ):
            load_key("JUDGE_API_KEY")
        self.assertEqual(caught.exception.category, "credential_missing_or_malformed")

    def test_symlink_credential_rejected(self):
        path = self.root / "credential"
        path.write_text("fake-test-value")
        path.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(PermissionError):
            load_key("ABSENT_TEST_ENV", link)

    def test_redaction_nested(self):
        client = APIClient(Config(), "fake-secret-key")
        self.assertEqual(
            client.redact({"x": ["echo fake-secret-key"]}), {"x": ["echo [REDACTED]"]}
        )

    def test_cross_host_redirect_disabled(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://elsewhere.invalid"
            )
        )

    def test_non_https_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            Config(base_url="http://example.invalid/v1")

    def test_no_credentials_in_endpoint(self):
        with self.assertRaises(ValueError):
            Config(base_url="https://user:secret@example.invalid/v1")

    def test_sampling_reproducible_and_not_replenished(self):
        items = [
            dict(self.item, item_id=f"{i:020x}", question=f"Question {i}")
            for i in range(50)
        ]
        first, manifest = select(items, 30, 123)
        second, _ = select(list(reversed(items)), 30, 123)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 30)
        self.assertEqual(len(set(manifest["selected_ids"])), 30)

    def test_visual_and_duplicate_filters_recorded(self):
        items = [
            self.item,
            dict(self.item, item_id="b" * 20),
            dict(self.item, item_id="c" * 20, question="Refer to the diagram"),
        ]
        chosen, manifest = select(items, 1, 1)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(len(manifest["excluded"]), 2)

    def test_input_identifier_cannot_escape_run(self):
        root = self.root / "unsafe"
        write_once(root / "items.json", [dict(self.item, item_id="../../escape")])
        write_once(root / "selection.json", {"selected_ids": ["../../escape"]})
        with self.assertRaises(ValueError):
            Pipeline(root, Config(), FakeClient()).run()

    def test_exclusive_run_lock(self):
        with (
            run_lock(self.root),
            self.assertRaises(BlockingIOError),
            run_lock(self.root),
        ):
            pass

    def test_structural_failure_preserves_raw_response(self):
        outputs = deepcopy(OUTPUTS)
        outputs["dependencies"]["parents"][3]["parents"] = [1]
        result = self.run_pipeline(FakeClient(outputs))
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertTrue(
            (
                self.root
                / "items"
                / self.item["item_id"]
                / "dependencies/attempt-00/response.json"
            ).exists()
        )

    def test_parallel_workers_respect_global_budget(self):
        root = self.root / "parallel"
        items = [dict(self.item, item_id=f"{i:020x}") for i in range(4)]
        write_once(root / "items.json", items)
        write_once(
            root / "selection.json", {"selected_ids": [i["item_id"] for i in items]}
        )
        client = FakeClient()
        result = Pipeline(root, Config(workers=2, max_calls=2), client).run()
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(result["paused"])

    def test_runtime_workers_override_does_not_change_frozen_config(self):
        runner = Pipeline(
            self.root,
            Config(workers=1),
            FakeClient(),
            runtime_workers=6,
        )
        result = runner.run(through="solve")
        self.assertEqual(result["runtime_workers"], 6)
        self.assertEqual(result["execution_policy"]["configured_workers"], 1)
        self.assertEqual(result["execution_policy"]["runtime_workers"], 6)
        self.assertEqual(read_json(self.root / "run_config.json")["workers"], 1)

    def test_runtime_workers_obey_task_limit(self):
        with self.assertRaises(ValueError):
            Pipeline(
                self.root,
                Config(workers=1),
                FakeClient(),
                runtime_workers=33,
            )

    def test_runtime_workers_allow_pressure_test_ceiling(self):
        runner = Pipeline(
            self.root,
            Config(workers=1),
            FakeClient(),
            runtime_workers=24,
        )
        self.assertEqual(runner.runtime_workers, 24)

    def test_stage_barrier_defers_annotation(self):
        client = FakeClient()
        result = Pipeline(self.root, Config(workers=1), client).run(through="solve")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["results"][0]["status"], "stage_complete")
        Pipeline(self.root, Config(workers=1), client).run(through="review_solution")
        self.assertEqual(len(client.calls), 2)
        self.run_pipeline(client)
        self.assertEqual(len(client.calls), 6)

    def test_missing_usage_is_not_zero_cost(self):
        path = self.root / "items" / self.item["item_id"] / "solve" / "attempt-00"
        write_once(path / "request.json", {})
        write_once(path / "response.json", {"body": {"model": "test-only"}})
        self.assertFalse(overview(self.root)["reported_usage_complete"])

    def test_gsm8k_normalization_extracts_final_answer_and_keeps_raw_solution(self):
        items = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "b" * 40,
            "main",
            "test",
        )
        self.assertEqual(items[0]["task_type"], "gsm8k")
        self.assertEqual(items[0]["gold_answer"], "4")
        self.assertEqual(items[0]["raw_answer"], "2 + 2 = 4\n#### 4")
        self.assertNotIn("choices", items[0])

    def test_gsm8k_answer_normalization_is_exact_and_conservative(self):
        self.assertEqual(extract_gsm8k_answer("work\n#### 2,125"), "2,125")
        self.assertTrue(numeric_answers_equivalent("2,125", "2125.0"))
        self.assertTrue(numeric_answers_equivalent("\\boxed{3/2}", "1.5"))
        self.assertFalse(numeric_answers_equivalent("18 dollars", "18"))

    def test_gsm8k_canonical_rationale_removes_only_calculator_markup(self):
        raw = "Each group has <<6*12=72>>72 items.\n#### 72"
        self.assertEqual(
            canonicalize_gsm8k_rationale(raw),
            "Each group has 6*12=72 items.",
        )

    def test_gsm8k_solve_request_contains_question_but_not_gold_answer(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "c" * 40,
            "main",
            "test",
        )[0]
        data = stage_input("solve", item, {})
        request = payload(
            "solve", data, Config(task_type="gsm8k", prompt_version="gsm8k-v1")
        )
        self.assertEqual(
            data["question"], {"task_type": "gsm8k", "question": "What is 2 + 2?"}
        )
        self.assertNotIn("gold_answer", json.dumps(request))
        self.assertNotIn("#### 4", json.dumps(request))

    def test_gsm8k_conditioned_solve_receives_only_reference_answer(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "c" * 40,
            "main",
            "test",
        )[0]
        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="answer_conditioned_generation",
        )
        data = stage_input("solve", item, {}, config.solution_source)
        request = payload("solve", data, config)
        serialized = json.dumps(request)
        self.assertEqual(data["reference_answer"], "4")
        self.assertNotIn("2 + 2 = 4", serialized)
        self.assertIn("Do not return an answer field", serialized)

    def test_gsm8k_conditioned_output_records_dataset_answer_provenance(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "d" * 40,
            "main",
            "test",
        )[0]
        root = self.root / "conditioned"
        write_once(root / "items.json", [item])
        write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})

        class ConditionedClient:
            def complete(self, request):
                return {
                    "model": "test-fixture-only",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({
                            "rationale": "Adding 2 and 2 gives 4.",
                            "estimated_difficulty": {
                                "level": "low",
                                "reason": "one addition",
                            },
                        })},
                    }],
                    "usage": {"total_tokens": 10},
                }

        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="answer_conditioned_generation",
            workers=1,
        )
        Pipeline(root, config, ConditionedClient()).run(through="solve")
        output = read_json(root / "items" / item["item_id"] / "solve" / "output.json")
        self.assertEqual(output["answer"], "4")
        self.assertEqual(output["answer_source"], "dataset_reference")
        self.assertEqual(output["solution_source"], "answer_conditioned_generation")

    def test_gsm8k_diagnostic_repair_request_keeps_draft_and_gold_separate(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "f" * 40,
            "main",
            "test",
        )[0]
        item.update(
            repair_draft_rationale="The old draft repeats 2 + 2 several times.",
            repair_diagnosis="dependencies disconnected",
        )
        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="diagnostic_repair_generation",
        )
        data = stage_input("solve", item, {}, config.solution_source)
        request = payload("solve", data, config)
        serialized = json.dumps(request)
        self.assertEqual(data["reference_answer"], "4")
        self.assertEqual(data["draft_rationale"], item["repair_draft_rationale"])
        self.assertEqual(data["repair_diagnosis"], item["repair_diagnosis"])
        self.assertIn("Do not return an answer field", serialized)
        self.assertNotIn(item["raw_answer"], serialized)

    def test_gsm8k_diagnostic_repair_records_dataset_answer_provenance(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "0" * 40,
            "main",
            "test",
        )[0]
        item.update(
            repair_draft_rationale="The old draft is not usable.",
            repair_diagnosis="source quote must occur verbatim",
        )
        root = self.root / "diagnostic"
        write_once(root / "items.json", [item])
        write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})

        testcase = self

        class DiagnosticClient:
            def complete(self, request):
                testcase.assertEqual(
                    request["messages"][0]["content"],
                    prompt(
                        "solve",
                        "gsm8k-v1",
                        "gsm8k",
                        "diagnostic_repair_generation",
                    ),
                )
                return {
                    "model": "test-fixture-only",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({
                            "rationale": "Adding 2 and 2 gives 4.",
                            "estimated_difficulty": {
                                "level": "low",
                                "reason": "one addition",
                            },
                        })},
                    }],
                    "usage": {"total_tokens": 10},
                }

        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="diagnostic_repair_generation",
            workers=1,
        )
        Pipeline(root, config, DiagnosticClient()).run(through="solve")
        output = read_json(root / "items" / item["item_id"] / "solve" / "output.json")
        self.assertEqual(output["answer"], "4")
        self.assertEqual(output["answer_source"], "dataset_reference")
        self.assertEqual(output["solution_source"], "diagnostic_repair_generation")

    def test_gsm8k_official_rationale_skips_solve_api(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "e" * 40,
            "main",
            "test",
        )[0]
        root = self.root / "official"
        write_once(root / "items.json", [item])
        write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})
        client = FakeClient()
        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="official_rationale",
            workers=1,
        )
        result = Pipeline(root, config, client).run(through="solve")
        output = read_json(root / "items" / item["item_id"] / "solve" / "output.json")
        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(output["rationale"], "2 + 2 = 4")
        self.assertEqual(output["answer"], "4")
        self.assertEqual(output["solution_source"], "official_rationale")

    def test_gsm8k_canonical_official_rationale_preserves_raw_provenance(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "<<2 + 2=4>>4\n#### 4"}],
            "1" * 40,
            "main",
            "test",
        )[0]
        item["canonical_rationale"] = "2 + 2=4"
        root = self.root / "canonical-official"
        write_once(root / "items.json", [item])
        write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})
        config = Config(
            task_type="gsm8k",
            prompt_version="gsm8k-v1",
            solution_source="canonical_official_rationale",
            workers=1,
        )
        Pipeline(root, config, FakeClient()).run(through="solve")
        input_record = read_json(root / "items" / item["item_id"] / "solve" / "input.json")
        output = read_json(root / "items" / item["item_id"] / "solve" / "output.json")
        self.assertEqual(input_record["input"]["raw_answer_sha256"], digest(item["raw_answer"]))
        self.assertEqual(
            input_record["input"]["canonical_rationale_sha256"],
            digest(item["canonical_rationale"]),
        )
        self.assertEqual(output["rationale"], item["canonical_rationale"])
        self.assertEqual(output["answer"], "4")
        self.assertEqual(output["solution_source"], "canonical_official_rationale")

    def test_non_gsm8k_rejects_alternate_solution_sources(self):
        with self.assertRaises(ValueError):
            Config(solution_source="official_rationale")

    def test_gsm8k_numeric_solution_can_match_gold_without_string_identity(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "d" * 40,
            "main",
            "test",
        )[0]
        self.assertTrue(answer_matches(item, {"answer": "4.0"}))
        self.assertFalse(answer_matches(item, {"answer": "5"}))

    def test_gsm8k_solution_schema_requires_numeric_answer_field(self):
        value = {
            "answer": "4 dollars",
            "rationale": "Two plus two equals four.",
            "estimated_difficulty": {"level": "low", "reason": "one addition"},
        }
        with self.assertRaises(InvalidOutput):
            validate_math_solution(value)

    def test_gsm8k_full_pipeline_and_export(self):
        item = normalize_gsm8k(
            [{"question": "What is 2 + 2?", "answer": "2 + 2 = 4\n#### 4"}],
            "e" * 40,
            "main",
            "test",
        )[0]
        root = self.root / "gsm8k"
        write_once(root / "items.json", [item])
        write_once(root / "selection.json", {"selected_ids": [item["item_id"]]})

        outputs = {
            "solve": {
                "answer": "4",
                "rationale": "Adding 2 and 2 gives 4. The final answer is 4.",
                "estimated_difficulty": {"level": "low", "reason": "one addition"},
            },
            "review_solution": REVIEW,
            "atomize": {
                "nodes": [
                    {
                        "node_id": 1,
                        "kind": "given",
                        "statement": "The problem asks for the sum of 2 and 2.",
                        "source_field": "question",
                        "source_quote": "What is 2 + 2?",
                    },
                    {
                        "node_id": 2,
                        "kind": "derived",
                        "statement": "Adding 2 and 2 gives 4.",
                        "source_field": "solution",
                        "source_quote": "Adding 2 and 2 gives 4.",
                    },
                    {
                        "node_id": 3,
                        "kind": "answer",
                        "statement": "The final numeric answer is 4.",
                        "source_field": "solution",
                        "source_quote": "The final answer is 4.",
                    },
                ]
            },
            "dependencies": {
                "parents": [
                    {"node_id": 1, "parents": []},
                    {"node_id": 2, "parents": [1]},
                    {"node_id": 3, "parents": [2]},
                ]
            },
            "justify": {
                "justifications": [
                    {"node_id": 1, "text": "The question supplies the two addends."},
                    {"node_id": 2, "text": "The addition of 2 and 2 produces 4."},
                    {
                        "node_id": 3,
                        "text": "The preceding calculation establishes the final answer.",
                    },
                ]
            },
            "review_dag": DAG_REVIEW,
        }

        class GSM8KFakeClient:
            def __init__(self):
                self.calls = []

            def complete(self, request):
                self.calls.append(request)
                stage = next(
                    stage_name
                    for stage_name in STAGES
                    if request["messages"][0]["content"]
                    == prompt(stage_name, "gsm8k-v1", "gsm8k")
                )
                return {
                    "model": "test-fixture-only",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(outputs[stage])},
                        }
                    ],
                    "usage": {"total_tokens": 100},
                }

        client = GSM8KFakeClient()
        result = Pipeline(
            root,
            Config(task_type="gsm8k", prompt_version="gsm8k-v1", workers=1),
            client,
        ).run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 6)
        dag = read_json(root / "items" / item["item_id"] / "dag.json")
        self.assertEqual(trajectory(dag, "node_only")["answer"], "4")


if __name__ == "__main__":
    unittest.main()
