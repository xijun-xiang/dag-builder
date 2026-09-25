"""Diagnosed repair contracts; synthetic fixtures, no API, code execution or GPU."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from dag_builder.client import CallFailure
from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_recovery import prepare_recovery, HumanEvalRecoveryPipeline
from dag_builder.humaneval_repair import (
    HumanEvalRepairPipeline, prepare_diagnosed_repair, diagnosis_input, diagnosis_prompt,
    validate_diagnosis, recovery_pipeline_type,
)
from dag_builder.humaneval_source import normalize_humaneval
from dag_builder.pipeline import Pipeline
from dag_builder.schemas import InvalidOutput
from dag_builder.storage import digest, read_json, write_once
from test_humaneval import source, Client, fixture_config
from test_humaneval_recovery import QualityClient


class RepairClient(QualityClient):
    def __init__(self, item, route='graph_repair'):
        super().__init__(item)
        self.diagnosis = {
            'route': route, 'source_consistent': route != 'source_concern',
            'rationale_reusable': route == 'graph_repair', 'reason': 'Synthetic diagnosis.',
            'issues': [{'evidence_source': 'rationale', 'quote': 'The inputs are two lists.',
                        'problem': 'Synthetic missing representation.', 'action': 'Rebuild faithful nodes.'}],
        }

    def complete(self, request):
        if request['messages'][0]['content'] != diagnosis_prompt():
            return super().complete(request)
        self.calls.append(request)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.diagnosis)}}],
                'usage': {'total_tokens': 100}}


class DiagnosedRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.original = self.root / 'original'
        self.repair = self.root / 'repair'
        self.item = normalize_humaneval([source()], 'a' * 40)[0]
        self.config = replace(fixture_config(), prompt_version='humaneval-reference-v4')

    def tearDown(self):
        self.temp.cleanup()

    def first_pass(self, failure='atomize'):
        client = Client(self.item, 'humaneval-reference-v3')
        if failure == 'atomize':
            for node in client.outputs['atomize']['nodes'][:-1]:
                node['source_field'] = 'solution.rationale'
        elif failure == 'review_solution':
            client.outputs[failure].update(decision='reject', issues=['Wrong proof but valid reference'])
        elif failure == 'review_dag':
            client.outputs[failure].update(decision='reject', issues=['Missing dependency'])
        write_once(self.original / 'items.json', [self.item])
        write_once(self.original / 'selection.json', {'selected_ids': [self.item['item_id']], 'candidate_count': 164})
        result = Pipeline(self.original, replace(fixture_config(), prompt_version='humaneval-reference-v3'), client).run()
        write_once(self.original / 'completion.json', {'status': 'processed', 'result': result})
        return result

    def prepare(self):
        self.first_pass()
        return prepare_diagnosed_repair(self.repair, self.original)

    def test_graph_repair_preserves_rationale_and_reruns_reviews(self):
        self.prepare()
        client = RepairClient(self.item)
        pipeline = HumanEvalRepairPipeline(self.repair, self.config, client)
        result = pipeline.run()
        self.assertEqual(result['results'][0]['status'], 'model_accepted')
        self.assertEqual(len(client.calls), 6)
        directory = self.repair / 'items' / self.item['item_id']
        self.assertFalse((directory / 'solve/output.json').exists())
        self.assertTrue((directory / 'seeded_stages/solve.json').exists())
        dag = read_json(directory / 'dag.json')
        self.assertEqual(dag['reference_solution']['rationale'], client.outputs['solve']['rationale'])
        for call in client.calls:
            self.assertNotIn('PRIVATE_TEST_SENTINEL', json.dumps(call))
            data = json.loads(call['messages'][1]['content'])
            if call['messages'][0]['content'].startswith(('Review', 'Audit')):
                self.assertNotIn('revision_context', data)
        exported = export_validation(self.repair)
        self.assertEqual(read_json(exported / 'manifest.json')['accepted'], 1)
        pipeline.run()
        self.assertEqual(len(client.calls), 6)

    def test_rationale_revision_runs_exactly_one_new_explanation(self):
        self.prepare()
        client = RepairClient(self.item, 'rationale_revision')
        result = HumanEvalRepairPipeline(self.repair, self.config, client).run()
        self.assertEqual(result['results'][0]['status'], 'model_accepted')
        self.assertEqual(len(client.calls), 7)
        self.assertTrue((self.repair / 'items' / self.item['item_id'] / 'solve/output.json').exists())
        self.assertTrue(export_validation(self.repair).is_dir())

    def test_source_concern_stops_after_diagnosis(self):
        self.prepare()
        client = RepairClient(self.item, 'source_concern')
        result = HumanEvalRepairPipeline(self.repair, self.config, client).run()
        self.assertEqual(result['results'][0]['stage'], 'diagnose')
        self.assertEqual(result['results'][0]['status'], 'rejected')
        self.assertEqual(len(client.calls), 1)
        with self.assertRaises(InvalidOutput):
            export_validation(self.repair)

    def test_source_evidence_quotes_and_route_consistency(self):
        self.prepare()
        seed = read_json(self.repair / 'items' / self.item['item_id'] / 'repair_seed.json')
        data = diagnosis_input(self.item, seed)
        value = RepairClient(self.item).diagnosis
        validate_diagnosis(value, data)
        for change in ({'source_consistent': False}, {'route': 'rationale_revision'}, {'route': 'unknown'}):
            bad = dict(value, **change)
            with self.assertRaises(InvalidOutput): validate_diagnosis(bad, data)
        bad = deepcopy(value); bad['issues'][0]['quote'] = 'invented quotation'
        with self.assertRaises(InvalidOutput): validate_diagnosis(bad, data)
        bad = deepcopy(value); del bad['source_consistent']
        with self.assertRaises(InvalidOutput): validate_diagnosis(bad, data)

    def test_bad_diagnosis_is_not_repaired_or_resampled(self):
        self.prepare()
        client = RepairClient(self.item)
        client.diagnosis['issues'][0]['quote'] = 'invented quotation'
        pipeline = HumanEvalRepairPipeline(self.repair, self.config, client)
        result = pipeline.run()
        self.assertEqual(result['results'][0]['status'], 'needs_review')
        pipeline.run()
        self.assertEqual(len(client.calls), 1)

    def test_review_rejection_does_not_trigger_second_candidate(self):
        self.prepare()
        client = RepairClient(self.item, 'rationale_revision')
        client.outputs['review_solution'].update(decision='reject', issues=['Still wrong'])
        pipeline = HumanEvalRepairPipeline(self.repair, self.config, client)
        result = pipeline.run()
        self.assertEqual(result['results'][0]['status'], 'rejected')
        pipeline.run()
        self.assertEqual(len(client.calls), 3)

    def test_prior_proof_rejection_is_eligible_for_diagnosis(self):
        self.first_pass('review_solution')
        self.assertEqual(prepare_diagnosed_repair(self.repair, self.original)['selected'], 1)

    def test_evidence_bound_quarantine_cannot_promote_candidate(self):
        self.first_pass('review_solution')
        result = read_json(self.original / 'items' / self.item['item_id'] / 'result.json')
        quarantine = [{'task_id': self.item['task_id'], 'reason': 'Source concern', 'evidence_result_sha256': digest(result)}]
        with self.assertRaisesRegex(InvalidOutput, 'no unresolved'):
            prepare_diagnosed_repair(self.repair, self.original, quarantines=quarantine)
        quarantine[0]['evidence_result_sha256'] = '0' * 64
        with self.assertRaisesRegex(InvalidOutput, 'evidence changed'):
            prepare_diagnosed_repair(self.repair, self.original, quarantines=quarantine)

    def test_existing_accepted_not_regenerated(self):
        self.first_pass(None)
        with self.assertRaisesRegex(InvalidOutput, 'no unresolved'):
            prepare_diagnosed_repair(self.repair, self.original)

    def test_direct_lossless_overlay_preserves_new_rejection(self):
        self.first_pass()
        overlay = self.root / 'format'
        prepare_recovery(overlay, self.original)
        client = QualityClient(self.item)
        client.outputs['review_dag'].update(decision='reject', issues=['New graph concern'], reason='NEW_REVIEW_REASON')
        result = HumanEvalRecoveryPipeline(overlay, self.config, client).run()
        write_once(overlay / 'completion.json', {'status': 'processed', 'result': result})
        prepared = prepare_diagnosed_repair(self.repair, self.original, overlay)
        self.assertEqual(prepared['selected'], 1)
        seed = read_json(self.repair / 'items' / self.item['item_id'] / 'repair_seed.json')
        self.assertEqual(seed['latest_result']['reason'], 'NEW_REVIEW_REASON')
        self.assertEqual(seed['stage_outputs']['review_dag']['reason'], 'NEW_REVIEW_REASON')
        self.assertEqual(recovery_pipeline_type(self.repair), HumanEvalRepairPipeline)

    def test_nested_semantic_repair_refused(self):
        self.prepare()
        HumanEvalRepairPipeline(self.repair, self.config, RepairClient(self.item)).run()
        write_once(self.repair / 'completion.json', {'status': 'processed'})
        with self.assertRaises(InvalidOutput):
            prepare_diagnosed_repair(self.root / 'again', self.original, self.repair)
        with self.assertRaises(InvalidOutput):
            prepare_diagnosed_repair(self.root / 'again2', self.repair)

    def test_seed_and_ancestry_tampering_refused(self):
        self.prepare()
        pipeline = HumanEvalRepairPipeline(self.repair, self.config, RepairClient(self.item))
        p = self.repair / 'items' / self.item['item_id'] / 'repair_seed.json'
        value = read_json(p); value['semantic_round'] = 2; p.write_text(json.dumps(value))
        with self.assertRaises(InvalidOutput): pipeline.seed(self.item)
        p = self.repair / 'originals/first_pass/items.json'; p.write_text('[]')
        with self.assertRaises(InvalidOutput): HumanEvalRepairPipeline(self.repair, self.config, object())

    def test_budget_exhaustion_is_not_scientific_rejection(self):
        self.prepare()
        client = RepairClient(self.item)
        result = HumanEvalRepairPipeline(self.repair, replace(self.config, max_calls=1), client).run()
        self.assertTrue(result['paused'])
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.repair / 'items' / self.item['item_id'] / 'result.json').exists())

    def test_plain_pipeline_cannot_bypass_diagnosis(self):
        self.prepare()
        with self.assertRaises(ValueError): Pipeline(self.repair, self.config, object())


if __name__ == '__main__':
    unittest.main()
