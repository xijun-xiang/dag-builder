"""Offline repair tests: synthetic fixtures, no credentials and no API calls."""

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.client import CallFailure
from dag_builder.config import Config
from dag_builder.continuation import prepare_continuation
from dag_builder.export import release, trajectory
from dag_builder.pipeline import Pipeline
from dag_builder.repair import RepairPipeline
from dag_builder.repair_graph import (
    EXTRA_REVIEW_CHECKS,
    topological_repair,
    validate_proposal,
    validate_repair_review,
)
from dag_builder.repair_source import prepare_repair
from dag_builder.report import overview, render
from dag_builder.schemas import InvalidOutput
from dag_builder.stages import REPAIR_STAGES, prompt, stage_input
from dag_builder.storage import digest, read_json, write_once
from test_builder import DAG_REVIEW, JUSTIFICATIONS, PARENTS
from test_gpqa import ReferenceClient, fixture, normalize_gpqa
from test_gpqa import config as initial_config


def config(**kwargs):
    return Config(
        task_type="gpqa", prompt_version="gpqa-repair-v1", workers=1, **kwargs
    )


def accepted_review():
    value = deepcopy(DAG_REVIEW)
    value["checks"].update(dict.fromkeys(EXTRA_REVIEW_CHECKS, True))
    return value


class RepairClient:
    def __init__(self, item):
        self.calls = []
        candidate = {
            "nodes": ReferenceClient(item).outputs["atomize"]["nodes"],
            "parents": deepcopy(PARENTS["parents"]),
        }
        self.outputs = {
            "repair": {
                "decision": "candidate",
                "category": "representation_error",
                "original_review": "not_applicable",
                "reason": "Fixture repair proposal.",
                "changes": ["Fixture-only change audit; no production data."],
                "candidate": candidate,
            },
            "justify": deepcopy(JUSTIFICATIONS),
            "review_repair": accepted_review(),
        }

    def complete(self, request):
        self.calls.append(request)
        stage = next(
            s
            for s in REPAIR_STAGES
            if request["messages"][0]["content"] == prompt(s, "gpqa-repair-v1", "gpqa")
        )
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self.outputs[stage])},
                }
            ],
            "usage": {"total_tokens": 100},
        }


