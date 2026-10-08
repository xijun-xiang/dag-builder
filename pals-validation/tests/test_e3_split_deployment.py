"""Keep per-model scientific inputs invariant when separating deployment sites."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import greedy, greedy_extension as extension, deployment
from pals_validation.e3.compat_runtime import verify_tree
from pals_validation.io import read, sha256
from test_e3_extension import inputs
import test_e3_held_submit as held
from test_e3_greedy import SUBMIT


class SplitDeploymentTests(unittest.TestCase):
    def test_single_model_same_config_batches_and_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prepared, configs = inputs(base)
            full = base / "full"
            greedy.init(prepared, configs, {}, full)
            for slot in extension.SLOTS:
                single = base / slot
                greedy.init(prepared, {slot: configs[slot]}, {}, single, deployment_slot=slot)
                manifest = read(single / "manifest.json")
                self.assertEqual(greedy.model_slots(manifest), {slot: extension.SLOTS[slot]})
                for name in ("config.json", "batches.json"):
                    self.assertEqual(read(full / slot / name), read(single / slot / name))
                self.assertEqual(read(full / "inputs/problems.json"), read(single / "inputs/problems.json"))
                for shard in range(8):
                    greedy.gpu_worker(single, slot, shard, barrier=False)
                    greedy.evaluate(single, slot, shard)
                self.assertEqual(greedy.audit(single, slot)["items"], 5)
                other = next(s for s in extension.SLOTS if s != slot)
                with self.assertRaises(ValueError):
                    greedy.load(single, other)

    def test_single_scope_is_explicit_and_cannot_disguise_missing_model(self):
        manifest = {"protocol_version": extension.VERSION,
                    "model_slots": {"internlm3": extension.SLOTS["internlm3"]}}
        with self.assertRaises(ValueError):
            greedy.model_slots(manifest)
        for slot in ("qwen25", "llama3"):
            with self.assertRaises(ValueError):
                greedy.model_slots({**manifest, "deployment_slot": slot})
        self.assertEqual(len(greedy.model_slots({**manifest, "deployment_slot": "internlm3"})), 1)

    def test_single_model_two_held_jobs_not_four(self):
        with held.HeldTests().setup_scheduler() as (root, repo, prepared, states, commands, releases):
            manifest = {"protocol_id":"test", "protocol_version":extension.VERSION,
                        "deployment_slot":"internlm3", "model_slots":{"internlm3":extension.SLOTS['internlm3']}}
            (root / "experiment/manifest.json").write_text(__import__('json').dumps(manifest))
            with patch.object(SUBMIT, "load", return_value=(manifest, None, None, None)):
                jobs = SUBMIT.submit(root, repo, prepared, '10', [held.EXCLUDED])
                self.assertEqual(len(jobs), 2)
                SUBMIT.release(root, repo, prepared, '10')
                self.assertEqual(releases, ['102', '101'])

    def test_b1_code_agent_is_separate_profile(self):
        with patch.dict(os.environ, {"PALS_CLUSTER":"b1-code-agent"}):
            self.assertEqual(deployment.profile().reservation, "code-agent")
            self.assertEqual(deployment.profile().root, "/work/projects/polyullm/xxj/PALS")
        self.assertIsNone(deployment.PROFILES["b1"].reservation)

    def test_compatibility_dependency_manifest_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "module.py").write_text("value = 1\n")
            files = {"module.py":sha256(root / "module.py")}
            self.assertEqual(verify_tree(root, files), 1)
            (root / "extra.py").write_text("value = 2\n")
            with self.assertRaisesRegex(ValueError, "extra"):
                verify_tree(root, files)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_tree(root, {"../outside.py":"bad"})
            with self.assertRaisesRegex(ValueError, "changed"):
                verify_tree(root, {"module.py":"bad"})


if __name__ == "__main__":
    unittest.main()
