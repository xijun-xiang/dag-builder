"""Expansion regression tests use synthetic text and never execute generated code."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3.expanded import MODEL_IDS, SLOTS, audit, batch_plan, prepare, select_cohort, worker
from pals_validation.e3.fixture import prepare_fixture
from pals_validation.e3.run import init_run, worker as original_worker
from pals_validation.io import digest, encoded, read, save, sha256

ROOT = Path(__file__).resolve().parents[1]


def sources(root):
    prepared = root / 'prepared'
    prepare_fixture(prepared)
    templates, answers = read(prepared / 'problems.json'), read(prepared / 'grading/answers.json')
    problems, grading = [], {}
    for p in templates:
        for n in range(24):
            row = {**p, 'problem_id': p['benchmark'] + ':' + str(n).zfill(3)}
            if p['benchmark'] == 'mmlu':
                row['subset'] = 'subject-' + str(n % 3)
            problems.append(row)
            grading[row['problem_id']] = answers[p['problem_id']]
    (prepared / 'problems.json').write_bytes(encoded(problems))
    (prepared / 'grading/answers.json').write_bytes(encoded(grading))
    manifest = read(prepared / 'manifest.json')
    manifest['files'] = {n: sha256(prepared / n) for n in manifest['files']}
    (prepared / 'manifest.json').write_bytes(encoded(manifest))
    for slot in SLOTS:
        config = read(ROOT / 'configs/e3-mock.json')
        config.update(schema_version='pals_e3_config_v2', protocol_version='native-trace-v2',
                      prompt_version='native-trace-prompt-v2', canary_batch_index=1)
        config['model']['id'] = MODEL_IDS[slot]
        if slot == 'qwen3':
            config['hf_runtime']['chat_template_kwargs'] = {'enable_thinking': False}
        save(root / (slot + '.json'), config)
        init_run(prepared, root / (slot + '.json'), root / 'sources' / slot)
    for shard in range(8):
        original_worker(root / 'sources/phi4mini', 'generate', shard, canary=True)
    return root / 'sources'


class ExpansionTests(unittest.TestCase):
    def test_selection_independent_of_input_order(self):
        problems = [{'problem_id': str(i), 'benchmark': 'mmlu', 'subset': 's' + str(i % 3)} for i in range(90)]
        a, info = select_cohort(problems, {'0', '1'}, 20)
        b, _ = select_cohort(list(reversed(problems)), {'0', '1'}, 20)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 20)
        self.assertTrue({'0', '1'} <= set(a))
        self.assertEqual(sum(info['subject_quotas'].values()), 20)

    def test_full_lifecycle_reuses_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = sources(root)
            before = {str(p.relative_to(src)): sha256(p) for p in src.rglob('*') if p.is_file()}
            out = root / 'expanded'
            result = prepare(src, out, 20)
            self.assertEqual(result['counts'], dict(gpqa=24, gsm8k=24, humaneval=24, livecodebench=24, mmlu=20))
            manifest = read(out / 'manifest.json')
            self.assertEqual(len(manifest['sources']['phi4mini']['reuse']), 5)
            with patch('pals_validation.e3.backend.E3MockBackend.generate_batch', side_effect=AssertionError('no regeneration')):
                for shard in range(8):
                    worker(out, 'phi4mini', 0, 'generate', shard)
            for wave in range(result['waves']['phi4mini']):
                for stage in ('generate', 'score', 'evaluate'):
                    if wave == 0 and stage == 'generate':
                        continue
                    for shard in range(8):
                        worker(out, 'phi4mini', wave, stage, shard)
                checked = audit(out, 'phi4mini', wave)
                self.assertEqual(checked['status'], 'PASS')
                self.assertFalse(checked['scientific_evidence'])
            all_rows = [r for p in sorted((out / 'phi4mini/audits').glob('*.json')) for r in read(p)['items']]
            self.assertEqual(len(all_rows), 116)
            self.assertEqual(len({r['problem_id'] for r in all_rows}), 116)
            self.assertEqual(sum(r['reused'] for r in all_rows), 40)
            self.assertTrue(all(r['summary']['W'] is None for r in all_rows))
            self.assertEqual(before, {str(p.relative_to(src)): sha256(p) for p in src.rglob('*') if p.is_file()})
            with self.assertRaisesRegex(ValueError, 'resubmitted'):
                worker(out, 'phi4mini', 0, 'generate', 0)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                prepare(src, out, 20)

    def test_uncertain_generation_not_retried_and_no_skip_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = sources(root)
            out = root / 'expanded'
            prepare(src, out, 20)
            batch = read(out / 'manifest.json')['sources']['qwen25']['waves'][0][0]
            save(out / 'qwen25/attempts' / (batch['batch_id'] + '.json'), {})
            with patch('pals_validation.e3.backend.E3MockBackend.generate_batch', side_effect=AssertionError('must not call')):
                with self.assertRaisesRegex(ValueError, 'UNCERTAIN_GENERATION'):
                    worker(out, 'qwen25', 0, 'generate', 0)
            with self.assertRaises(FileNotFoundError):
                worker(out, 'phi4mini', 1, 'generate', 0)

    def test_token_tamper_and_reuse_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = sources(root)
            out = root / 'expanded'
            prepare(src, out, 20)
            for stage in ('generate', 'score', 'evaluate'):
                for shard in range(8):
                    worker(out, 'phi4mini', 0, stage, shard)
            score = next((out / 'phi4mini/scores').glob('*.json'))
            original = read(score)
            bad = copy.deepcopy(original)
            bad['steps'][0]['evidence']['target_ids'][0] += 1
            score.write_bytes(encoded(bad))
            with self.assertRaisesRegex(ValueError, 'token IDs'):
                audit(out, 'phi4mini', 0)
            score.write_bytes(encoded(original))
            raw = next((out / 'phi4mini/generation_batches').glob('*.json'))
            raw.write_bytes(raw.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'reused raw'):
                audit(out, 'phi4mini', 0)

    def test_source_changed_or_partial_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = sources(root)
            batch = read(src / 'qwen25/batches.json')[0]
            save(src / 'qwen25/attempts' / (batch['batch_id'] + '.json'), {})
            with self.assertRaisesRegex(ValueError, 'uncertain/partial'):
                prepare(src, root / 'bad', 20)
            self.assertFalse((root / 'bad').exists())


if __name__ == '__main__':
    unittest.main()
