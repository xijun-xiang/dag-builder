"""Offline compatibility checks for the merged dataset/source routing matrix."""

import unittest
from pathlib import Path

from dag_builder.config import Config
from dag_builder.report import report_title
from dag_builder.stages import (
    REFERENCE_STAGES,
    REPAIR_STAGES,
    THINKING_STAGES,
    prompt,
    stages_for,
)


class MergedRouteTests(unittest.TestCase):
    def test_all_shipped_configs_load_and_resolve_their_prompts(self):
        paths = sorted((Path(__file__).parent.parent / "configs").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(config=path.name):
                config = Config.load(path)
                self.assertEqual(Config(**config.to_dict()), config)
                for stage in stages_for(config):
                    self.assertTrue(
                        prompt(
                            stage,
                            config.prompt_version,
                            config.task_type,
                            config.solution_source,
                        ).strip()
                    )

    def test_gsm8k_source_modes_do_not_replace_other_dataset_protocols(self):
        for version, task, stages, extra in [
            ("mmlu-thinking-v1", "mmlu", THINKING_STAGES, {"thinking": "enabled"}),
            ("gpqa-reference-v1", "gpqa", REFERENCE_STAGES, {}),
            ("gpqa-repair-v1", "gpqa", REPAIR_STAGES, {}),
        ]:
            with self.subTest(protocol=version):
                config = Config(prompt_version=version, task_type=task, **extra)
                self.assertEqual(stages_for(config), stages)
                self.assertNotIn("solution_source", config.to_dict())
                with self.assertRaisesRegex(ValueError, "require gsm8k"):
                    Config(
                        prompt_version=version,
                        task_type=task,
                        solution_source="answer_conditioned_generation",
                        **extra,
                    )

    def test_report_titles_cover_both_reference_datasets(self):
        self.assertIn("GPQA-Diamond", report_title([{"task_type": "gpqa"}], {}))
        self.assertIn("GSM8K", report_title([{"task_type": "gsm8k"}], {}))


if __name__ == "__main__":
    unittest.main()
