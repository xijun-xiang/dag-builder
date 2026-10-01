"""Synthetic import/continuation tests; no network, model or candidate execution."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_e3_outcome_recovery as recovery_tests
from pals_validation.e3 import outcome_recovery as recovery
from pals_validation.e3.continuation import prepare
from pals_validation.e3.expanded import audit, load, worker
from pals_validation.io import encoded, read, sha256


class ContinuationTests(unittest.TestCase):
    def make(self, root):
        parent, test = recovery_tests.RecoveryTests().fixture(root)
        recovered = root / 'recovery'
        recovery.prepare(parent, recovered, 'phi4mini', 0, test)
        recovery.evaluate(recovered)
        recovery.audit(recovered)
        return parent, recovered

    def test_import_then_new_wave_no_regeneration_or_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            parent, recovered = self.make(root)
            before = {str(p): sha256(p) for base in (parent,recovered) for p in base.rglob('*') if p.is_file()}
            out = root / 'continuation'
            with patch('pals_validation.e3.expanded._backend', side_effect=AssertionError('no forward pass')):
                result = prepare(parent, recovered, out)
            self.assertEqual(result['imported_audit']['counts']['planned'], 40)
            self.assertNotEqual(read(parent/'manifest.json')['expansion_id'], result['expansion_id'])
            self.assertEqual(read(out/'manifest.json')['selected_ids'],read(parent/'manifest.json')['selected_ids'])
            for stage in ('generate','score','evaluate'):
                for shard in range(8):
                    worker(out,'qwen25',0,stage,shard)
            self.assertEqual(audit(out,'qwen25',0)['status'],'PASS')
            for stage in ('generate','score','evaluate'):
                for shard in range(8):
                    worker(out,'phi4mini',1,stage,shard)
            self.assertEqual(audit(out,'phi4mini',1)['status'],'PASS')
            self.assertEqual(before,{str(p):sha256(p) for base in (parent,recovered) for p in base.rglob('*') if p.is_file()})
            with self.assertRaisesRegex(ValueError,'resubmitted'):
                worker(out,'phi4mini',0,'generate',0)
            score = out / next(k for k in read(out/'manifest.json')['continuation']['imported_records']
                               if k.startswith('phi4mini/scores/'))
            r=read(score);r['summary']['G']+=1
            score.write_bytes(encoded(r))
            with self.assertRaisesRegex(ValueError,'payload changed'):
                load(out,'qwen3')

    def test_unaccepted_recovery_blocks_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            parent,recovered=self.make(root)
            r=read(recovered/'audit.json');r['coverage_gate']=False
            (recovered/'audit.json').write_bytes(encoded(r))
            with self.assertRaisesRegex(ValueError,'not accepted'):
                prepare(parent,recovered,root/'blocked')
            self.assertFalse((root/'blocked').exists())
