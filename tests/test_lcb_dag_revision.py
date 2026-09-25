"""Contract checks for the source-bound LiveCodeBench DAG revision protocol."""

import unittest

from dag_builder.config import Config
from dag_builder.stages import prompt, stages_for
from dag_builder.livecodebench_dag_revision import _normalize
from dag_builder.schemas import InvalidOutput


class LCBDAGRevisionTests(unittest.TestCase):
    def config(self, workers=32, version="lcb-dag-revision-v1"):
        return Config(task_type="livecodebench", prompt_version=version,
                      solution_source="reference_dag_revision", workers=workers)

    def test_32_workers_and_all_three_stages_are_explicit(self):
        config = self.config()
        self.assertEqual(stages_for(config), ("revise", "dependencies", "review_dag"))
        for stage in stages_for(config):
            self.assertIn("DATA", prompt(stage, config.prompt_version, config.task_type,
                                          config.solution_source))
        with self.assertRaisesRegex(ValueError, "at most 32"):
            self.config(33)

    def test_v2_prompts_include_mechanical_lessons(self):
        config = self.config(version="lcb-dag-revision-v2")
        self.assertEqual(stages_for(config), ("revise", "dependencies", "review_dag"))
        self.assertIn("supported ONLY by `C`", prompt(
            "revise", config.prompt_version, config.task_type, config.solution_source))
        self.assertIn("every retained normalized node", prompt(
            "dependencies", config.prompt_version, config.task_type,
            config.solution_source))

    def test_protocol_cannot_claim_an_unrelated_solution_source(self):
        with self.assertRaisesRegex(ValueError, "dedicated source label"):
            Config(task_type="livecodebench", prompt_version="lcb-dag-revision-v1",
                   solution_source="t2ance_reference_normalization")

    def test_revision_requires_an_edit_summary(self):
        with self.assertRaises((InvalidOutput, ValueError)):
            _normalize({"steps": [], "omissions": []}, {}, "CALIBRI")


if __name__ == "__main__":
    unittest.main()
