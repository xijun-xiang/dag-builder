"""Offline protocol dispatch regression tests for the detached launcher."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from dag_builder.humaneval_recovery import HumanEvalRecoveryPipeline
from dag_builder.pipeline import Pipeline
from dag_builder.repair import RepairPipeline
from dag_builder.repair_loop import RevisionPipeline
from dag_builder.storage import write_once


class RunnerTests(unittest.TestCase):
    def test_protocol_dispatch_and_recovery_guard(self):
        script = Path(__file__).resolve().parents[1] / "scripts/run_private_pilot.py"
        spec = importlib.util.spec_from_file_location("private_runner_dispatch", script)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for protocol, expected in (
                ("gpqa-reference-v1", Pipeline),
                ("gpqa-revision-v1", RevisionPipeline),
                ("gpqa-repair-v1", RepairPipeline),
            ):
                config = SimpleNamespace(task_type="gpqa", prompt_version=protocol)
                self.assertIs(runner.pipeline_type(root, config), expected)
            config = SimpleNamespace(task_type="humaneval", prompt_version="humaneval-reference-v4")
            self.assertIs(runner.pipeline_type(root, config), Pipeline)
            write_once(root / "recovery_manifest.json", {"protocol": "humaneval-recovery-v1"})
            self.assertIs(runner.pipeline_type(root, config), HumanEvalRecoveryPipeline)
            config.prompt_version = "humaneval-reference-v3"
            with self.assertRaisesRegex(ValueError, "v4 protocol"):
                runner.pipeline_type(root, config)


if __name__ == "__main__":
    unittest.main()