class RepairTests(unittest.TestCase):
    def test_six_worker_continuation_preserves_cached_response_and_budget(self):
        self.prepare()
        original = config(max_calls=10)
        write_once(self.root / "config.json", original.to_dict())
        runner = RepairPipeline(self.root, original, self.client, resilient=True)
        runner._candidate(self.item, self.reference, self.baseline())
        self.assertEqual(len(self.client.calls), 1)
        write_once(
            self.root / "items" / ("f" * 20) / "repair/attempt-00/request.json",
            {"reserved_tokens": 800},
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary).resolve() / "continuation"
            result = prepare_continuation(self.root, destination)
            self.assertEqual(result["workers"], 6)
            self.assertEqual(result["config"]["max_calls"], 9)
            self.assertEqual(
                result["config"]["max_reserved_tokens"],
                original.max_reserved_tokens - 800,
            )
            run = RepairPipeline(
                destination,
                Config.load(destination / "config.json"),
                self.client,
                resilient=True,
            ).run()
            self.assertEqual(run["attempt_count"], 3)
            self.assertEqual(len(self.client.calls), 3)
            self.assertEqual(run["results"][0]["status"], "repaired_model_accepted")
            with self.assertRaises(FileExistsError):
                prepare_continuation(self.root, destination)

    def test_continuation_does_not_select_completed_semantic_results(self):
        self.prepare()
        write_once(self.root / "config.json", config().to_dict())
        self.run_repair()
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "no unfinished items"),
        ):
            prepare_continuation(self.root, Path(temporary).resolve() / "continuation")

    def test_worker_bound_remains_explicit(self):
        self.assertEqual(Config(workers=6).workers, 6)
        with self.assertRaises(ValueError):
            Config(workers=7)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.item = normalize_gpqa([fixture()])[0]
        self.client = RepairClient(self.item)
        self.reference = stage_input("review_solution", self.item, {})
        self.candidate = deepcopy(self.client.outputs["repair"]["candidate"])

    def tearDown(self):
        self.tmp.cleanup()

    def baseline(
        self,
        reason="nodes not connected to the answer; do not invent edges to close them",
    ):
        return {
            "result": {
                "item_id": self.item["item_id"],
                "status": "needs_review",
                "stage": "dependencies",
                "reason": reason,
            },
            "stage_outputs": {"atomize": {"nodes": deepcopy(self.candidate["nodes"])}},
            "failed_completions": {
                "dependencies": {
                    "parsed": {"parents": deepcopy(self.candidate["parents"])}
                }
            },
        }

    def prepare(self, baseline=None):
        baseline = baseline or self.baseline()
        write_once(self.root / "items.json", [self.item])
        write_once(
            self.root / "selection.json",
            {
                "protocol": "gpqa-repair-v1",
                "semantic_repair_round_limit": 1,
                "selected_ids": [self.item["item_id"]],
                "selected_item_sha256": {self.item["item_id"]: digest(self.item)},
                "baseline_sha256": {self.item["item_id"]: digest(baseline)},
            },
        )
        write_once(
            self.root / "items" / self.item["item_id"] / "baseline.json", baseline
        )

    def run_repair(self, **kwargs):
        return RepairPipeline(
            self.root, kwargs.pop("config", config()), self.client, **kwargs
        ).run()

    def forward_reference(self):
        # Move the knowledge premise (old 3) after calculation (old 4).
        order = [1, 2, 4, 3, 5]
        renumber = {old: new for new, old in enumerate(order, 1)}
        nodes = [
            dict(self.candidate["nodes"][old - 1], node_id=renumber[old])
            for old in order
        ]
        parents = [
            {
                "node_id": renumber[old],
                "parents": [
                    renumber[p] for p in self.candidate["parents"][old - 1]["parents"]
                ],
            }
            for old in order
        ]
        return nodes, parents

    def test_topology_preserves_all_statements_and_edges(self):
        nodes, parents = self.forward_reference()
        before = deepcopy((nodes, parents))
        repaired, mapping = topological_repair(nodes, parents, self.reference)
        self.assertEqual((nodes, parents), before)
        self.assertEqual(repaired, self.candidate)
        self.assertEqual(mapping[2], {"old_id": 3, "new_id": 4})

    def test_topology_never_repairs_cycles_unknown_self_or_duplicate_edges(self):
        for bad in ([4], [999], [1, 1], [True]):
            parents = deepcopy(self.candidate["parents"])
            parents[3]["parents"] = bad
            with self.subTest(bad=bad), self.assertRaises(InvalidOutput):
                topological_repair(self.candidate["nodes"], parents, self.reference)
        parents = deepcopy(self.candidate["parents"])
        parents[0]["parents"] = [4]
        with self.assertRaisesRegex(InvalidOutput, "cycle"):
            topological_repair(self.candidate["nodes"], parents, self.reference)

    def test_topology_never_prunes_disconnected_nodes(self):
        parents = deepcopy(self.candidate["parents"])
        parents[3]["parents"] = [1, 2]
        with self.assertRaisesRegex(InvalidOutput, "not connected"):
            topological_repair(self.candidate["nodes"], parents, self.reference)

    def test_proposal_forbids_source_replacement_and_unresolved_candidate(self):
        proposal = deepcopy(self.client.outputs["repair"])
        proposal["official_explanation"] = "new invented source"
        with self.assertRaisesRegex(InvalidOutput, "unexpected"):
            validate_proposal(proposal, self.reference)
        proposal = deepcopy(self.client.outputs["repair"])
        proposal["category"] = "source_gap"
        with self.assertRaisesRegex(InvalidOutput, "unresolved"):
            validate_proposal(proposal, self.reference)
        proposal["decision"] = "unrepairable"
        with self.assertRaisesRegex(InvalidOutput, "must not include"):
            validate_proposal(proposal, self.reference)

    def test_all_nine_review_checks_required(self):
        for key in EXTRA_REVIEW_CHECKS:
            review = accepted_review()
            review["checks"][key] = False
            with self.subTest(key=key), self.assertRaises(InvalidOutput):
                validate_repair_review(review)
        with self.assertRaises(InvalidOutput):
            validate_repair_review(deepcopy(DAG_REVIEW))

    def test_success_resume_fresh_context_and_export(self):
        baseline = self.baseline()
        baseline["result"]["reason"] = "SENTINEL_PRIOR_REJECTION"
        self.prepare(baseline)
        self.client.outputs["repair"]["reason"] = "SENTINEL_REPAIR_SELF_PRAISE"
        for _ in range(2):
            result = self.run_repair()
            self.assertEqual(result["results"][0]["status"], "repaired_model_accepted")
        self.assertEqual(len(self.client.calls), 3)
        final_input = self.client.calls[-1]["messages"][1]["content"]
        self.assertNotIn("SENTINEL", final_input)
        self.assertNotIn('"original"', final_input)
        self.assertEqual(
            json.loads(final_input)["solution"]["rationale"],
            self.item["official_explanation"],
        )
        directory = self.root / "items" / self.item["item_id"]
        dag = read_json(directory / "dag.json")
        self.assertTrue(trajectory(dag, "node_only")["variant"].startswith("repaired:"))
        write_once(
            self.root / "human.json",
            [
                {
                    "item_id": self.item["item_id"],
                    "reviewer": "fixture",
                    "reason": "fixture only",
                    "decision": "accept",
                    "dag_sha256": digest(dag),
                }
            ],
        )
        released = release(self.root, self.root / "human.json")
        self.assertEqual(read_json(released / "manifest.json")["repaired_count"], 1)
        self.assertIn("修复前原始状态", (render(self.root) / "review.html").read_text())

    def test_topological_route_skips_model_repair_but_still_audits(self):
        nodes, parents = self.forward_reference()
        baseline = self.baseline("unknown, self or future dependency")
        baseline["stage_outputs"]["atomize"]["nodes"] = nodes
        baseline["failed_completions"]["dependencies"]["parsed"]["parents"] = parents
        self.prepare(baseline)
        self.assertEqual(
            self.run_repair()["results"][0]["status"], "repaired_model_accepted"
        )
        self.assertEqual(len(self.client.calls), 2)
        self.assertFalse(
            (self.root / "items" / self.item["item_id"] / "repair").exists()
        )

    def test_source_rejection_can_be_reassessed_without_old_nodes(self):
        baseline = self.baseline()
        baseline.update(stage_outputs={}, failed_completions={})
        baseline["result"].update(stage="review_solution", status="rejected")
        self.prepare(baseline)
        self.client.outputs["repair"].update(
            category="review_error", original_review="overturned"
        )
        self.assertEqual(
            self.run_repair()["results"][0]["status"], "repaired_model_accepted"
        )

    def test_unrepairable_source_gets_no_further_calls(self):
        self.prepare()
        self.client.outputs["repair"].update(
            decision="unrepairable", category="source_gap", candidate=None, changes=[]
        )
        for _ in range(2):
            self.assertEqual(
                self.run_repair()["results"][0]["status"], "repair_unrepairable"
            )
        self.assertEqual(len(self.client.calls), 1)

    def test_malformed_semantic_candidate_is_not_resampled(self):
        self.prepare()
        self.client.outputs["repair"]["candidate"]["nodes"][0]["source_quote"] = (
            "fabricated source quote"
        )
        for _ in range(2):
            self.assertEqual(
                self.run_repair(resilient=True)["results"][0]["status"],
                "repair_needs_review",
            )
        self.assertEqual(len(self.client.calls), 1)

    def test_final_rejection_never_triggers_second_repair(self):
        self.prepare()
        self.client.outputs["review_repair"].update(
            decision="reject", issues=["fixture scientific gap"]
        )
        for _ in range(2):
            self.assertEqual(
                self.run_repair()["results"][0]["status"], "repair_rejected"
            )
        self.assertEqual(len(self.client.calls), 3)
        self.assertFalse(
            (self.root / "items" / self.item["item_id"] / "dag.json").exists()
        )

    def test_invalid_json_is_retained_without_retry_or_promotion(self):
        self.prepare()
        malformed = '{"decision":"candidate" "category":"representation_error"}'
        response = {
            "choices": [{"finish_reason": "stop", "message": {"content": malformed}}],
            "usage": {"total_tokens": 100},
        }
        with patch.object(self.client, "complete", return_value=response) as complete:
            for _ in range(2):
                result = self.run_repair(resilient=True)["results"][0]
                self.assertEqual(result["status"], "repair_needs_review")
                self.assertEqual(result["reason"], "response is not one JSON object")
            self.assertEqual(complete.call_count, 1)
        directory = self.root / "items" / self.item["item_id"]
        responses = list(directory.glob("repair/attempt-*/response.json"))
        self.assertEqual(len(responses), 1)
        self.assertEqual(read_json(responses[0])["body"], response)
        self.assertFalse((directory / "candidate.json").exists())
        self.assertFalse((directory / "dag.json").exists())

    def test_change_log_schema_failure_does_not_hide_graph_requirement(self):
        self.prepare()
        proposal = self.client.outputs["repair"]
        proposal["changes"] = [{"change": "Synthetic structured change record"}]
        # Separately leave a knowledge premise disconnected from the answer.
        proposal["candidate"]["parents"][3]["parents"] = [1, 2]
        for _ in range(2):
            result = self.run_repair(resilient=True)["results"][0]
            self.assertEqual(result["status"], "repair_needs_review")
            self.assertEqual(result["reason"], "changes must be explanatory strings")
        self.assertEqual(len(self.client.calls), 1)
        directory = self.root / "items" / self.item["item_id"]
        self.assertFalse((directory / "dag.json").exists())
        # An in-memory diagnostic, not a repaired/accepted production response:
        # correcting metadata alone must not bypass candidate validation.
        corrected_metadata = deepcopy(proposal)
        corrected_metadata["changes"] = ["Synthetic explanatory change record"]
        with self.assertRaisesRegex(InvalidOutput, "not connected"):
            validate_proposal(corrected_metadata, self.reference)
        self.assertIsInstance(proposal["changes"][0], dict)

    def test_transport_exhaustion_is_paused_not_semantic_rejection(self):
        self.prepare()
        runner = RepairPipeline(self.root, config(), self.client, resilient=True)
        with (
            patch.object(
                self.client,
                "complete",
                side_effect=CallFailure("uncertain_remote_state"),
            ) as complete,
            patch.object(runner._stop, "wait", return_value=False),
        ):
            observed = []
            result = runner.run(
                progress=lambda _: observed.append(overview(self.root)["counts"])
            )
        self.assertEqual(complete.call_count, 4)
        self.assertTrue(result["paused"])
        self.assertFalse(result["global_stop"])
        self.assertEqual(result["results"][0]["reason"], "transient_retries_exhausted")
        self.assertEqual(observed, [{"paused": 1}])
        page = (render(self.root) / "review.html").read_text()
        self.assertIn("transient_retries_exhausted", page)
        self.assertIn("pause_event", page)
        self.assertFalse(
            (self.root / "items" / self.item["item_id"] / "result.json").exists()
        )

    def test_budget_cap_stops_without_scientific_rejection(self):
        self.prepare()
        result = self.run_repair(config=config(max_calls=1))
        self.assertTrue(result["global_stop"])
        self.assertEqual(result["results"][0]["reason"], "budget_exhausted")
        self.assertEqual(len(self.client.calls), 1)

    def test_changed_baseline_fails_before_paid_request(self):
        self.prepare()
        path = self.root / "items" / self.item["item_id"] / "baseline.json"
        value = read_json(path)
        value["result"]["reason"] = "tampered"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "baseline changed"):
            self.run_repair()
        self.assertEqual(len(self.client.calls), 0)

    def test_protocol_guard(self):
        with self.assertRaises(ValueError):
            Pipeline(self.root, config(), self.client)
        with self.assertRaises(ValueError):
            RepairPipeline(self.root, initial_config(), self.client)

    def test_changed_official_explanation_fails_before_paid_request(self):
        self.prepare()
        changed = deepcopy(self.item)
        changed["official_explanation"] += " A hidden repair to the source."
        (self.root / "items.json").write_text(json.dumps([changed]))
        with self.assertRaisesRegex(ValueError, "official source changed"):
            self.run_repair()
        self.assertEqual(len(self.client.calls), 0)

    def test_source_snapshot_preserves_original_and_excludes_accepted(self):
        source, target = self.root / "source-run", self.root / "repair-run"
        original_client = ReferenceClient(self.item)
        original_client.outputs["dependencies"]["parents"][3]["parents"] = [1, 2]
        write_once(source / "items.json", [self.item])
        write_once(source / "selection.json", {"selected_ids": [self.item["item_id"]]})
        write_once(source / "config.json", initial_config().to_dict())
        initial = Pipeline(source, initial_config(), original_client).run()
        write_once(
            source / "completion.json", {"status": "processed", "result": initial}
        )

        def file_hashes():
            return {
                str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob("*")
                if p.is_file()
            }

        before = file_hashes()
        selection = prepare_repair(target, source)
        self.assertEqual(selection["selected_ids"], [self.item["item_id"]])
        self.assertEqual(before, file_hashes())
        self.assertEqual(selection, prepare_repair(target, source))
        saved = read_json(target / "items" / self.item["item_id"] / "baseline.json")
        self.assertIn("parsed", saved["failed_completions"]["dependencies"])
        with self.assertRaises(InvalidOutput):
            prepare_repair(source / "nested", source)

    def test_source_must_finish_and_cannot_be_a_second_repair_round(self):
        with self.assertRaisesRegex(InvalidOutput, "must finish"):
            prepare_repair(self.root / "new", self.root / "old")
        old = self.root / "old"
        write_once(old / "completion.json", {"status": "processed"})
        write_once(old / "config.json", config().to_dict())
        (old / ".lock").touch(mode=0o600)
        with self.assertRaisesRegex(InvalidOutput, "only first-pass"):
            prepare_repair(self.root / "new", old)

    def test_source_selection_excludes_accepted_and_transport_pending(self):
        source, target = self.root / "first-pass", self.root / "repair"
        items = normalize_gpqa([fixture("a"), fixture("b"), fixture("c")])
        write_once(source / "items.json", items)
        write_once(
            source / "selection.json",
            {"selected_ids": [item["item_id"] for item in items]},
        )
        write_once(source / "inputs_manifest.json", {"items_sha256": digest(items)})
        write_once(source / "config.json", initial_config().to_dict())
        write_once(source / "completion.json", {"status": "paused"})
        (source / ".lock").touch(mode=0o600)
        for item, status in zip(items[:2], ["needs_review", "model_accepted"]):
            write_once(
                source / "items" / item["item_id"] / "result.json",
                {"item_id": item["item_id"], "status": status, "stage": "dependencies"},
            )
        selection = prepare_repair(target, source)
        self.assertEqual(selection["selected_ids"], [items[0]["item_id"]])
        self.assertEqual(
            {row["status"] for row in selection["excluded"]},
            {"model_accepted", "unfinished_transport_or_other"},
        )

    def test_repair_cli_runs_fixture_without_legacy_stopping_flags(self):
        import sys

        from dag_builder.cli import main

        self.prepare()
        write_once(self.root / "config.json", config().to_dict())
        args = [
            "dag-builder",
            "repair",
            "--root",
            str(self.root),
            "--config",
            str(self.root / "config.json"),
        ]
        with (
            patch.object(sys, "argv", args),
            patch("dag_builder.cli.load_key", return_value="fixture-only"),
            patch("dag_builder.cli.APIClient", return_value=self.client),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(len(self.client.calls), 3)


if __name__ == "__main__":
    unittest.main()
