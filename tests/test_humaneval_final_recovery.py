"""Synthetic-only final recovery tests; no network or reference-code execution."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_final_recovery import (
    PROTOCOL, REVIEW_SCOPE, HumanEvalFinalRecoveryPipeline, revise_outputs,
)
from dag_builder.humaneval_repair import recovery_pipeline_type
from dag_builder.humaneval_source import normalize_humaneval
from dag_builder.schemas import InvalidOutput
from dag_builder.storage import digest, read_json, write_once
from test_humaneval import fixture_config, source
from test_humaneval_contracts import CorrectedClient


class FinalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'run'
        self.item = normalize_humaneval([source(5)], 'a' * 40)[0]
        self.item['row'] = 5  # Synthetic subset row must mimic the full cohort.
        self.config = replace(fixture_config(), prompt_version='humaneval-reference-v5')
        self.client = CorrectedClient(self.item)
        self.expected = deepcopy(self.client.outputs)
        self.patches = [patch('dag_builder.humaneval_final_recovery.APPROVED_TASKS', (5,)),
                        patch.dict('dag_builder.humaneval_final_recovery.NODE_COUNTS', {5: 7})]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        original_complete = self.client.complete
        def complete(request):
            request = deepcopy(request)
            request['messages'][0]['content'] = request['messages'][0]['content'].removesuffix('\n\n' + REVIEW_SCOPE)
            return original_complete(request)
        self.client.complete = complete
        original = {k: v for k, v in self.expected.items() if k != 'review_dag'}
        revised, changes = revise_outputs(5, original)
        seed = {'task_id': self.item['task_id'], 'item': self.item, 'round': 1,
                'source_root': 'synthetic-parent', 'original_outputs': original,
                'reused_outputs': revised, 'change_log': changes, 'new_stages': ['review_dag']}
        selection = {'selected_ids': [self.item['item_id']]}
        manifest = {'protocol': PROTOCOL, 'round_limit': 1, 'config_sha256': digest(self.config.to_dict()),
                    'items_sha256': digest([self.item]), 'selection_sha256': digest(selection),
                    'seed_sha256': {self.item['item_id']: digest(seed)}, 'original_file_sha256': {},
                    'review_scope': REVIEW_SCOPE, 'source_inventory': {}, 'historical_cost': {}}
        write_once(self.root / 'items.json', [self.item])
        write_once(self.root / 'selection.json', selection)
        write_once(self.root / 'recovery_manifest.json', manifest)
        write_once(self.root / 'items' / self.item['item_id'] / 'final_recovery_seed.json', seed)

    def tearDown(self):
        self.temp.cleanup()

    def pipeline(self):
        return HumanEvalFinalRecoveryPipeline(self.root, self.config, self.client)

    def test_one_new_review_export_and_no_duplicate_calls(self):
        pipeline = self.pipeline()
        self.assertIs(recovery_pipeline_type(self.root), HumanEvalFinalRecoveryPipeline)
        self.assertEqual(pipeline.run()['results'][0]['status'], 'model_accepted')
        pipeline.run()
        self.assertEqual(len(self.client.calls), 1)
        self.assertTrue(export_validation(self.root).is_dir())
        directory = self.root / 'items' / self.item['item_id']
        self.assertFalse((directory / 'solve/output.json').exists())
        self.assertTrue((directory / 'seeded_stages/solve.json').exists())
        dag = read_json(directory / 'dag.json')
        self.assertFalse(dag['recovery_provenance']['human_approved'])
        self.assertNotIn('PRIVATE_TEST_SENTINEL', str(self.client.calls))
        self.assertIn(REVIEW_SCOPE, read_json(directory / 'review_dag/attempt-00/request.json')['payload']['messages'][0]['content'])

    def test_semantic_reject_is_terminal(self):
        self.client.outputs['review_dag'].update(decision='reject', issues=['Synthetic new defect.'])
        pipeline = self.pipeline()
        self.assertEqual(pipeline.run()['results'][0]['status'], 'rejected')
        pipeline.run()
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaisesRegex(InvalidOutput, 'no accepted'):
            export_validation(self.root)

    def test_truncation_is_terminal_no_json_repair_or_reasoning_fallback(self):
        calls = []
        def invalid(request):
            calls.append(request)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{',
                    'reasoning_content': '{"decision":"accept"}'}}]}
        self.client.complete = invalid
        pipeline = self.pipeline()
        self.assertEqual(pipeline.run()['results'][0]['status'], 'needs_review')
        pipeline.run()
        self.assertEqual(len(calls), 1)

    def test_config_mutation_refused(self):
        with self.assertRaisesRegex(InvalidOutput, 'config changed'):
            HumanEvalFinalRecoveryPipeline(self.root, replace(self.config, workers=2), self.client)

    def test_export_cannot_substitute_new_review(self):
        pipeline = self.pipeline()
        pipeline.run()
        dag = read_json(self.root / 'items' / self.item['item_id'] / 'dag.json')
        dag['dag_review']['reason'] = 'Substituted.'
        with self.assertRaisesRegex(InvalidOutput, 'export differs'):
            pipeline.validate_export(self.item, dag)

    def test_seed_cannot_change_explanation(self):
        pipeline = self.pipeline()
        def tampered(path):
            value = read_json(path)
            if Path(path).name == 'final_recovery_seed.json':
                value['reused_outputs']['solve']['rationale'] = 'Changed.'
            return value
        with patch('dag_builder.humaneval_final_recovery.read_json', side_effect=tampered):
            with self.assertRaisesRegex(InvalidOutput, 'seed changed'):
                pipeline.seed(self.item)


class RecipeTests(unittest.TestCase):
    def inputs(self, number):
        from dag_builder.humaneval_final_recovery import NODE_COUNTS
        size = NODE_COUNTS[number]
        nodes = [{'node_id': n, 'kind': 'knowledge' if n < size else 'answer',
                  'statement': f'Fixture assertion {n}.', 'source_field': 'solution',
                  'source_quote': f'Fixture assertion {n}.'} for n in range(1, size + 1)]
        pm = {n: [] for n in range(1, size)}
        pm[size] = list(range(1, size))
        return {'solve': {'rationale': 'Unchanged fixture explanation.'}, 'review_solution': {'decision': 'accept'},
                'atomize': {'nodes': nodes},
                'dependencies': {'parents': [{'node_id': n, 'parents': ps} for n, ps in pm.items()]}}

    def test_stride_edit_changes_only_three_statements(self):
        original = self.inputs(85)
        for n in (3, 4, 10):
            original['atomize']['nodes'][n-1]['statement'] = 'A range with step 2 selects alternate indices.'
        saved = deepcopy(original)
        revised, changes = revise_outputs(85, original)
        self.assertEqual(saved, original)
        self.assertEqual(len(changes['edits']), 3)
        self.assertNotIn('dependencies', revised)
        for before, after in zip(original['atomize']['nodes'], revised['atomize']['nodes']):
            self.assertEqual(before['source_quote'], after['source_quote'])
        original['atomize']['nodes'][0]['statement'] = 'with step 2'
        with self.assertRaisesRegex(InvalidOutput, 'target changed'):
            revise_outputs(85, original)

    def test_root_move_preserves_text_and_edge_identity(self):
        original = self.inputs(106)
        original['atomize']['nodes'][11]['kind'] = 'given'
        original['atomize']['nodes'][10]['kind'] = 'derived'
        original['dependencies']['parents'][10]['parents'] = [12]
        revised, changes = revise_outputs(106, original)
        self.assertEqual(changes['old_to_new_node_id']['12'], 11)
        self.assertEqual(changes['old_to_new_node_id']['11'], 12)
        self.assertEqual(revised['dependencies']['parents'][11]['parents'], [11])
        self.assertEqual(revised['atomize']['nodes'][10]['statement'], original['atomize']['nodes'][11]['statement'])

    def test_cannot_prune_used_node_or_expand_shortlist(self):
        with self.assertRaisesRegex(InvalidOutput, 'still-used'):
            revise_outputs(83, self.inputs(83))
        with self.assertRaisesRegex(InvalidOutput, 'outside approved'):
            revise_outputs(24, {})


if __name__ == '__main__':
    unittest.main()
