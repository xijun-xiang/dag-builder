"""Failure injection and resume invariants; synthetic results only."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pals_validation import campaign
from pals_validation.backend import MockBackend
from pals_validation.io import read, save, sha256
from pals_validation.locking import exclusive_lock
from pals_validation.model_policy import local_code_policy
from pals_validation.prepare import prepare
from pals_validation.run import init_run, init_or_resume, worker
from test_validation import fixture


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source.jsonl"
        source.write_text(json.dumps(fixture()) + "\n")
        self.prepared = self.root / "prepared"
        prepare(source, self.prepared)
        self.config = self.root / "config.json"
        save(self.config, read(Path(__file__).parents[1] / "configs/mock.json"))

    def init(self, name="run", experiment="e2"):
        run = self.root / name
        init_run(self.prepared, self.config, run, experiment, 1)
        return run

    def test_os_releases_lock_after_sigterm_and_sigkill(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            run = self.init(str(signum))
            code = ("import sys, signal; from pathlib import Path; "
                    "from pals_validation import run as r; "
                    "from pals_validation.backend import MockBackend; "
                    "\nclass Waiting(MockBackend):\n def score(self,*a):\n  print('LOCKED',flush=True); signal.pause()\n"
                    "r.MockBackend=Waiting\nr.worker(Path(sys.argv[1]),0)")
            proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(run)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
                with self.assertRaisesRegex(RuntimeError, "Active owner"):
                    worker(run, 0)
                os.kill(proc.pid, signum)
                proc.wait(timeout=10)
                self.assertEqual(proc.returncode, -signum)
                self.assertTrue((run / "workers/0/ACTIVE.lock").exists())
                self.assertEqual(worker(run, 0)["status"], "complete")
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
                proc.stdout.close()

    def test_generation_survives_scoring_failure_without_resampling(self):
        run = self.init()
        class Failure(MockBackend):
            def score(self, *args):
                if list((run / "generations").glob("*.json")):
                    raise RuntimeError("injected scoring failure after saved generation")
                return super().score(*args)
        with patch("pals_validation.run.MockBackend", Failure):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                worker(run, 0)
        checkpoints = {p: sha256(p) for p in (run / "generations").glob("*.json")}
        self.assertEqual(len(checkpoints), 1)
        original = MockBackend.generate
        calls = []
        def counted(self, *args):
            calls.append(args)
            return original(self, *args)
        with patch.object(MockBackend, "generate", counted):
            worker(run, 0)
        self.assertEqual(len(calls), len(read(run / "jobs.json")) - 1)
        self.assertEqual({p: sha256(p) for p in checkpoints}, checkpoints)

    def test_completed_results_verified_before_skip(self):
        run = self.init()
        worker(run, 0)
        path = next((run / "results").glob("*.json"))
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "Completed result changed"):
            worker(run, 0)

    def test_resume_requires_same_inputs_config_code_and_shards(self):
        run = self.init()
        self.assertEqual(init_or_resume(self.prepared, self.config, run, "e2", 1)["status"], "resumed")
        with self.assertRaisesRegex(ValueError, "shards mismatch"):
            init_or_resume(self.prepared, self.config, run, "e2", 2)
        config = read(self.config); config["seed"] += 1
        changed = self.root / "changed.json"; save(changed, config)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            init_or_resume(self.prepared, changed, run, "e2", 1)

    def test_failed_initialization_is_not_published(self):
        with patch("pals_validation.run.shutil.copyfile", side_effect=RuntimeError("interrupted init")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.init()
        self.assertFalse((self.root / "run").exists())
        self.init()

    def test_campaign_explicit_resume_and_unique_attempts(self):
        first = campaign.attempt(self.root, {"seed": 1}, False)
        with self.assertRaises(FileExistsError):
            campaign.attempt(self.root, {"seed": 1}, False)
        with self.assertRaisesRegex(ValueError, "changed"):
            campaign.attempt(self.root, {"seed": 2}, True)
        self.assertNotEqual(first, campaign.attempt(self.root, {"seed": 1}, True))

    def test_full_phases_resume_does_not_generate_or_overwrite(self):
        def local_workers(logs, run, experiment, visible):
            for shard in range(len(visible)):
                worker(run, shard)
        logs = self.root / "attempt-1"; logs.mkdir()
        with patch.object(campaign, "workers", local_workers):
            campaign.phases(self.root, self.prepared, ["0", "1"], logs)
            raw = {p: sha256(p) for sub in ("results", "generations")
                   for p in (self.root / "e2" / sub).glob("*.json")}
            later = self.root / "attempt-2"; later.mkdir()
            with patch.object(MockBackend, "generate", side_effect=AssertionError("resampled")):
                campaign.phases(self.root, self.prepared, ["0", "1"], later)
            self.assertEqual({p: sha256(p) for p in raw}, raw)

    def test_filesystem_lock_probe(self):
        campaign.lock_probe(self.root)

    def test_symlink_lock_rejected(self):
        target = self.root / "target"; target.write_text("unchanged")
        link = self.root / "link"; link.symlink_to(target)
        with self.assertRaises(OSError):
            with exclusive_lock(link): pass
        self.assertEqual(target.read_text(), "unchanged")

    def test_workers_binding_and_cleanup(self):
        children = [Mock() for _ in range(8)]
        for proc in children:
            proc.wait.return_value = 0; proc.poll.return_value = 0
        with patch.object(campaign.subprocess, "Popen", side_effect=children) as start:
            campaign.workers(self.root, self.root / "run", "e2", [str(i) for i in range(8)])
        for index, call in enumerate(start.call_args_list):
            self.assertEqual(call.kwargs["env"]["CUDA_VISIBLE_DEVICES"], str(index))
            self.assertTrue(call.kwargs["stdout"].closed)

    def test_partial_launch_failure_terminates_only_own_children(self):
        proc = Mock(); proc.poll.return_value = None
        with patch.object(campaign.subprocess, "Popen", side_effect=[proc, OSError("fixture")]):
            with self.assertRaises(OSError):
                campaign.workers(self.root, self.root / "run", "e1", ["0", "1"])
        proc.terminate.assert_called_once()


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "implementation.py").write_text("# synthetic reviewed fixture\n")
        for name in ("config.json", "tokenizer_config.json"):
            save(self.root / name, {"auto_map": {"AutoModel": "implementation.Model"}})
        self.config = {"model_revision": "fixed", "reviewed_local_code": {"revision": "fixed",
                       "review_note": "synthetic test", "files": {"implementation.py": sha256(self.root / "implementation.py")}}}

    def test_default_denies_custom_code(self):
        self.assertFalse(local_code_policy(self.root, {}))

    def test_reviewed_code_allowed_but_changed_code_rejected(self):
        self.assertTrue(local_code_policy(self.root, self.config))
        (self.root / "implementation.py").write_text("# changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            local_code_policy(self.root, self.config)

    def test_extra_code_and_revision_mismatch_rejected(self):
        self.config["model_revision"] = "other"
        with self.assertRaisesRegex(ValueError, "revision"):
            local_code_policy(self.root, self.config)
        self.config["model_revision"] = "fixed"
        (self.root / "extra.py").write_text("# extra")
        with self.assertRaisesRegex(ValueError, "inventory"):
            local_code_policy(self.root, self.config)

    def test_external_automap_rejected(self):
        (self.root / "config.json").write_text(json.dumps({"auto_map": {"AutoModel": "remote/repo--implementation.Model"}}))
        with self.assertRaisesRegex(ValueError, "reviewed local"):
            local_code_policy(self.root, self.config)


if __name__ == "__main__":
    unittest.main()
