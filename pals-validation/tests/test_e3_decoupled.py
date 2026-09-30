"""Adversarial parsing and immutable replay tests; generated code is never run."""

import copy
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from pals_validation.e3.decoupled import parse_decoupled
from pals_validation.e3.derived_score import audit_scores, score_shard
from pals_validation.e3.fixture import prepare_fixture
from pals_validation.e3.protocol import parse_trace, score_text_pair
from pals_validation.e3.reparse import replay_sources
from pals_validation.e3.run import init_run, worker
from pals_validation.io import encoded, read, save, sha256

ROOT = Path(__file__).resolve().parents[1]
STEPS = "<step>one\n</step>\n<step>two</step>\n<step>three</step>\n"
FENCE = "```python\ndef f():\n    return 1\n```\n"


class DecoupledParsingTests(unittest.TestCase):
    def test_answer_suffix_cannot_change_scoring_context(self):
        suffixes = ("<answer>def f():\n    return 1</answer>",
                    "<answer>def f():\n    return 1", FENCE, "answer:\n" + FENCE,
                    "<answer>" + FENCE, "<answer>this is not valid Python")
        baseline = parse_trace(STEPS + suffixes[0], "eos")
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                parsed = parse_decoupled(STEPS + suffix, "eos", "humaneval")
                self.assertTrue(parsed["process_valid"])
                self.assertEqual(parsed["steps"], baseline["steps"])
                for index in (1, 2):
                    self.assertEqual(score_text_pair("ORIGINAL_PROMPT", parsed, index),
                                     score_text_pair("ORIGINAL_PROMPT", baseline, index))
                self.assertIsNone(parsed["status"]["answer_correct"])

    def test_answer_spans_are_exact_substrings(self):
        for suffix in ("<answer>" + FENCE + "</answer>", "<answer>" + FENCE,
                       FENCE, "Answer:\n" + FENCE, "<answer>plain code"):
            raw = STEPS + suffix
            parsed = parse_decoupled(raw, "eos", "livecodebench")
            self.assertTrue(parsed["answer"]["valid"])
            start, end = parsed["answer"]["span"]
            self.assertEqual(raw[start:end], parsed["answer"]["text"])
            self.assertEqual(parsed["raw_text"], raw)

    def test_no_partial_process_salvage(self):
        bad = ("preface\n" + STEPS + "<answer>A</answer>",
               "<step>a</step>untagged reasoning<step>b</step><answer>A</answer>",
               "<step>a</step><step>not closed<answer>A</answer>",
               "<step>a<step>b</step><answer>A</answer>",
               "<step></step><answer>A</answer>",
               STEPS + "Some more calculations\n<answer>A</answer>",
               STEPS, STEPS + "<answer>A</answer>new reasoning",
               STEPS + "<answer>A</answer><step>later</step>",
               STEPS + "<answer>A</answer><answer>B</answer>",
               STEPS + "<answer>A</step", STEPS + "<answer>A<Step>")
        for raw in bad:
            with self.subTest(raw=raw):
                self.assertFalse(parse_decoupled(raw, "eos", "gpqa")["process_valid"])

    def test_no_truncation_or_unknown_finish_salvage(self):
        for finish in ("length", "error", "unknown"):
            parsed = parse_decoupled(STEPS + "<answer>A</answer>", finish, "gpqa")
            self.assertFalse(parsed["process_valid"])
            self.assertFalse(parsed["answer"]["valid"])
        self.assertFalse(parse_decoupled(STEPS + "<answer>A", "boundary", "gpqa")["process_valid"])

    def test_bad_answer_does_not_destroy_delimited_process(self):
        parsed = parse_decoupled(STEPS + "<answer>" + FENCE + "```", "eos", "livecodebench")
        self.assertTrue(parsed["process_valid"])
        self.assertFalse(parsed["answer"]["valid"])
        self.assertEqual(parsed["answer"]["reason"], "ambiguous_code_fence")
        parsed = parse_decoupled(STEPS + "<answer></answer>", "eos", "gpqa")
        self.assertTrue(parsed["process_valid"])
        self.assertFalse(parsed["answer"]["valid"])

    def test_answer_still_independent_of_bad_steps(self):
        raw = "unlabelled reasoning\n<answer>A</answer>"
        parsed = parse_decoupled(raw, "boundary", "gpqa")
        self.assertFalse(parsed["process_valid"])
        self.assertTrue(parsed["answer"]["valid"])
        self.assertEqual(parsed["answer"]["text"], "A")

    def test_code_fences_not_silently_accepted_on_noncode_tasks(self):
        for benchmark in ("gpqa", "gsm8k", "mmlu"):
            self.assertFalse(parse_decoupled(STEPS + FENCE, "eos", benchmark)["process_valid"])

    def test_ambiguous_code_and_prose_not_guessed(self):
        for suffix in (FENCE + FENCE, "Here is the implementation:\n" + FENCE,
                       FENCE + "more reasoning", "answer:\n" + FENCE + "<answer>A</answer>"):
            self.assertFalse(parse_decoupled(STEPS + suffix, "eos", "humaneval")["process_valid"])


