"""CPU-only regression: no invented steps, lost prose, or repeated generation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import greedy, greedy_recovery
from pals_validation.e3.backend import E3MockBackend
from pals_validation.e3.greedy_parse import parse, parse_v1, V1_VERSION, VERSION
from pals_validation.e3.protocol import score_text_pair
from pals_validation.io import read, save, sha256
from test_e3_greedy import fixture

class TitleBodyTests(unittest.TestCase):
    def test_title_body_is_one_lossless_step_not_two(self):
        raw = '<step>Compute A.</step> A=2.\n<step>Compute B.</step> B=A+3=5.\n<answer>5</answer>'
        result = parse(raw, 'boundary', 'gsm8k')
        self.assertFalse(parse_v1(raw, 'boundary', 'gsm8k')['process_valid'])
        self.assertTrue(result['process_valid'])
        self.assertEqual(len(result['steps']), 2)
        self.assertTrue(result['answer']['valid'])
        full, deleted, target = score_text_pair('PROMPT', result, 1)
        self.assertEqual(target, 'Compute B.</step> B=A+3=5.\n')
        self.assertNotIn('A=2', deleted)
        self.assertIn('A=2', full)
        self.assertEqual(deleted, 'PROMPT<step>')
        for step in result['steps']:
            self.assertEqual(step['text'], raw[slice(*step['span'])])

    def test_existing_valid_parse_and_answer_unchanged(self):
        for raw in ('<step>A</step>\n<step>B</step><answer>C</answer>',
                    '<step>A<step>B<answer>C</answer>',
                    '<step>A</step\n<step>B</step><answer>C</answer>'):
            before, after = parse_v1(raw, 'boundary', 'gpqa'), parse(raw, 'boundary', 'gpqa')
            self.assertTrue(greedy_recovery.parse_equivalent(before, after))

    def test_refuse_guesses_malformed_markers_and_truncation(self):
        for raw in ('intro<step>A</step> body<answer>A</answer>',
                    '<step>A</step> body',
                    '<step>A</step> body<step>unfinished',
                    '<step>A</step> body</step><answer>A</answer>',
                    '<step>A</step> body<step extra>B</step><answer>A</answer>',
                    '<step>A</step> body<answer>A</answer>tail',
                    '<step>A</step> body<answer>A</answer><answer>B</answer>'):
            self.assertFalse(parse(raw, 'length', 'gpqa')['process_valid'], raw)
        p = parse('<step>A</step> body<step>B</step> body<answer>unfinished', 'length', 'gpqa')
        self.assertTrue(p['process_valid'])
        self.assertFalse(p['answer']['valid'])


class RecoveryTests(unittest.TestCase):
    def source(self, base, title_body=False):
        with patch.object(greedy, 'PARSER_VERSION', V1_VERSION), patch.object(greedy, 'parse', parse_v1):
            old = fixture(base)
            for slot in greedy.SLOTS:
                m, c, p, batches = greedy.load(old, slot)
                backend = E3MockBackend('', c)
                generate = backend.generate_batch
                def custom(problems, seed):
                    result = generate(problems, seed)
                    for row in result['rows']:
                        row['raw_text'] = '<step>A</step> A=2.\n<step>B</step> B=5.\n<answer>B</answer>'
                    return result
                if title_body:
                    with patch.object(backend, 'generate_batch', side_effect=custom):
                        greedy.process_batch(old, slot, batches[0], backend, m, p)
                else:
                    greedy.process_batch(old, slot, batches[0], backend, m, p)
        return old

    def test_recovery_roundtrip_and_no_regeneration(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            old = self.source(base)
            plan = greedy_recovery.plan(old)
            configs = {s: {**read(old/s/'config.json'), 'parser_version':VERSION} for s in greedy.SLOTS}
            new = base/'recovered'
            greedy.init(base/'prepared', configs, {}, new, recovery_plan=plan)
            with self.assertRaises(FileNotFoundError):
                greedy.load(new, 'qwen25')  # interrupted import is not runnable
            greedy_recovery.import_sealed(old, new, plan)
            slot = 'qwen25'
            m,c,p,batches = greedy.load(new, slot)
            backend = E3MockBackend('',c)
            with patch.object(backend, 'generate_batch', side_effect=AssertionError('regenerated')):
                greedy.process_batch(new, slot, batches[0], backend, m, p)
            self.assertEqual(read(new/'recovery/ready.json')['slots'][slot]['reused_scores'], 1)
            greedy_recovery.verify_imports(new, slot, m)
            for s in greedy.SLOTS:
                for shard in range(8):
                    greedy.gpu_worker(new,s,shard,barrier=False)
                    greedy.evaluate(new,s,shard)
                self.assertEqual(greedy.audit(new,s)['items'],5)
            for rel,value in plan['files'].items():
                self.assertEqual(sha256(old/rel), value)
            with self.assertRaisesRegex(ValueError,'already attempted'):
                greedy_recovery.import_sealed(old,new,plan)

    def test_uncertain_generation_blocks_import(self):
        with tempfile.TemporaryDirectory() as temp:
            old=self.source(Path(temp).resolve())
            save(old/'phi4mini/attempts/unknown.json',{})
            with self.assertRaisesRegex(ValueError,'UNCERTAIN_GENERATION'):
                greedy_recovery.plan(old)

    def test_recovered_output_is_rescored_not_regenerated_and_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp).resolve()
            old=self.source(base,title_body=True)
            planned=greedy_recovery.plan(old)
            configs={s:{**read(old/s/'config.json'),'parser_version':VERSION} for s in greedy.SLOTS}
            new=base/'new'
            greedy.init(base/'prepared',configs,{},new,recovery_plan=planned)
            result=greedy_recovery.import_sealed(old,new,planned)
            self.assertEqual(result['slots']['qwen25']['newly_valid'],1)
            self.assertEqual(result['slots']['qwen25']['scores_to_compute'],1)
            m,c,p,batches=greedy.load(new,'qwen25')
            backend=E3MockBackend('',c)
            with patch.object(backend,'generate_batch',side_effect=AssertionError('regenerated')):
                rows=greedy.process_batch(new,'qwen25',batches[0],backend,m,p)
            self.assertTrue(rows[0]['G_defined'])
            original=new/'recovery/source/qwen25/raw'/(batches[0]['batch_id']+'.json')
            original.write_text('{}')
            with self.assertRaisesRegex(ValueError,'imported raw hash'):
                greedy.raw_batch(new/'qwen25',batches[0],m['protocol_id'])

if __name__ == '__main__':
    unittest.main()
