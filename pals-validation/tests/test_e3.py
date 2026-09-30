"""Scientific and lifecycle regression gates for E3; no real model or code execution."""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from pals_validation.e3.audit import audit_run
from pals_validation.e3.backend import AnswerBoundaryTracker
from pals_validation.e3.data import common_subset
from pals_validation.e3.evaluate import evaluate_answer
from pals_validation.e3.fixture import prepare_fixture
from pals_validation.e3.metrics import summarize_trace
from pals_validation.e3.protocol import messages, parse_trace, score_text_pair
from pals_validation.e3.run import canary_batches, init_run, validate_config, worker
from pals_validation.e3.schema import make_problem, validate_problem
from pals_validation.io import read, save, sha256


ROOT = Path(__file__).resolve().parents[1]


class MathAndProtocolTests(unittest.TestCase):
    def test_population_w_and_negative_part(self):
        rows = [{"g": g, "v": max(-g, 0), "full_nll": 1., "deleted_nll": 2.}
                for g in (-1., 1., 2.)]
        summary = summarize_trace(rows)
        self.assertAlmostEqual(summary["G"], 2 / 3)
        self.assertAlmostEqual(summary["M"], 1 / 3)
        self.assertAlmostEqual(summary["W"], math.sqrt(14) / 3)
        self.assertEqual(summarize_trace([])["W"], None)
        self.assertEqual(summarize_trace(rows[:1])["W"], None)
        self.assertEqual(summarize_trace([rows[1], rows[2]])["W"], .5)

    def test_parser_preserves_text_and_only_removes_adjacent_step(self):
        raw = "<step>first\n</step>\n<step>second</step>\n<step>third</step>\n<answer>A</answer>"
        parsed = parse_trace(raw, "boundary")
        self.assertTrue(parsed["process_valid"])
        self.assertEqual(parsed["steps"][0]["text"], "first\n")
        full, deleted, target = score_text_pair("BASE", parsed, 2)
        self.assertEqual(target, "third")
        self.assertIn("<step>first\n</step>", deleted)
        self.assertNotIn("<step>second</step>", deleted)
        self.assertEqual(full.replace("<step>second</step>", ""), deleted)

    def test_independent_answer_with_bad_process(self):
        parsed = parse_trace("bad prelude <answer>A</answer>", "boundary")
        self.assertFalse(parsed["process_valid"])
        self.assertTrue(parsed["answer"]["valid"])
        self.assertEqual(parsed["answer"]["text"], "A")
        for raw in ("<step>a</step><step>b</step>",
                    "<step>a<step>b</step><answer>A</answer>",
                    "<step>a</step><answer>A</answer><answer>B</answer>"):
            self.assertFalse(parse_trace(raw, "length")["process_valid"])

    def test_problem_public_allowlist_and_grading(self):
        problem = make_problem(benchmark="gpqa", problem_id="gpqa:x", subset="diamond",
                               source_id="source-x", source_revision="revision-x",
                               source_record_sha256="hash-x", question="Choose one",
                               choices=["one", "two", "three", "four"])
        self.assertNotIn("GOLD_UNIQUE_SECRET", json.dumps(messages(problem)))
        corrupt = {**problem, "reference_answer": "GOLD_UNIQUE_SECRET"}
        with self.assertRaises(ValueError):
            validate_problem(corrupt)
        v2 = messages(problem, "native-trace-prompt-v2")
        self.assertIn("first characters", v2[0]["content"])
        self.assertIn("Response format reminder", v2[1]["content"])
        self.assertNotIn("GOLD_UNIQUE_SECRET", json.dumps(v2))
        with self.assertRaisesRegex(ValueError, "prompt version"):
            messages(problem, "unregistered-prompt")
        answer = {"valid": True, "text": "A", "span": [0, 1]}
        self.assertTrue(evaluate_answer(problem, {"kind": "choice", "value": "A"}, answer)["correct"])
        self.assertIsNone(evaluate_answer(problem, {"kind": "choice", "value": "A",
                                                "outcome_eligible": False,
                                                "ineligibility_reason": "duplicate_choice_text"},
                                          answer)["correct"])

    def test_common_subset_stable(self):
        rows = [make_problem(benchmark="gsm8k", problem_id=f"gsm8k:{i}", subset="test",
                             source_id=str(i), source_revision="r", source_record_sha256="h",
                             question=f"What is {i}?") for i in range(25)]
        self.assertEqual(common_subset(rows, 2026092903), common_subset(list(reversed(rows)), 2026092903))
        self.assertEqual(len(common_subset(rows, 2026092903)), 3)

    def test_batch_tracker_keeps_individual_stop_lengths(self):
        class Decoder:
            def decode(self, tokens, skip_special_tokens=True):
                return "".join(chr(t) for t in tokens if t != 0)
        tracker = AnswerBoundaryTracker(Decoder(), 2, 2, [0])
        first = [[1, 1] + list(map(ord, "<answer>A</answer>")),
                 [2, 2] + list(map(ord, "unfinished"))]
        self.assertEqual(tracker.update(first), [True, False])
        fixed = tracker.lengths[0]
        second = [[1, 1] + list(map(ord, "<answer>A</answer>")) + [0],
                  [2, 2] + list(map(ord, "<answer>B</answer>"))]
        self.assertEqual(tracker.update(second), [True, True])
        self.assertEqual(tracker.lengths[0], fixed)


