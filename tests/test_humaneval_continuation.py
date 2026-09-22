"""Bounded continuation, immutable original responses, synthetic API only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dag_builder.humaneval_continuation import prepare_continuation, HumanEvalContinuationPipeline
from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_recheck import recheck_contracts
from dag_builder.humaneval_repair import recovery_pipeline_type
from dag_builder.humaneval_source import normalize_humaneval
from dag_builder.schemas import InvalidOutput
from dag_builder.storage import read_json, write_once
from test_humaneval import fixture_config, source
import test_humaneval_contracts as contracts
from test_humaneval_contracts import CorrectedClient
from test_humaneval_repair import RepairClient


class ContinuationTests(unittest.TestCase):
    initialize = contracts.HumanEvalContractTests.initialize
    old_failed_repair = contracts.HumanEvalContractTests.old_failed_repair

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.item = normalize_humaneval([source()], 'a' * 40)[0]
        self.config = replace(fixture_config(), prompt_version='humaneval-reference-v5')
        self.destination = self.root / 'continue'

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, failed_stage='atomize'):
        self.parent = self.old_failed_repair(failed_stage)
        self.audit = self.root / 'audit'
        recheck_contracts(self.parent, self.audit)
        return prepare_continuation(self.destination, self.parent, self.audit)

    def test_three_missing_stages_only_and_exported_provenance(self):
        result = self.prepare()
        self.assertEqual(result['maximum_new_semantic_requests'], 3)
        self.assertEqual(result['reused_stage_counts'], {'solve': 1, 'review_solution': 1, 'atomize': 1})
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.parent.rglob('*') if p.is_file()}
        client = CorrectedClient(self.item)
        pipeline = HumanEvalContinuationPipeline(self.destination, self.config, client)
        self.assertIs(recovery_pipeline_type(self.destination), HumanEvalContinuationPipeline)
        self.assertEqual(pipeline.run()['results'][0]['status'], 'model_accepted')
        self.assertEqual(len(client.calls), 3)
        pipeline.run()
        self.assertEqual(len(client.calls), 3)
        directory = self.destination / 'items' / self.item['item_id']
        self.assertFalse((directory / 'diagnose/output.json').exists())
        self.assertFalse((directory / 'review_solution/output.json').exists())
        self.assertTrue((directory / 'seeded_stages/review_solution.json').exists())
        export = export_validation(self.destination)
        row = json.loads((export / 'model_accepted.jsonl').read_text())
        self.assertEqual(row['dag']['recovery_provenance']['solution_review_origin'], 'preserved_parent_response')
        self.assertNotIn('both_reviews_rerun', row['dag']['recovery_provenance'])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.parent.rglob('*') if p.is_file()})
        self.assertNotIn('PRIVATE_TEST_SENTINEL', json.dumps(client.calls))

    def test_diagnosis_reused_graph_route_needs_five_calls(self):
        prepared = self.prepare('diagnose')
        self.assertEqual(prepared['maximum_new_semantic_requests'], 5)
        client = CorrectedClient(self.item)
        result = HumanEvalContinuationPipeline(self.destination, self.config, client).run()
        self.assertEqual(result['results'][0]['status'], 'model_accepted')
        self.assertEqual(len(client.calls), 5)
        self.assertTrue(export_validation(self.destination).is_dir())

    def test_new_review_rejection_is_terminal_not_resampled(self):
        self.prepare()
        client = CorrectedClient(self.item)
        client.outputs['review_dag'].update(decision='reject', issues=['Synthetic semantic defect.'])
        pipeline = HumanEvalContinuationPipeline(self.destination, self.config, client)
        self.assertEqual(pipeline.run()['results'][0]['status'], 'rejected')
        pipeline.run()
        self.assertEqual(len(client.calls), 3)
        with self.assertRaisesRegex(InvalidOutput, 'no accepted'):
            export_validation(self.destination)

    def test_incompatible_version_or_api_profile_rejected(self):
        self.prepare()
        for config in (replace(self.config, prompt_version='humaneval-reference-v4'),
                       replace(self.config, model='a-different-model')):
            with self.assertRaises(InvalidOutput):
                HumanEvalContinuationPipeline(self.destination, config, object())

    def test_corrupt_seed_and_parent_evidence_rejected(self):
        self.prepare()
        pipeline = HumanEvalContinuationPipeline(self.destination, self.config, object())
        original_read = read_json
        def tampered(path):
            value = original_read(path)
            if Path(path).name == 'continuation_seed.json':
                value['reused_outputs']['solve']['rationale'] = 'An altered explanation.'
            return value
        with patch('dag_builder.humaneval_continuation.read_json', side_effect=tampered):
            with self.assertRaisesRegex(InvalidOutput, 'seed changed'):
                pipeline.seed(self.item)

    def test_cannot_reuse_recheck_after_source_changes(self):
        self.prepare()
        write_once(self.parent / 'new-artifact.json', {'changed': True})
        with self.assertRaisesRegex(InvalidOutput, 'parent changed'):
            prepare_continuation(self.root / 'other', self.parent, self.audit)

    def test_export_rejects_substituted_review(self):
        self.prepare()
        pipeline = HumanEvalContinuationPipeline(self.destination, self.config, CorrectedClient(self.item))
        pipeline.run()
        dag = read_json(self.destination / 'items' / self.item['item_id'] / 'dag.json')
        dag['solution_review']['reason'] = 'Substituted review'
        with self.assertRaisesRegex(InvalidOutput, 'differs'):
            pipeline.validate_export(self.item, dag)

    def test_nested_semantic_continuation_not_supported(self):
        self.prepare()
        result = HumanEvalContinuationPipeline(self.destination, self.config, CorrectedClient(self.item)).run()
        write_once(self.destination / 'completion.json', {'status': 'processed', 'result': result})
        with self.assertRaises(InvalidOutput):
            prepare_continuation(self.root / 'another-round', self.destination, self.audit)

    def test_unstarted_rationale_revision_gets_one_explanation_only(self):
        with patch('test_humaneval_contracts.RepairClient', side_effect=lambda item: RepairClient(item, 'rationale_revision')):
            prepared = self.prepare('diagnose')
        self.assertEqual(prepared['maximum_new_semantic_requests'], 6)
        client = CorrectedClient(self.item)
        pipeline = HumanEvalContinuationPipeline(self.destination, self.config, client)
        self.assertEqual(pipeline.run()['results'][0]['status'], 'model_accepted')
        pipeline.run()
        self.assertEqual(len(client.calls), 6)
        self.assertTrue(export_validation(self.destination).is_dir())

    def test_source_concern_never_starts_generation(self):
        with patch('test_humaneval_contracts.RepairClient', side_effect=lambda item: RepairClient(item, 'source_concern')):
            prepared = self.prepare('diagnose')
        self.assertEqual(prepared['maximum_new_semantic_requests'], 0)
        client = CorrectedClient(self.item)
        result = HumanEvalContinuationPipeline(self.destination, self.config, client).run()
        self.assertEqual(result['results'][0]['status'], 'rejected')
        self.assertEqual(client.calls, [])
