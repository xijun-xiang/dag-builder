"""Full rollout wiring uses fake responses only; no external calls/execution."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dag_builder.calibri_pipeline import CALIBRIPipeline, prepare, verify_prepared
from dag_builder.calibri_repair import CALIBRIRepairPipeline, CHECKED_PROTOCOL, prepare_repair
from dag_builder.calibri_repair_plan import AUDIT_CHECKS
from dag_builder.livecodebench_dag import sha256
from dag_builder.storage import digest, read_json, write_once
from test_calibri_pipeline import Client as NormalizeClient, config, setup
from test_calibri_repair import Client, failed_parent, offline_audit, repair_outputs
from test_calibri_repair_plan import checked_plan


class FirstPassTests(unittest.TestCase):
    def test_checked_first_pass_binds_original_failure_and_never_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp).resolve()
            parent, outputs = failed_parent(folder)
            run = folder / 'first-pass'
            prepare_repair(parent, run, prompt_version=CHECKED_PROTOCOL, first_pass=True)
            prepared = read_json(run / 'calibri-normalization-manifest.json')
            self.assertEqual(prepared['prior_calls'], 2)
            outputs = repair_outputs(outputs)
            outputs['repair'] = checked_plan()
            outputs['review_dag']['checks'].update(dict.fromkeys(AUDIT_CHECKS, True))
            client = Client(outputs, CHECKED_PROTOCOL)
            pipeline = CALIBRIRepairPipeline(run, config(prompt_version=CHECKED_PROTOCOL, max_calls=9), client)
            for _ in range(2):
                result = pipeline.run()
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(result['results'][0]['status'], 'model_accepted')
            dag = read_json(run / 'items' / ('a' * 20) / 'dag.json')
            self.assertEqual(dag['recovery_provenance']['repair_mode'], 'checked_first_pass')
            self.assertNotIn('development_iteration', dag['recovery_provenance'])
            self.assertFalse(dag['formal_eligible'])
            self.assertNotIn('tests_sha256', json.dumps(client.calls))
            write_once(run / 'completion.json', {'status': 'processed'})
            write_once(run / 'code_origin.json', {'source_files': {}, 'git_commit': 'synthetic'})
            self.assertTrue(offline_audit(run)['mechanical_pass'])
            with self.assertRaisesRegex(ValueError, 'one repair pass'):
                prepare_repair(run, folder / 'second', prompt_version=CHECKED_PROTOCOL, first_pass=True)

    def test_first_pass_cannot_masquerade_as_revision_or_use_v1(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _ = failed_parent(Path(temp).resolve())
            for index, kwargs in enumerate(({}, {'prompt_version': CHECKED_PROTOCOL, 'history': parent})):
                with self.assertRaisesRegex(ValueError, 'first pass requires'):
                    prepare_repair(parent, parent.parent / str(index), first_pass=True, **kwargs)


def two_candidates_after_development(folder):
    source, execution, development, outputs = setup(folder)
    CALIBRIPipeline(development, config(), NormalizeClient(outputs)).run()
    write_once(development / 'completion.json', {'status': 'processed'})
    # Expand the synthetic full source and execution evidence, not the already
    # immutable development run. The second candidate is a different source row.
    old = read_json(source / 'items.json')[0]
    item = copy.deepcopy(old)
    item.update(item_id='d' * 20, question_id='synthetic-second', row=1)
    raw = read_json(source / 'source-rows/livecodebench_qwen3' / (old['item_id'] + '.json'))
    raw['id'] = item['question_id']
    item['origin']['selected_columns_sha256'] = digest(raw)
    write_once(source / 'source-rows/livecodebench_qwen3' / (item['item_id'] + '.json'), raw)
    def replace(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    selection = {'selected_ids': [old['item_id'], item['item_id']]}
    manifest = read_json(source / 'calibri-manifest.json')
    manifest.update(items_sha256=digest([old, item]), selection_sha256=digest(selection))
    replace(source / 'items.json', [old, item])
    replace(source / 'selection.json', selection)
    replace(source / 'calibri-manifest.json', manifest)
    rows = read_json(execution / 'execution-input.json')
    rows.append({**rows[0], 'item_id': item['item_id']})
    replace(execution / 'execution-input.json', rows)
    execution_manifest = read_json(execution / 'input-manifest.json')
    execution_manifest.update(planned=2, inputs_sha256=digest(rows), calibri_manifest_sha256=digest(manifest))
    replace(execution / 'input-manifest.json', execution_manifest)
    second = {**read_json(execution / 'results' / (old['item_id'] + '.json')), 'item_id': item['item_id']}
    path = execution / 'results' / (item['item_id'] + '.json')
    write_once(path, second)
    completion = read_json(execution / 'completion.json')
    completion.update(executed=2, passed=2, input_manifest_sha256=sha256(execution / 'input-manifest.json'))
    completion['results'][path.name] = sha256(path)
    replace(execution / 'completion.json', completion)
    return source, execution, development


class DevelopmentPreservationTests(unittest.TestCase):
    def test_development_is_excluded_and_tampering_stops_before_api(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp).resolve()
            source, execution, development = two_candidates_after_development(folder)
            run = folder / 'full'
            selection = prepare(source, execution, execution / 'input-manifest.json', run,
                development_run=development, prompt_version='calibri-lcb-normalize-v2')
            self.assertEqual(selection['selected_ids'], ['d' * 20])
            self.assertEqual(selection['excluded'][0]['item_id'], 'a' * 20)
            self.assertEqual(len(verify_prepared(run, config(prompt_version='calibri-lcb-normalize-v2'))), 1)
            path = run / 'evidence/development-cohort.json'
            value = read_json(path)
            value['items'][0]['question'] = 'tampered'
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'development cohort evidence'):
                verify_prepared(run, config(prompt_version='calibri-lcb-normalize-v2'))

    def test_deployment_integrity_checked_before_hidden_test_materialization(self):
        import prepare_calibri_full_execution as module
        with tempfile.TemporaryDirectory() as temp, patch.object(module, 'implementation', side_effect=ValueError('bad snapshot')):
            output = Path(temp) / 'new'
            with self.assertRaisesRegex(ValueError, 'bad snapshot'):
                module.prepare(Path(temp) / 'missing', Path(temp) / 'missing', Path(temp) / 'missing', output)
            self.assertFalse(output.exists())
