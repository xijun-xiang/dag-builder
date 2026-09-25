"""Versioned interface regression tests; synthetic responses, no network."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dag_builder.humaneval_export import export_validation
from dag_builder.humaneval_quality import CODE_FACT_VERSION
from dag_builder.humaneval_recheck import recheck_contracts
from dag_builder.humaneval_repair import (
    CONTRACT_PROTOCOL, HumanEvalRepairPipeline, diagnosis_input, diagnosis_prompt,
    prepare_diagnosed_repair, recovery_pipeline_type, validate_diagnosis,
)
from dag_builder.humaneval_source import normalize_humaneval
from dag_builder.pipeline import Pipeline
from dag_builder.schemas import InvalidOutput, validate_nodes
from dag_builder.stages import STAGES, prompt, stage_input, validate
from dag_builder.storage import digest, read_json, write_once
from test_humaneval import Client, fixture_config, outputs, source
from test_humaneval_recovery import QualityClient
from test_humaneval_repair import RepairClient


def code_fact(value):
    value['atomize']['nodes'][0].update(
        statement='The program computes the sum of left and assigns it to left_total.',
        source_field='reference_code', source_quote='left_total = sum(left)')


class CorrectedClient(QualityClient):
    def __init__(self, item):
        super().__init__(item)
        self.version = CODE_FACT_VERSION
        self.outputs['review_dag']['checks']['code_facts_grounded'] = True
        self.diagnosis = RepairClient(item).diagnosis
        code_fact(self.outputs)

    def complete(self, request):
        if request['messages'][0]['content'] != diagnosis_prompt(CONTRACT_PROTOCOL):
            return super().complete(request)
        self.calls.append(request)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.diagnosis)}}],
                'usage': {'total_tokens': 100}}


class HumanEvalContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.item = normalize_humaneval([source()], 'a' * 40)[0]
        self.values = CorrectedClient(self.item).outputs
        self.data = stage_input('atomize', self.item, self.values)
        self.config = replace(fixture_config(), prompt_version=CODE_FACT_VERSION)

    def tearDown(self):
        self.temp.cleanup()

    def atomize(self, value):
        validate('atomize', value, self.data, prompt_version=CODE_FACT_VERSION)

    def initialize(self, root):
        write_once(root / 'items.json', [self.item])
        write_once(root / 'selection.json', {'selected_ids': [self.item['item_id']]})

    def old_failed_repair(self, failure='atomize'):
        original = self.root / 'original'
        self.initialize(original)
        client = Client(self.item, 'humaneval-reference-v3')
        client.outputs['atomize']['nodes'][0]['source_field'] = 'solution.rationale'
        result = Pipeline(original, replace(fixture_config(), prompt_version=client.version), client).run()
        write_once(original / 'completion.json', {'status': 'processed', 'result': result})
        repair = self.root / 'old-repair'
        prepare_diagnosed_repair(repair, original)
        client = RepairClient(self.item)
        if failure == 'atomize':
            code_fact(client.outputs)
        else:
            client.diagnosis['issues'][0].update(evidence_source='prior_dependencies', quote='null')
        result = HumanEvalRepairPipeline(repair, replace(fixture_config(), prompt_version=client.version), client).run()
        write_once(repair / 'completion.json', {'status': 'processed', 'result': result})
        return repair

    def test_v5_code_given_is_allowed_but_legacy_stays_strict(self):
        self.atomize(self.values['atomize'])
        for version in ('humaneval-reference-v1', 'humaneval-reference-v4', None):
            with self.assertRaisesRegex(InvalidOutput, 'answer label'):
                validate('atomize', self.values['atomize'], self.data, prompt_version=version)

    def test_code_quote_never_allows_derived_or_knowledge_premise(self):
        for kind in ('derived', 'knowledge'):
            value = deepcopy(self.values['atomize'])
            value['nodes'][0]['kind'] = kind
            with self.assertRaisesRegex(InvalidOutput, 'not derived or knowledge'):
                self.atomize(value)

    def test_fake_quote_copied_code_positional_and_framing_are_rejected(self):
        for change in ({'source_quote': 'fabricated code'}, {'statement': 'left_total = sum(left)'},
                       {'statement': self.item['canonical_solution']}, {'statement': 'By step 1.'},
                       {'statement': 'Given <step>a fact</step>'}, {'parents': [2]}):
            value = deepcopy(self.values['atomize'])
            value['nodes'][0].update(change)
            with self.assertRaises(InvalidOutput):
                self.atomize(value)

    def test_gpqa_answer_labels_cannot_become_premises(self):
        value = {'nodes': [dict(node) for node in self.values['atomize']['nodes']]}
        value['nodes'][0].update(source_field='correct_answer', source_quote='A')
        with self.assertRaisesRegex(InvalidOutput, 'answer label'):
            validate_nodes(value, 'question', 'rationale', {'correct_answer': 'A'}, allow_reference_code_facts=True)

    def test_v5_cannot_waive_quality_check_with_old_review(self):
        review = deepcopy(self.values['review_dag'])
        data = {'nodes': self.values['atomize']['nodes']}
        for bad in (None, False):
            review['checks']['code_facts_grounded'] = bad
            with self.assertRaisesRegex(InvalidOutput, 'quality check'):
                validate('review_dag', review, data, prompt_version=CODE_FACT_VERSION)
        del review['checks']['code_facts_grounded']
        with self.assertRaisesRegex(InvalidOutput, 'code_facts_grounded'):
            validate('review_dag', review, data, prompt_version=CODE_FACT_VERSION)
        validate('review_dag', review, data, prompt_version='humaneval-reference-v4')

    def test_v5_prompts_are_complete_and_share_only_unchanged_stages(self):
        for stage in STAGES:
            actual = prompt(stage, CODE_FACT_VERSION, 'humaneval')
            self.assertTrue(actual)
            old = prompt(stage, 'humaneval-reference-v4', 'humaneval')
            self.assertEqual(actual == old, stage not in ('atomize', 'review_dag'))

    def test_fresh_v5_pipeline_exports_and_resumes_without_new_calls(self):
        root = self.root / 'fresh'; self.initialize(root)
        client = CorrectedClient(self.item)
        pipeline = Pipeline(root, self.config, client)
        self.assertEqual(pipeline.run()['results'][0]['status'], 'model_accepted')
        self.assertTrue(export_validation(root).is_dir())
        pipeline.run()
        self.assertEqual(len(client.calls), 6)
        self.assertNotIn('PRIVATE_TEST_SENTINEL', json.dumps(client.calls))

    def test_export_rechecks_quotes_even_if_dag_hash_is_updated(self):
        root = self.root / 'fresh'; self.initialize(root)
        Pipeline(root, self.config, CorrectedClient(self.item)).run()
        directory = root / 'items' / self.item['item_id']
        dag = read_json(directory / 'dag.json')
        dag['nodes'][0]['source_quote'] = 'not in code'
        result = read_json(directory / 'result.json'); result['dag_sha256'] = digest(dag)
        real_read = read_json
        def tampered(path):
            return dag if Path(path) == directory / 'dag.json' else result if Path(path) == directory / 'result.json' else real_read(path)
        with patch('dag_builder.humaneval_export.read_json', side_effect=tampered):
            with self.assertRaisesRegex(InvalidOutput, 'verbatim'):
                export_validation(root)

    def test_v2_evidence_is_exact_existing_serialization_without_mutation(self):
        seed = {'stage_outputs': outputs(self.item), 'latest_result': {'status': 'needs_review'}}
        frozen = deepcopy(seed)
        old = diagnosis_input(self.item, seed)
        new = diagnosis_input(self.item, seed, protocol=CONTRACT_PROTOCOL)
        for name in ('prior_dependencies', 'prior_justifications'):
            self.assertNotIn(name, old['evidence_sources'])
            self.assertEqual(new['evidence_sources'][name], json.dumps(old[name], ensure_ascii=False, sort_keys=True))
            value = deepcopy(RepairClient(self.item).diagnosis)
            value['issues'][0].update(evidence_source=name, quote=new['evidence_sources'][name])
            validate_diagnosis(value, new)
            with self.assertRaisesRegex(InvalidOutput, 'unknown'):
                validate_diagnosis(value, old)
            value['issues'][0]['quote'] = 'invented quotation'
            with self.assertRaisesRegex(InvalidOutput, 'match'):
                validate_diagnosis(value, new)
        self.assertEqual(seed, frozen)
        self.assertNotIn('PRIVATE_TEST_SENTINEL', json.dumps(new))

    def test_v2_complete_repair_and_manifest_version_pairing(self):
        old = self.old_failed_repair()
        root = self.root / 'new-repair'
        prepare_diagnosed_repair(root, self.root / 'original', protocol=CONTRACT_PROTOCOL)
        self.assertIs(recovery_pipeline_type(root), HumanEvalRepairPipeline)
        with self.assertRaisesRegex(InvalidOutput, 'protocol/config'):
            HumanEvalRepairPipeline(root, replace(self.config, prompt_version='humaneval-reference-v4'), object())
        client = CorrectedClient(self.item)
        result = HumanEvalRepairPipeline(root, self.config, client).run()
        self.assertEqual(result['results'][0]['status'], 'model_accepted')
        self.assertEqual(len(client.calls), 6)
        self.assertTrue(export_validation(root).is_dir())
        with self.assertRaises(InvalidOutput):
            prepare_diagnosed_repair(self.root / 'recursive', old, protocol=CONTRACT_PROTOCOL)

    def test_offline_replay_keeps_raw_history_and_never_promotes(self):
        source_root = self.old_failed_repair()
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_root.rglob('*') if p.is_file()}
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network')):
            report = recheck_contracts(source_root, self.root / 'audit')
            self.assertEqual(report, recheck_contracts(source_root, self.root / 'audit'))
        self.assertEqual(report['counts'], {'contract_recheck_passed': 1})
        self.assertEqual(report['api_calls'], 0)
        self.assertEqual(report['new_model_accepted'], 0)
        self.assertEqual(hashes, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_root.rglob('*') if p.is_file()})
        with self.assertRaisesRegex(InvalidOutput, 'separate'):
            recheck_contracts(source_root, source_root / 'forbidden-audit')

    def test_offline_diagnosis_replay_does_not_make_new_request(self):
        root = self.old_failed_repair('diagnose')
        report = recheck_contracts(root, self.root / 'audit')
        self.assertEqual(report['categories'], {'diagnosis_evidence_contract': 1})
        self.assertEqual(report['counts'], {'contract_recheck_passed': 1})

    def test_offline_secondary_validation_failure_is_not_hidden(self):
        root = self.old_failed_repair()
        from dag_builder.humaneval_recheck import validate as real_validate
        def secondary(stage, value, data, **kwargs):
            if kwargs.get('prompt_version') == CODE_FACT_VERSION:
                raise InvalidOutput('secondary source-quote defect')
            return real_validate(stage, value, data, **kwargs)
        with patch('dag_builder.humaneval_recheck.validate', side_effect=secondary):
            report = recheck_contracts(root, self.root / 'audit')
        self.assertEqual(report['counts'], {'still_invalid': 1})