class LifecycleTests(unittest.TestCase):
    def test_v2_selects_only_untouched_second_batches(self):
        original = read(ROOT / "configs/e3-mock.json")
        v2 = {**original, "schema_version": "pals_e3_config_v2",
              "protocol_version": "native-trace-v2",
              "prompt_version": "native-trace-prompt-v2", "canary_batch_index": 1}
        validate_config(v2)
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            validate_config({**original, "canary_batch_index": 1})
        with self.assertRaisesRegex(ValueError, "untouched second batch"):
            validate_config({**v2, "canary_batch_index": 0})
        batches = [{"benchmark": benchmark, "batch_id": benchmark + str(index),
                    "problem_ids": [f"{benchmark}:{index}:{item}" for item in range(8)]}
                   for benchmark in ("gpqa", "gsm8k", "humaneval", "livecodebench", "mmlu")
                   for index in range(2)]
        first, second = canary_batches(batches), canary_batches(batches, 1)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertFalse(first & second)
        with self.assertRaisesRegex(ValueError, "absent or incomplete"):
            canary_batches(batches[:-1], 1)

    def test_v2_mock_canary_replays_only_second_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_fixture(root / "seed")
            originals = read(root / "seed" / "problems.json")
            original_grading = read(root / "seed" / "grading" / "answers.json")
            prepared = root / "prepared"
            prepared.mkdir()
            (prepared / "grading").mkdir()
            problems, grading = [], {}
            for problem in originals:
                for index in range(16):
                    clone = copy.deepcopy(problem)
                    clone["problem_id"] = problem["problem_id"].replace("-0", f"-{index:02d}")
                    clone["source_id"] = clone["problem_id"]
                    problems.append(clone)
                    grading[clone["problem_id"]] = original_grading[problem["problem_id"]]
            save(prepared / "problems.json", problems)
            save(prepared / "grading" / "answers.json", grading)
            save(prepared / "common_subset.json", common_subset(problems, 2026092903))
            manifest = {**read(root / "seed" / "manifest.json"),
                        "counts": {p["benchmark"]: 16 for p in originals},
                        "files": {name: sha256(prepared / name) for name in
                                  ("problems.json", "grading/answers.json", "common_subset.json")}}
            save(prepared / "manifest.json", manifest)
            config = {**read(ROOT / "configs/e3-mock.json"),
                      "schema_version": "pals_e3_config_v2", "protocol_version": "native-trace-v2",
                      "prompt_version": "native-trace-prompt-v2", "canary_batch_index": 1}
            save(root / "v2-config.json", config)
            run = root / "run"
            init_run(prepared, root / "v2-config.json", run)
            for stage in ("generate", "score", "evaluate"):
                for shard in range(8):
                    worker(run, stage, shard, canary=True)
            result = audit_run(run, "canary", root / "audit")
            self.assertEqual(result["counts"]["planned"], 40)
            self.assertEqual(result["counts"]["complete_process"], 40)
            batches = read(run / "batches.json")
            self.assertEqual({path.stem for path in (run / "generation_batches").glob("*.json")},
                             canary_batches(batches, 1))
            self.assertFalse({path.stem for path in (run / "generation_batches").glob("*.json")}
                             & canary_batches(batches))

    def _new_run(self, root: Path):
        prepared = root / "prepared"
        prepare_fixture(prepared)
        run = root / "run"
        init_run(prepared, ROOT / "configs/e3-mock.json", run)
        return run

    def test_mock_end_to_end_and_evidence_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = self._new_run(root)
            for stage in ("generate", "score", "evaluate"):
                for shard in range(8):
                    worker(run, stage, shard)
            audited = audit_run(run, "all", root / "audit")
            self.assertEqual(audited["counts"]["planned"], 5)
            self.assertEqual(audited["counts"]["G_valid"], 5)
            self.assertEqual(audited["counts"]["W_valid"], 0)
            self.assertFalse(audited["scientific_evidence"])
            self.assertEqual(worker(run, "generate", 0)["problems"], 1)
            score_file = next((run / "scores").glob("*.json"))
            corrupt = read(score_file)
            corrupt["steps"][0]["evidence"]["full_logprobs"][0] += .1
            score_file.write_text(json.dumps(corrupt), encoding="utf-8")
            with self.assertRaises(ValueError):
                audit_run(run, "all", root / "tampered-audit")

    def test_uncertain_attempt_is_not_regenerated(self):
        with tempfile.TemporaryDirectory() as temp:
            run = self._new_run(Path(temp))
            batch = read(run / "batches.json")[0]
            protocol = read(run / "protocol.json")
            save(run / "attempts" / (batch["batch_id"] + ".json"),
                 {"batch_id": batch["batch_id"], "protocol_id": protocol["protocol_id"],
                  "state": "attempt_started"})
            with self.assertRaisesRegex(ValueError, "UNCERTAIN_GENERATION"):
                worker(run, "generate", 0)
            self.assertFalse((run / "generation_batches" / (batch["batch_id"] + ".json")).exists())

    def test_token_id_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = self._new_run(root)
            for stage in ("generate", "score"):
                for shard in range(8):
                    worker(run, stage, shard)
            score_file = next((run / "scores").glob("*.json"))
            corrupted = read(score_file)
            corrupted["steps"][0]["evidence"]["target_ids"][0] += 1
            score_file.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "token IDs"):
                audit_run(run, "score", root / "tampered-token-audit")

    def test_canary_then_formal_reuses_committed_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = self._new_run(root)
            for stage in ("generate", "score", "evaluate"):
                for shard in range(8):
                    worker(run, stage, shard, canary=True)
            canary = audit_run(run, "canary", root / "canary-audit")
            self.assertEqual(canary["counts"]["planned"], 5)
            before = {path.name: path.read_bytes() for path in
                      (run / "generation_batches").glob("*.json")}
            for stage in ("generate", "score", "evaluate"):
                for shard in range(8):
                    worker(run, stage, shard)
            self.assertEqual(before, {path.name: path.read_bytes() for path in
                                      (run / "generation_batches").glob("*.json")})
            self.assertEqual(audit_run(run, "all", root / "full-audit")["counts"]["planned"], 5)


if __name__ == "__main__":
    unittest.main()
