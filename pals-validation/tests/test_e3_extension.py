"""CPU-only checks for the separately identified, fixed two-model completion."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import greedy, greedy_extension as extension
from pals_validation.e3.backend import E3MockBackend
from pals_validation.e3.fixture import prepare_fixture
from pals_validation.e3.greedy_report import group_summary
from pals_validation.io import read, save
import test_e3_held_submit as held
from test_e3_greedy import SUBMIT

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_download_test', ROOT / 'scripts/e3_public_models.py')
DOWNLOAD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOWNLOAD)


def config(slot):
    cfg = greedy.make_config(read(ROOT / 'configs/e3-mock.json'), extension_slot=slot)
    cfg['model']['id'] = extension.SLOTS[slot]
    cfg['hf_runtime']['max_context'] = extension.CONTEXTS[slot]
    return cfg


def inputs(base):
    prepared = base / 'prepared'
    prepare_fixture(prepared)
    meta = read(prepared / 'manifest.json')
    meta['schema_version'] = 'pals_e3_greedy_mock_v1'
    (prepared / 'manifest.json').write_text(__import__('json').dumps(meta))
    return prepared, {s: config(s) for s in extension.SLOTS}


class ExtensionTests(unittest.TestCase):
    def test_native_context_cap_uses_longest_padded_prompt(self):
        cfg = config('llama3')
        greedy.validate_config(cfg)
        b = extension.generation_budget(cfg, 'humaneval', [100, 500])
        self.assertEqual(b['effective_max_new_tokens'], 8192 - 500 - 64)
        self.assertEqual(b['requested_max_new_tokens'], 16384)
        self.assertTrue(b['context_capped'])
        for lengths in ([], [0], [-1], [False], [8128], [9000]):
            with self.subTest(lengths=lengths), self.assertRaises(ValueError):
                extension.generation_budget(cfg, 'gpqa', lengths)

    def test_internlm_keeps_original_output_budget(self):
        cfg = config('internlm3')
        greedy.validate_config(cfg)
        b = extension.generation_budget(cfg, 'livecodebench', [3000])
        self.assertEqual(b['effective_max_new_tokens'], 16384)
        self.assertFalse(b['context_capped'])
        with self.assertRaises(ValueError):
            extension.generation_budget(cfg, 'livecodebench', [20000])

    def test_no_policy_context_or_prompt_drift(self):
        for slot in extension.SLOTS:
            for name in ('budget', 'context', 'prompt'):
                cfg = config(slot)
                if name == 'budget':
                    cfg['budget_policy']['reserve_tokens'] += 1
                elif name == 'context':
                    cfg['hf_runtime']['max_context'] += 1
                else:
                    cfg['prompt_version'] = 'different'
                with self.assertRaises(ValueError):
                    greedy.validate_config(cfg)

    def test_real_revision_must_match_frozen_snapshot(self):
        for slot in extension.SLOTS:
            cfg = config(slot)
            cfg['backend'] = 'hf'
            with self.assertRaisesRegex(ValueError, 'revision'):
                greedy.validate_config(cfg)
            cfg['model']['revision'] = extension.REVISIONS[slot]
            greedy.validate_config(cfg)
            self.assertEqual(DOWNLOAD.SOURCES[slot][2], extension.REVISIONS[slot])

    def test_architecture_probe_requires_cpu_and_offline_before_imports(self):
        from pals_validation.e3.cpu_compatibility import probe
        import os
        for env in ({}, {'SLURM_JOB_ID':'1', 'CUDA_VISIBLE_DEVICES':'0'}, {'SLURM_JOB_ID':'1'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                probe(config('internlm3'))

    def test_old_exact_three_new_exact_two_no_recovery(self):
        self.assertEqual(len(greedy.model_slots({'protocol_version':greedy.VERSION})), 3)
        with self.assertRaises(ValueError):
            greedy.model_slots({'protocol_version':extension.VERSION, 'model_slots':greedy.SLOTS})
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared, configs = inputs(base)
            for bad in ({'llama3':configs['llama3']}, {**configs, 'qwen25':configs['llama3']}):
                with self.assertRaises(ValueError):
                    greedy.init(prepared, bad, {}, base / 'bad')
            with self.assertRaisesRegex(ValueError, 'cannot import'):
                greedy.init(prepared, configs, {}, base / 'bad', recovery_plan={})
            self.assertFalse((base / 'bad').exists())

    def test_roundtrip_and_budget_evidence_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared, configs = inputs(base)
            run = base / 'run'
            greedy.init(prepared, configs, {}, run)
            self.assertEqual(greedy.model_slots(read(run / 'manifest.json')), extension.SLOTS)
            for slot in extension.SLOTS:
                for shard in range(8):
                    greedy.gpu_worker(run, slot, shard, barrier=False)
                    greedy.evaluate(run, slot, shard)
                greedy.audit(run, slot)
                audit = read(run / slot / 'audit.json')
                self.assertFalse(audit['scientific_evidence'])
                self.assertEqual(len(audit['items']), 5)
                self.assertTrue(all(abs(r['summary']['M'] - .2) < 1e-9 for r in audit['items']))
                _, cfg, problems, batches = greedy.load(run, slot)
                batch = batches[0]
                backend = E3MockBackend('', cfg)
                output = backend.generate_batch([problems[i] for i in batch['problem_ids']], batch['seed'])
                extension.verify_budget(cfg, batch, output)
                for kind in ('budget', 'padding', 'length'):
                    bad = deepcopy(output)
                    if kind == 'budget':
                        bad['generation_contract']['max_new_tokens'] += 1
                    elif kind == 'padding':
                        bad['rows'][0]['left_pad_tokens'] += 1
                    else:
                        bad['rows'][0]['finish_reason'] = 'length'
                    with self.assertRaises(ValueError):
                        extension.verify_budget(cfg, batch, bad)

    def test_download_hash_and_source_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file'
            path.write_bytes(b'hello\n')
            result = DOWNLOAD.hashes(path)
            self.assertEqual(result['git_blob_sha1'], 'ce013625030ba8dba906f756967f9e9ca394464a')
        url = DOWNLOAD.file_url('modelscope', *DOWNLOAD.SOURCES['llama3'][1:], 'config.json')
        self.assertIn('e9f7e7d3', url)
        self.assertIn('FilePath=config.json', url)

    def test_proxy_is_explicit_job_only_and_rejects_credentials(self):
        import os
        with patch.dict(os.environ, {'https_proxy':'http://unrelated:9999', 'ALL_PROXY':'http://elsewhere:9999'}):
            args, env = DOWNLOAD.transport('http://127.0.0.1:1234')
            self.assertEqual(args, ['--proxy','http://127.0.0.1:1234','--noproxy',''])
            self.assertNotIn('https_proxy', env)
            self.assertNotIn('ALL_PROXY', env)
            self.assertEqual(os.environ['ALL_PROXY'], 'http://elsewhere:9999')
        for proxy in ('http://u:p@localhost:80', 'file:///tmp/proxy', 'http://localhost:80/path', 'http://localhost'):
            with self.subTest(proxy=proxy), self.assertRaises((AssertionError, ValueError)):
                DOWNLOAD.transport(proxy)

    def test_missing_scores_not_zero_and_caps_are_reported(self):
        row = {'summary':None, 'process_valid':False, 'process_reason':'truncated',
               'step_count':0, 'correct':None, 'outcome_status':'N/A', 'finish_reason':'length',
               'generation_budget':{'context_capped':True, 'effective_max_new_tokens':7000}}
        result = group_summary([row])
        self.assertEqual(result['context_capped'], 1)
        self.assertEqual(result['length_terminations'], 1)
        self.assertEqual(result['scoreable'], 0)
        self.assertIsNone(result['M']['mean'])


class ExtensionSchedulerTests(unittest.TestCase):
    def test_exact_four_serial_held_jobs(self):
        with held.HeldTests().setup_scheduler() as (root, repo, prepared, states, commands, releases):
            manifest = {'protocol_id':'test', 'protocol_version':extension.VERSION, 'model_slots':extension.SLOTS}
            (root / 'experiment/manifest.json').write_text(__import__('json').dumps(manifest))
            with patch.object(SUBMIT, 'load', return_value=(manifest, None, None, None)):
                jobs = SUBMIT.submit(root, repo, prepared, '10', [held.EXCLUDED])
                self.assertEqual(len(jobs), 4)
                self.assertEqual(sum('--gpus-per-node=8' in c for c in commands), 2)
                SUBMIT.release(root, repo, prepared, '10')
                self.assertEqual(releases, ['104','103','102','101'])


if __name__ == '__main__':
    unittest.main()
