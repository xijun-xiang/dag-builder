"""Offline state-machine and safety tests using synthetic, non-benchmark data."""

import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from dag_builder.client import CallFailure
from dag_builder.config import Config
from dag_builder.output_normalization import diagnose, normalize, validate_bundle
from dag_builder.pipeline import Pipeline
from dag_builder.repair_loop import RevisionPipeline
from dag_builder.review_issues import (
    issue_packet,
    validate_audit,
    validate_choice_analysis,
)
from dag_builder.revision import validate_revision, validate_revision_scope
from dag_builder.revision_source import prepare_revision
from dag_builder.schemas import InvalidOutput
from dag_builder.stages import payload, prompt, stage_input
from dag_builder.storage import digest, read_json, write_once
from test_builder import JUSTIFICATIONS, PARENTS
from test_gpqa import ReferenceClient, fixture, normalize_gpqa
from test_repair import accepted_review


def config(**kwargs):
    return Config(
        task_type="gpqa",
        prompt_version="gpqa-revision-v1",
        model="deepseek-flash",
        workers=1,
        **kwargs,
    )


class QueueClient:
    def __init__(self, outputs):
        self.outputs, self.calls = outputs, []

    def complete(self, request):
        self.calls.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return {
            "model": "deepseek-flash",
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(output)}}
            ],
            "usage": {"total_tokens": 100},
        }


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "run"
        self.item = normalize_gpqa([fixture()])[0]
        self.reference = stage_input("review_solution", self.item, {})
        self.bundle = {
            "nodes": ReferenceClient(self.item).outputs["atomize"]["nodes"],
            "parents": deepcopy(PARENTS["parents"]),
            "justifications": deepcopy(JUSTIFICATIONS["justifications"]),
        }
        self.accept = accepted_review()
        self.issue = {
            "diagnosis": {
                "kind": "other",
                "missing_content": "Synthetic justification defect",
                "necessity": "Synthetic explanation is incomplete",
                "source_support": "not_applicable",
                "remedy": "other",
            },
            "code": "justification_error",
            "node_ids": [1],
            "certainty": "definite",
            "reason": "Synthetic test issue, not a scientific verdict.",
            "evidence": [
                {"field": "node:1", "quote": self.bundle["nodes"][0]["statement"]}
            ],
        }
        self.reject = dict(
            self.accept,
            decision="reject",
            issues=[self.issue],
            checks=dict(self.accept["checks"], justifications_complete=False),
        )

    def prepare(self, candidate="default", role="case", source_dispute=False):
        baseline = {
            "candidate": self.bundle if candidate == "default" else candidate,
            "role": role,
            "source_dispute": source_dispute,
            "prior_reason": "synthetic disputed claim",
        }
        write_once(self.root / "items.json", [self.item])
        write_once(
            self.root / "items" / self.item["item_id"] / "baseline.json", baseline
        )
        write_once(
            self.root / "selection.json",
            {
                "protocol": "gpqa-revision-v1",
                "max_additional_rounds": 2,
                "selected_ids": [self.item["item_id"]],
                "selected_item_sha256": {self.item["item_id"]: digest(self.item)},
                "baseline_sha256": {self.item["item_id"]: digest(baseline)},
            },
        )

    def proposal(self, candidate, issues=None, action="revise"):
        issues = issue_packet(self.reject) if issues is None else issues
        return {
            "action": action,
            "reason": "Synthetic change rationale.",
            "changes": ["Fixture modification."],
            "issue_responses": [
                {
                    "issue_id": i["issue_id"],
                    "action": "fixed" if action == "revise" else "disputed",
                    "reason": "Fixture response.",
                    "evidence": [{"field": "solution", "quote": "The distance is 6 m"}],
                }
                for i in issues
            ],
            "candidate": candidate if action == "revise" else None,
        }

    def run_queue(self, outputs, **kwargs):
        client = QueueClient(outputs)
        result = RevisionPipeline(self.root, config(**kwargs), client).run()
        return result["results"][0], client, result

    def changed(self, suffix=" Updated wording."):
        candidate = deepcopy(self.bundle)
        candidate["justifications"][0]["text"] += suffix
        return candidate

    def choice_analysis(self):
        return [
            {
                "choice": f"choice_{letter}",
                "status": "undetermined",
                "reason": "Synthetic unresolved comparison.",
                "evidence": [
                    {
                        "field": f"choice_{letter}",
                        "quote": self.reference["reference_sources"][
                            f"choice_{letter}"
                        ],
                    },
                    {"field": "solution", "quote": "The distance is 6 m"},
                ],
            }
            for letter in "ABCD"
        ]

    def test_accepted_initial_candidate_needs_one_audit(self):
        self.prepare()
        result, client, _ = self.run_queue([self.accept])
        self.assertEqual(result["status"], "repaired_model_accepted")
        self.assertEqual(len(client.calls), 1)

    def test_structural_diagnosis_requires_matching_remedy(self):
        review = deepcopy(self.reject)
        issue = review["issues"][0]
        issue["code"] = "invalid_dependency"
        issue["diagnosis"].update(kind="structural_error", remedy="repair_structure")
        validate_audit(review, self.reference, self.bundle)
        issue["diagnosis"]["remedy"] = "explain_rule"
        with self.assertRaises(InvalidOutput):
            validate_audit(review, self.reference, self.bundle)

    def test_structural_repair_preserves_assertions(self):
        issue = deepcopy(self.issue)
        issue["diagnosis"].update(kind="structural_error", remedy="repair_structure")
        candidate = deepcopy(self.bundle)
        candidate["parents"][-1]["parents"] = [1]
        validate_revision_scope(self.bundle, candidate, [issue])
        candidate["nodes"][0]["statement"] += " Invented fact."
        with self.assertRaises(InvalidOutput):
            validate_revision_scope(self.bundle, candidate, [issue])

    def test_structural_repair_allows_topological_renumbering_scope(self):
        issue = deepcopy(self.issue)
        issue["diagnosis"].update(kind="structural_error", remedy="repair_structure")
        candidate = deepcopy(self.bundle)
        # Scope is ID-invariant; validate_bundle separately checks graph validity.
        for node in candidate["nodes"]:
            node["node_id"] += 10
        candidate["parents"] = []
        validate_revision_scope(self.bundle, candidate, [issue])
        candidate["nodes"][0]["source_quote"] += " fabricated"
        with self.assertRaises(InvalidOutput):
            validate_revision_scope(self.bundle, candidate, [issue])

    def test_choice_analysis_requires_all_actual_choices(self):
        rows = self.choice_analysis()
        validate_choice_analysis(rows, self.reference)
        with self.assertRaises(InvalidOutput):
            validate_choice_analysis(rows[:-1], self.reference)
        rows[-1] = deepcopy(rows[0])
        with self.assertRaises(InvalidOutput):
            validate_choice_analysis(rows, self.reference)

    def test_choice_analysis_rejects_wrong_option_and_answer_authority(self):
        rows = self.choice_analysis()
        rows[0]["evidence"][0]["quote"] += " not in option"
        with self.assertRaises(InvalidOutput):
            validate_choice_analysis(rows, self.reference)
        rows = self.choice_analysis()
        rows[0]["evidence"].append(
            {
                "field": "correct_answer",
                "quote": self.reference["reference_sources"]["correct_answer"],
            }
        )
        with self.assertRaises(InvalidOutput):
            validate_choice_analysis(rows, self.reference)

    def test_choice_analysis_requires_source_not_only_choices(self):
        rows = self.choice_analysis()
        rows[0]["evidence"] = rows[0]["evidence"][:1]
        with self.assertRaises(InvalidOutput):
            validate_choice_analysis(rows, self.reference)

    def test_audit_requires_actionable_diagnosis(self):
        value = deepcopy(self.reject)
        del value["issues"][0]["diagnosis"]
        with self.assertRaises(InvalidOutput):
            validate_audit(value, self.reference, self.bundle)

    def test_granularity_cannot_be_definite_rejection(self):
        value = deepcopy(self.reject)
        value["issues"][0]["diagnosis"].update(
            kind="granularity_dispute", remedy="adjudicate"
        )
        with self.assertRaises(InvalidOutput):
            validate_audit(value, self.reference, self.bundle)
        value["decision"] = "needs_review"
        value["issues"][0]["certainty"] = "uncertain"
        validate_audit(value, self.reference, self.bundle)

    def test_added_fact_requires_source_evidence(self):
        value = deepcopy(self.reject)
        issue = value["issues"][0]
        issue["diagnosis"].update(
            kind="factual_gap", remedy="add_source_premise", source_support="available"
        )
        with self.assertRaises(InvalidOutput):
            validate_audit(value, self.reference, self.bundle)
        issue["evidence"] = [{"field": "solution", "quote": "The distance is 6 m"}]
        validate_audit(value, self.reference, self.bundle)
        issue["diagnosis"]["source_support"] = "absent"
        with self.assertRaises(InvalidOutput):
            validate_audit(value, self.reference, self.bundle)

    def test_dependency_feedback_cannot_hide_in_other(self):
        value = deepcopy(self.reject)
        value["issues"][0]["code"] = "invalid_dependency"
        with self.assertRaises(InvalidOutput):
            validate_audit(value, self.reference, self.bundle)

    def test_rule_only_revision_cannot_expand_graph(self):
        issue = deepcopy(self.issue)
        issue["diagnosis"].update(kind="rule_gap", remedy="explain_rule")
        packet = [dict(issue, issue_id="synthetic-rule")]
        candidate = self.changed()
        validate_revision(
            self.proposal(candidate, packet), self.reference, self.bundle, packet
        )
        candidate["nodes"][0]["statement"] += " Extra assertion."
        with self.assertRaises(InvalidOutput):
            validate_revision(
                self.proposal(candidate, packet), self.reference, self.bundle, packet
            )

    def test_granularity_dispute_routes_to_adjudication(self):
        self.prepare()
        review = deepcopy(self.reject)
        review["decision"] = "needs_review"
        review["issues"][0]["certainty"] = "uncertain"
        review["issues"][0]["diagnosis"].update(
            kind="granularity_dispute", remedy="adjudicate"
        )
        packet = issue_packet(review)
        verdict = {
            "choice_analysis": self.choice_analysis(),
            "decision": "uncertain",
            "reason": "Cannot resolve the boundary.",
            "evidence": [{"field": "solution", "quote": "The distance is 6 m"}],
        }
        result, client, _ = self.run_queue(
            [review, self.proposal(None, packet, action="dispute"), verdict]
        )
        self.assertEqual(result["status"], "revision_review_disputed")
        self.assertEqual(len(client.calls), 3)
        with self.assertRaises(InvalidOutput):
            validate_revision(
                self.proposal(self.changed(), packet),
                self.reference,
                self.bundle,
                packet,
            )

    def test_literal_conclusion_as_knowledge_parent_is_blocked(self):
        before = {
            "nodes": [
                {"node_id": 1, "kind": "knowledge", "statement": "Fact"},
                {"node_id": 2, "kind": "derived", "statement": "Conclusion"},
            ],
            "parents": [{"node_id": 1, "parents": []}, {"node_id": 2, "parents": [1]}],
        }
        after = deepcopy(before)
        after["nodes"][0]["statement"] = "Conclusion"
        with self.assertRaises(InvalidOutput):
            validate_revision_scope(before, after, [])
        # Final-answer restatement is not this prohibited derived inference.
        after["nodes"][1]["kind"] = "answer"
        validate_revision_scope(before, after, [])

    def test_revision_evidence_has_explicit_version(self):
        candidate = self.changed(" NEW VERSION ONLY")
        proposal = self.proposal(candidate)
        proposal["issue_responses"][0]["evidence"] = [
            {"field": "revised_justification:1", "quote": "NEW VERSION ONLY"}
        ]
        validate_revision(
            proposal, self.reference, self.bundle, issue_packet(self.reject)
        )
        proposal["issue_responses"][0]["evidence"][0]["field"] = "justification:1"
        with self.assertRaises(InvalidOutput):
            validate_revision(
                proposal, self.reference, self.bundle, issue_packet(self.reject)
            )

    def test_dispute_cannot_cite_nonexistent_revised_candidate(self):
        proposal = self.proposal(None, action="dispute")
        proposal["issue_responses"][0]["evidence"] = [
            {"field": "revised_node:1", "quote": self.bundle["nodes"][0]["statement"]}
        ]
        with self.assertRaises(InvalidOutput):
            validate_revision(
                proposal, self.reference, self.bundle, issue_packet(self.reject)
            )

    def test_revision_then_blind_audit(self):
        self.prepare()
        result, client, _ = self.run_queue(
            [self.reject, self.proposal(self.changed()), self.accept]
        )
        self.assertEqual(result["status"], "repaired_model_accepted")
        for request in (client.calls[0], client.calls[2]):
            data = json.loads(request["messages"][1]["content"])
            self.assertEqual(
                set(data),
                {
                    "question",
                    "solution",
                    "reference_sources",
                    "reference_answer",
                    "candidate",
                },
            )
            self.assertNotIn(
                "Synthetic change rationale", request["messages"][1]["content"]
            )

    def test_terminal_resume_is_free(self):
        self.prepare()
        self.run_queue([self.accept])
        result, client, snapshot = self.run_queue([])
        self.assertEqual(result["status"], "repaired_model_accepted")
        self.assertEqual(client.calls, [])
        self.assertEqual(snapshot["attempt_count"], 1)

    def test_unchanged_proposal_stops_before_repeat_audit(self):
        self.prepare()
        result, client, _ = self.run_queue([self.reject, self.proposal(self.bundle)])
        self.assertEqual(result["status"], "revision_no_progress")
        self.assertEqual(len(client.calls), 2)

    def test_two_round_bound_and_persistent_issue(self):
        self.prepare()
        outputs = [
            self.reject,
            self.proposal(self.changed(" one")),
            self.reject,
            self.proposal(self.changed(" two")),
            self.reject,
        ]
        result, client, _ = self.run_queue(outputs)
        self.assertEqual(result["status"], "revision_no_progress")
        self.assertEqual(len(client.calls), 5)
        self.assertFalse(
            (self.root / "items" / self.item["item_id"] / "dag.json").exists()
        )

    def test_control_rejection_does_not_edit_control(self):
        self.prepare(role="control")
        result, client, _ = self.run_queue([self.reject])
        self.assertEqual(result["status"], "revision_control_disagreement")
        self.assertEqual(len(client.calls), 1)

    def test_source_dispute_never_constructs_dag(self):
        self.prepare(candidate=None, source_dispute=True)
        adjudication = {
            "choice_analysis": self.choice_analysis(),
            "decision": "source_disputed",
            "reason": "Fixture source defect.",
            "evidence": [{"field": "solution", "quote": "The distance is 6 m"}],
        }
        result, client, _ = self.run_queue([adjudication])
        self.assertEqual(result["status"], "revision_source_disputed")
        self.assertEqual(len(client.calls), 1)

    def test_producer_appeal_cannot_approve_candidate(self):
        self.prepare()
        adjudication = {
            "choice_analysis": self.choice_analysis(),
            "decision": "revisable",
            "reason": "Fixture critique overturned.",
            "evidence": [{"field": "solution", "quote": "The distance is 6 m"}],
        }
        result, _, _ = self.run_queue(
            [self.reject, self.proposal(None, action="dispute"), adjudication]
        )
        self.assertEqual(result["status"], "revision_review_disputed")

    def test_absent_justifications_can_enter_one_revision(self):
        incomplete = deepcopy(self.bundle)
        incomplete["justifications"] = []
        self.prepare(candidate=incomplete)
        proposal = self.proposal(self.bundle, issues=[])
        proposal["changes"] = [
            {"change": "Supply missing explanations.", "evidence": "Fixture source."}
        ]
        result, _, _ = self.run_queue([proposal, self.accept])
        self.assertEqual(result["status"], "repaired_model_accepted")
        path = (
            self.root
            / "items"
            / self.item["item_id"]
            / "round-01-revise/normalization.json"
        )
        self.assertTrue(read_json(path)["operations"])

    def test_budget_exhaustion_does_not_become_semantic_rejection(self):
        self.prepare()
        result, client, _ = self.run_queue([self.reject], max_calls=1)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["reason"], "budget_exhausted")
        self.assertEqual(len(client.calls), 1)

    def test_failed_protocol_not_semantically_resampled(self):
        self.prepare()
        bad = deepcopy(self.reject)
        bad["issues"][0]["evidence"][0]["quote"] = "NOT PRESENT"
        result, client, _ = self.run_queue([bad])
        self.assertEqual(result["status"], "revision_protocol_error")
        self.assertEqual(len(client.calls), 1)

    def test_wrong_node_evidence_fails_closed(self):
        bad = deepcopy(self.reject)
        bad["issues"][0]["evidence"][0]["field"] = "node:2"
        with self.assertRaisesRegex(InvalidOutput, "evidence quote"):
            validate_audit(bad, self.reference, self.bundle)

    def test_unknown_evidence_node_and_uncertain_rejection(self):
        for issue in (
            dict(self.issue, node_ids=[999]),
            dict(self.issue, certainty="uncertain"),
        ):
            with self.assertRaises(InvalidOutput):
                validate_audit(
                    dict(self.reject, issues=[issue]), self.reference, self.bundle
                )

    def test_nonaccept_without_issues_fails(self):
        with self.assertRaises(InvalidOutput):
            validate_audit(dict(self.reject, issues=[]), self.reference, self.bundle)

    def test_normalization_preserves_scientific_strings(self):
        original = {
            "candidate": deepcopy(self.bundle),
            "changes": [{"a": "b", "nested": [1, 2]}],
        }
        original["candidate"]["nodes"][0]["source_field"] = "solution.rationale"
        before = deepcopy(original)
        value, operations = normalize(original)
        self.assertEqual(original, before)
        self.assertEqual(json.loads(value["changes"][0]), original["changes"][0])
        self.assertEqual(
            value["candidate"]["nodes"][0]["statement"],
            original["candidate"]["nodes"][0]["statement"],
        )
        self.assertEqual(len(operations), 2)
        validate_bundle(value["candidate"], self.reference)

    def test_multiple_structural_errors_are_reported(self):
        candidate = deepcopy(self.bundle)
        candidate["nodes"][0]["source_quote"] = "MISSING QUOTE"
        candidate["parents"] = []
        candidate["justifications"] = []
        self.assertEqual(len(diagnose(candidate, self.reference)), 3)

    def test_source_mutation_blocks_before_network(self):
        self.prepare()
        changed = dict(self.item, official_explanation="changed")
        client = QueueClient([])
        with self.assertRaisesRegex(InvalidOutput, "source changed"):
            RevisionPipeline(self.root, config(), client).process(changed)
        self.assertEqual(client.calls, [])

    def test_truncated_completion_not_accepted(self):
        self.prepare()
        client = QueueClient([self.accept])
        original = client.complete

        def truncated(request):
            response = original(request)
            response["choices"][0]["finish_reason"] = "length"
            return response

        with patch.object(client, "complete", side_effect=truncated):
            result = RevisionPipeline(self.root, config(), client).run()
        self.assertEqual(result["results"][0]["status"], "revision_protocol_error")

    def test_auth_failure_pauses(self):
        self.prepare()
        result, _, _ = self.run_queue([CallFailure("authentication", 401)])
        self.assertEqual(result["status"], "paused")

    def test_new_model_and_json_format_are_explicit(self):
        request = payload(
            "audit",
            dict(self.reference, candidate=self.bundle),
            config(thinking="enabled", response_format="json_object"),
        )
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertNotIn("temperature", request)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertIn("untrusted data", prompt("audit", "gpqa-revision-v1", "gpqa"))

    def test_source_preparation_preserves_accepted_control_and_hashes(self):
        source = self.root.parent / "source"
        write_once(source / "items.json", [self.item])
        write_once(source / "selection.json", {"selected_ids": [self.item["item_id"]]})
        Pipeline(
            source,
            Config(task_type="gpqa", prompt_version="gpqa-reference-v1", workers=1),
            ReferenceClient(self.item),
        ).run()
        before = {str(p): p.read_bytes() for p in source.rglob("*.json")}
        selection = prepare_revision(
            self.root,
            [
                {
                    "item_id": self.item["item_id"],
                    "source_root": str(source),
                    "role": "control",
                }
            ],
            "Synthetic fixture control.",
        )
        self.assertEqual(selection["max_additional_rounds"], 2)
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in source.rglob("*.json")}
        )
        baseline = read_json(
            self.root / "items" / self.item["item_id"] / "baseline.json"
        )
        self.assertEqual(baseline["role"], "control")
        validate_bundle(baseline["candidate"], self.reference)

    def test_different_issue_after_second_revision_hits_round_limit(self):
        self.prepare()
        another = deepcopy(self.reject)
        another["issues"][0]["code"] = "source_fidelity"
        result, client, _ = self.run_queue(
            [
                self.reject,
                self.proposal(self.changed(" first")),
                self.reject,
                self.proposal(self.changed(" second")),
                another,
            ]
        )
        self.assertEqual(result["status"], "revision_round_limit")
        self.assertEqual(len(client.calls), 5)

    def test_unchanged_schema_error_does_not_request_again_on_resume(self):
        self.prepare()
        self.run_queue([{}])
        result, client, _ = self.run_queue([])
        self.assertEqual(result["status"], "revision_protocol_error")
        self.assertEqual(client.calls, [])

    def test_cli_repair_routes_to_new_protocol(self):
        from dag_builder import cli

        self.prepare()
        path = self.root / "config.json"
        write_once(path, config().to_dict())
        client = QueueClient([self.accept])
        with (
            patch(
                "sys.argv",
                [
                    "dag-builder",
                    "repair",
                    "--root",
                    str(self.root),
                    "--config",
                    str(path),
                ],
            ),
            patch.object(cli, "load_key", return_value="synthetic-not-a-key"),
            patch.object(cli, "APIClient", return_value=client),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
