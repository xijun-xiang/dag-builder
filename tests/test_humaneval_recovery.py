"""Recovery uses synthetic fixtures only; no API, source code execution or GPU."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dag_builder.config import Config
from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_quality import normalize_sources, text_findings, validate_quality, DAG_CHECKS, REVIEW_CHECKS
from dag_builder.humaneval_recovery import audit_quality, prepare_recovery, HumanEvalRecoveryPipeline
from dag_builder.humaneval_source import normalize_humaneval
from dag_builder.pipeline import Pipeline
from dag_builder.schemas import InvalidOutput
from dag_builder.stages import validate
from dag_builder.storage import digest, read_json, write_once
from test_humaneval import source, outputs, Client, fixture_config


class QualityClient(Client):
    def __init__(self, item):
        super().__init__(item, "humaneval-reference-v4")
        self.outputs["review_solution"]["checks"].update({k: True for k in REVIEW_CHECKS})
        self.outputs["review_dag"]["checks"].update({k: True for k in DAG_CHECKS})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.item = normalize_humaneval([source()], "a" * 40)[0]
        self.source = self.root / "source"
        self.destination = self.root / "recovery"
        self.config = replace(fixture_config(), prompt_version="humaneval-reference-v4")

    def tearDown(self):
        self.temp.cleanup()

    def original(self, positional=False, review_reject=False):
        client = Client(self.item, "humaneval-reference-v3")
        for node in client.outputs["atomize"]["nodes"][:-1]:
            node["source_field"] = "solution.rationale"
        if positional:
            client.outputs["atomize"]["nodes"][2]["statement"] += " By step 2."
        if review_reject:
            client.outputs["review_solution"].update(decision="reject", issues=["Source ambiguity."])
        write_once(self.source / "items.json", [self.item])
        write_once(self.source / "selection.json", {"selected_ids": [self.item["item_id"]], "candidate_count": 164})
        result = Pipeline(self.source, replace(fixture_config(), prompt_version="humaneval-reference-v3"), client).run()
        write_once(self.source / "completion.json", {"status": "processed", "result": result})
        return result

    def test_alias_only_and_no_input_mutation(self):
        raw = outputs(self.item)["atomize"]
        raw["nodes"][0]["source_field"] = "rationale"
        original = deepcopy(raw)
        normalized, changes = normalize_sources(raw)
        self.assertEqual(raw, original)
        self.assertEqual(changes, [{"node_id": 1, "field": "source_field", "from": "rationale", "to": "solution"}])
        raw["nodes"][0]["source_field"] = "unknown"
        self.assertEqual(normalize_sources(raw), (raw, []))

    def test_alias_never_waives_quote_or_answer_checks(self):
        raw = outputs(self.item)["atomize"]
        raw["nodes"][0].update(source_field="rationale", source_quote="INVENTED")
        data = {"question": {"task_type": "humaneval", "question": self.item["question"]},
                "solution": outputs(self.item)["solve"], "reference_code": self.item["canonical_solution"],
                "reference_sources": {"reference_code": self.item["canonical_solution"]}}
        with self.assertRaises(InvalidOutput):
            validate("atomize", normalize_sources(raw)[0], data, "reference_code_explanation", prompt_version=self.config.prompt_version)

    def test_position_references_not_arithmetic_numbers(self):
        for phrase in ("By step 6.", "From nodes 2 and 3.", "由第 3 步得到。", "Using statement #2."):
            self.assertTrue(text_findings([{"kind": "derived", "node_id": 1, "statement": phrase}]))
        self.assertFalse(text_findings([{"kind": "derived", "statement": "For n >= 2 the result is n + 3."}]))

    def test_accept_requires_additional_quality_checks(self):
        value = outputs(self.item)["review_dag"]
        with self.assertRaises(InvalidOutput):
            validate_quality("review_dag", value, {"nodes": []})
        value["checks"].update({k: True for k in DAG_CHECKS})
        validate_quality("review_dag", value, {"nodes": []})
        value["checks"]["no_invariant_assumed"] = False
        with self.assertRaises(InvalidOutput):
            validate_quality("review_dag", value, {"nodes": []})

    def test_v4_fresh_normalizes_with_audit_record_and_exports(self):
        client = QualityClient(self.item)
        client.outputs["atomize"]["nodes"][0]["source_field"] = "solution.rationale"
        write_once(self.destination / "items.json", [self.item])
        write_once(self.destination / "selection.json", {"selected_ids": [self.item["item_id"]]})
        result = Pipeline(self.destination, self.config, client).run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        stage = self.destination / "items" / self.item["item_id"] / "atomize"
        self.assertEqual(len(read_json(stage / "normalization.json")["changes"]), 1)
        self.assertIn("solution.rationale", read_json(stage / "attempt-00/response.json")["body"]["choices"][0]["message"]["content"])
        self.assertTrue(export_validation(self.destination).is_dir())

    def test_format_recovery_reuses_two_stages_but_reruns_both_reviews(self):
        self.assertEqual(self.original()["results"][0]["status"], "needs_review")
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob("*.json")}
        with patch("urllib.request.urlopen", side_effect=AssertionError("offline preparation")):
            prepared = prepare_recovery(self.destination, self.source)
        self.assertEqual(prepared["routes"], {"lossless_format": 1})
        self.assertEqual(prepared["api_calls"], 0)
        client = QualityClient(self.item)
        result = HumanEvalRecoveryPipeline(self.destination, self.config, client).run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 4)
        HumanEvalRecoveryPipeline(self.destination, self.config, client).run()
        self.assertEqual(len(client.calls), 4)
        directory = self.destination / "items" / self.item["item_id"]
        self.assertEqual(len(list((directory / "seeded_stages").glob("*.json"))), 2)
        self.assertFalse((directory / "solve/output.json").exists())
        dag = read_json(directory / "dag.json")
        self.assertEqual(dag["source"], self.item)
        self.assertTrue(dag["recovery_provenance"]["both_reviews_rerun"])
        destination = export_validation(self.destination)
        exported = read_json(destination / "manifest.json")
        self.assertEqual(exported["recovery"]["source_inventory"]["source_candidates"], 164)
        self.assertEqual(exported["recovery"]["source_inventory"]["counts"], {"lossless_format": 1})
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob("*.json")})

    def test_positional_reference_routes_to_explicit_semantic_revision(self):
        self.original(positional=True)
        report = audit_quality(self.source, self.root / "audit")
        self.assertEqual(report["counts"], {"semantic_revision": 1})
        self.assertTrue(report["inventory"][0]["structural_pass_after_alias"])
        with self.assertRaises(InvalidOutput):
            prepare_recovery(self.destination, self.source)
        prepare_recovery(self.destination, self.source, include_semantic=True)
        client = QualityClient(self.item)
        result = HumanEvalRecoveryPipeline(self.destination, self.config, client).run()
        self.assertEqual(result["results"][0]["status"], "model_accepted")
        self.assertEqual(len(client.calls), 6)
        context = json.loads(client.calls[0]["messages"][1]["content"])["revision_context"]
        self.assertEqual(context["round"], 1)
        self.assertEqual(context["original_result"]["status"], "needs_review")
        self.assertEqual(context["offline_diagnosis"]["text_findings"][0]["code"], "positional_reference")
        self.assertIsNotNone(context["prior_nodes"])

    def test_source_rejection_not_automatically_repaired(self):
        self.original(review_reject=True)
        report = audit_quality(self.source, self.root / "audit")
        self.assertEqual(report["counts"], {"manual_source_review": 1})
        with self.assertRaises(InvalidOutput):
            prepare_recovery(self.destination, self.source, include_semantic=True)

    def test_quality_veto_is_never_resurrected(self):
        self.original()
        write_once(self.source / "quality_exclusions.json", [{"item_id": self.item["item_id"],
            "task_id": self.item["task_id"], "decision": "reject", "reason": "Independent concern", "evidence": "Fixture",
            "solve_output_sha256": digest(read_json(self.source / "items" / self.item["item_id"] / "solve/output.json"))}])
        self.assertEqual(audit_quality(self.source, self.root / "audit")["counts"], {"manual_source_review": 1})

    def test_historical_cost_remains_and_budget_blocks_new_calls(self):
        self.original()
        prepare_recovery(self.destination, self.source)
        history = read_json(self.destination / "recovery_manifest.json")["historical_cost"]
        self.assertEqual(history["request_attempts"], 3)
        self.assertGreater(history["reserved_tokens"], 0)
        client = QualityClient(self.item)
        result = HumanEvalRecoveryPipeline(self.destination, replace(self.config, max_calls=1), client).run()
        self.assertTrue(result["paused"])
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(InvalidOutput):
            export_validation(self.destination)

    def test_modified_seed_and_original_evidence_refused(self):
        self.original(); prepare_recovery(self.destination, self.source)
        path = self.destination / "items" / self.item["item_id"] / "recovery_seed.json"
        seed = read_json(path); seed["route"] = "semantic_revision"
        path.write_text(json.dumps(seed))
        runner = HumanEvalRecoveryPipeline(self.destination, self.config, QualityClient(self.item))
        with self.assertRaises(InvalidOutput):
            runner.seed(self.item)
        path = self.destination / "originals/items.json"
        path.write_text("[]")
        with self.assertRaises(InvalidOutput):
            HumanEvalRecoveryPipeline(self.destination, self.config, QualityClient(self.item))

    def test_plain_run_cannot_bypass_recovery_provenance(self):
        self.original(); prepare_recovery(self.destination, self.source)
        with self.assertRaises(ValueError):
            Pipeline(self.destination, self.config, QualityClient(self.item))

    def test_new_review_can_reject_format_candidate_without_resampling(self):
        self.original(); prepare_recovery(self.destination, self.source)
        client = QualityClient(self.item)
        client.outputs["review_solution"].update(decision="reject", issues=["Unproved invariant."])
        client.outputs["review_solution"]["checks"]["root_premises_sound"] = False
        result = HumanEvalRecoveryPipeline(self.destination, self.config, client).run()
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(len(client.calls), 1)
        HumanEvalRecoveryPipeline(self.destination, self.config, client).run()
        self.assertEqual(len(client.calls), 1)

    def test_invalid_json_is_not_fixed_by_source_alias_normalizer(self):
        from dag_builder.schemas import parse_object
        with self.assertRaises(InvalidOutput):
            parse_object('{"nodes": [{"source_field": "rationale"}')

    def test_tampered_original_stage_is_not_reused(self):
        self.original()
        path = self.source / "items" / self.item["item_id"] / "solve/output.json"
        path.write_text(json.dumps({"rationale": "An invented replacement."}))
        with self.assertRaises(InvalidOutput):
            prepare_recovery(self.destination, self.source)

    def test_nested_repair_and_source_as_destination_refused(self):
        self.original()
        with self.assertRaises(InvalidOutput):
            prepare_recovery(self.source, self.source)
        prepare_recovery(self.destination, self.source)
        result = HumanEvalRecoveryPipeline(self.destination, self.config, QualityClient(self.item)).run()
        write_once(self.destination / "completion.json", {"result": result})
        with self.assertRaises(InvalidOutput):
            prepare_recovery(self.root / "second-round", self.destination, include_semantic=True)
