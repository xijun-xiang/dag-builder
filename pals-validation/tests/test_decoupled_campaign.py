"""Independent E1/E2 gates, explicit budgets and unchanged scoring prompts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pals_validation import campaign
from pals_validation.backend import HFBackend
from pals_validation.io import read, save
from pals_validation.protocol import HUMANEVAL_SYSTEM, parse_step, system_prompt
from pals_validation.run import validate_config


def summary(valid=7):
    return {"complete_questions": int(valid == 8), "planned_questions": 1,
            "cells": [{"attempted": 8, "planned": 8, "valid": valid}]}


class DecoupledTests(unittest.TestCase):
    def test_partial_format_coverage_is_recorded_not_repaired(self):
        c = {"repeats": 8, "canary_coverage_policy": "report_invalid", "canary_min_valid_repeats": 2}
        report = campaign.canary_coverage(summary(), c)
        self.assertEqual(report["valid"], 7)
        self.assertEqual(report["invalid"], 1)
        self.assertTrue(report["coverage_warning"])
        with self.assertRaisesRegex(RuntimeError, "too few valid"):
            campaign.canary_coverage(summary(1), c)
        broken = summary(); broken["cells"][0]["attempted"] = 7
        with self.assertRaisesRegex(RuntimeError, "slots incomplete"):
            campaign.canary_coverage(broken, c)

    def test_legacy_gate_remains_strict(self):
        with self.assertRaisesRegex(RuntimeError, "coverage incomplete"):
            campaign.canary_coverage(summary(), {"repeats": 8})
        self.assertEqual(campaign.canary_coverage(summary(8), {"repeats": 8})["invalid"], 0)

    def test_scope_mismatch_rejected(self):
        self.assertEqual(campaign.selected_experiments({"campaign_experiment": "e1"}, "e1"), ("e1",))
        with self.assertRaisesRegex(ValueError, "differs"):
            campaign.selected_experiments({"campaign_experiment": "e1"}, "e2")

    def test_e1_never_invokes_e2_workers_or_coverage_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save(root / "config.json", {"campaign_experiment": "e1"})
            save(root / "reference.json", {"identity": {}, "absolute_nll_errors": {"full": 0., "deleted": 0.}})
            with patch.object(campaign, "reference_identity", return_value={}), \
                 patch.object(campaign, "init_or_resume") as init, \
                 patch.object(campaign, "workers") as workers, \
                 patch.object(campaign, "analyze", return_value={"accepted_jobs": 5}), \
                 patch.object(campaign, "canary_coverage", side_effect=AssertionError("E2 gate called")):
                result = campaign.run_canary(root, ["0"], experiment="e1")
            self.assertEqual(result["experiments"], ["e1"])
            self.assertIsNone(result["generation_coverage"])
            self.assertEqual(init.call_args.args[3], "e1")
            self.assertEqual(workers.call_args.args[2], "e1")

    def test_e2_phase_does_not_run_e1_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(campaign, "init_or_resume") as init, patch.object(campaign, "workers") as workers, \
                 patch.object(campaign, "analyze", return_value={"accepted_jobs": 3}):
                result = campaign.phases(root, root, ["0"], root, experiments=("e2",))
            self.assertEqual(set(result), {"e2"})
            self.assertEqual(init.call_count, 1)
            self.assertEqual(workers.call_args.args[2], "e2")

    def test_generation_prompt_changes_but_scoring_prompt_does_not(self):
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                return messages[0]["content"]
        backend = HFBackend.__new__(HFBackend)
        backend.config = {"generation_prompt_version": "humaneval-single-step-v2", "chat_template_kwargs": {}}
        backend.tokenizer = Tokenizer()
        case = {"task_type": "humaneval", "question": "Synthetic specification", "steps": []}
        self.assertEqual(system_prompt(case), HUMANEVAL_SYSTEM)
        score = backend.context(case, [])
        generation = backend.context(case, [], for_generation=True)
        self.assertTrue(score.startswith(HUMANEVAL_SYSTEM))
        self.assertNotIn("exact seven characters", score)
        self.assertIn("exact seven characters", generation)
        with self.assertRaises(ValueError):
            system_prompt({"task_type": "gpqa"}, "humaneval-single-step-v2")

    def test_bad_tags_and_cutoff_remain_invalid(self):
        for text in ("A complete-looking sentence.</step", "A sentence.[/step>", "A long unfinished sentence"):
            self.assertFalse(parse_step(text, "humaneval")["valid"])

    def test_unknown_protocol_and_invalid_gate_threshold_rejected(self):
        base = read(Path(__file__).parents[1] / "configs/mock.json")
        for change in ({"generation_prompt_version": "invented"}, {"campaign_experiment": "both"},
                       {"canary_coverage_policy": "ignore"}, {"canary_min_valid_repeats": 1}):
            with self.assertRaises(ValueError):
                validate_config({**base, **change})


if __name__ == "__main__":
    unittest.main()