class ReparseTests(unittest.TestCase):
    def test_all_sources_replay_without_modifying_old_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_fixture(root / "prepared")
            source = root / "source"
            init_run(root / "prepared", ROOT / "configs/e3-mock.json", source)
            for shard in range(8):
                worker(source, "generate", shard, canary=True)
            # A synthetic EOS answer-envelope failure, still with complete steps.
            raw_file = next((source / "generation_batches").glob("*.json"))
            raw = read(raw_file)
            row = raw["rows"][0]
            row["raw_text"] = row["raw_text"].replace("</answer>", "")
            row["finish_reason"] = "eos"
            row["parse"] = parse_trace(row["raw_text"], "eos")
            raw_file.write_bytes(encoded(raw))
            before = {str(p.relative_to(source)): sha256(p) for p in source.rglob("*") if p.is_file()}
            input_path = root / "input.json"
            save(input_path, {"runs": [{"label": "mock", "path": str(source), "remote_source": None}]})
            result = replay_sources(input_path, root / "replay")
            self.assertEqual(result["total"], 5)
            self.assertEqual(sum(g["recovered"] for g in result["groups"]), 1)
            self.assertFalse(read(root / "replay/manifest.json")["sources"][0]["scientific_evidence"])
            self.assertEqual(before, {str(p.relative_to(source)): sha256(p) for p in source.rglob("*") if p.is_file()})
            for item in read(root / "replay/items.json"):
                self.assertIsNone(item["G"])
                self.assertIsNone(item["parse"]["status"]["answer_correct"])
            with patch("pals_validation.e3.backend.E3MockBackend.generate_batch",
                       side_effect=AssertionError("must not regenerate")):
                for shard in range(8):
                    score_shard(root / "replay", source, "mock", root / "derived-scores", shard)
            audited = audit_scores(root / "replay", source, "mock", root / "derived-scores")
            self.assertEqual(audited, {"status": "PASS", "items": 5})
            with self.assertRaisesRegex(ValueError, "already exists"):
                score_shard(root / "replay", source, "mock", root / "derived-scores", 0)
            self.assertEqual(before, {str(p.relative_to(source)): sha256(p) for p in source.rglob("*") if p.is_file()})
            with self.assertRaisesRegex(ValueError, "already exists"):
                replay_sources(input_path, root / "replay")
            corrupted = copy.deepcopy(raw)
            corrupted["rows"][0]["parse"]["process_valid"] = True
            raw_file.write_bytes(encoded(corrupted))
            with self.assertRaisesRegex(ValueError, "strict parse"):
                replay_sources(input_path, root / "bad-replay")

    def test_old_score_comparison_and_token_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_fixture(root / "prepared")
            source = root / "source"
            init_run(root / "prepared", ROOT / "configs/e3-mock.json", source)
            for stage in ("generate", "score"):
                for shard in range(8):
                    worker(source, stage, shard, canary=True)
            save(root / "input.json", {"runs": [{"label": "mock", "path": str(source), "remote_source": None}]})
            replay_sources(root / "input.json", root / "replay")
            for label in ("good", "tampered"):
                for shard in range(8):
                    score_shard(root / "replay", source, "mock", root / label, shard)
            audit_scores(root / "replay", source, "mock", root / "good")
            for row in read(root / "good/audit.json")["items"]:
                self.assertEqual(row["baseline_comparison"]["max_abs_g_delta"], 0)
            path = root / "tampered/shard-0.json"
            bad = read(path)
            bad["items"][0]["steps"][0]["evidence"]["target_ids"][0] += 1
            path.write_bytes(encoded(bad))
            with self.assertRaisesRegex(ValueError, "token IDs"):
                audit_scores(root / "replay", source, "mock", root / "tampered")


if __name__ == "__main__":
    unittest.main()
