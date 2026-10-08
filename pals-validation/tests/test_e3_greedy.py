"""CPU-only contracts for greedy E3. No model generation or untrusted execution."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import greedy, greedy_data
from pals_validation.e3.deployment import profile
from pals_validation.e3.backend import generation_options, E3MockBackend
from pals_validation.e3.fixture import prepare_fixture
from pals_validation.e3.greedy_parse import parse
from pals_validation.e3.protocol import parse_trace, score_text_pair, messages
from pals_validation.e3.greedy_report import group_summary, estimate
from pals_validation.io import read, save, sha256, digest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("greedy_submit_test_module", ROOT / "scripts/e3_greedy_submit.py")
SUBMIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBMIT)


def fixture(root):
    prepared = root / "prepared"
    prepare_fixture(prepared)
    old = read(prepared / "manifest.json")
    # Synthetic fixture metadata only; never modify any real frozen input.
    old["schema_version"] = "pals_e3_greedy_mock_v1"
    (prepared / "manifest.json").write_text(__import__("json").dumps(old))
    configs = {}
    for slot, name in greedy.SLOTS.items():
        config = greedy.make_config(read(ROOT / "configs/e3-mock.json"))
        config["model"]["id"] = name
        if slot == "qwen3":
            config["hf_runtime"]["chat_template_kwargs"] = {"enable_thinking": False}
        configs[slot] = config
    run = root / "run"
    greedy.init(prepared, configs, {}, run)
    return run


class ParseTests(unittest.TestCase):
    def test_lossless_and_exact_predecessor_deletion(self):
        for raw in ("<step>A</step>\n<step>B</step><answer>C</answer>",
                    "<step>A\n<step>B<answer>C</answer>",
                    "<step>A</step\n<step>B</step><answer>C</answer>"):
            p = parse(raw, "boundary", "gpqa")
            self.assertTrue(p["process_valid"], p)
            full, deleted, target = score_text_pair("PROMPT", p, 1)
            self.assertEqual(target, "B")
            self.assertEqual(full.replace(raw[slice(*p["steps"][0]["block_span"])], "", 1), deleted)
            for step in p["steps"]:
                self.assertEqual(step["text"], raw[slice(*step["span"])])

    def test_no_invented_steps_and_no_ambiguous_repair(self):
        for raw in ("free prose <answer>A</answer>", "<step>One sentence. Two sentences.",
                    "<step>A</step extra><answer>A</answer>",
                    "<step>A</step><answer>A</answer><answer>B</answer>"):
            self.assertFalse(parse(raw, "eos", "gpqa")["process_valid"])
        p = parse("<step>One sentence. Two sentences.</step><answer>A</answer>", "boundary", "gpqa")
        self.assertEqual(len(p["steps"]), 1)

    def test_answer_truncation_is_not_reasoning_truncation(self):
        p = parse("<step>A</step><step>B</step><answer>unfinished", "length", "humaneval")
        self.assertTrue(p["process_valid"])
        self.assertFalse(p["answer"]["valid"])
        self.assertFalse(parse("<step>A</step><step>unfinished", "length", "humaneval")["process_valid"])

    def test_old_parser_unchanged(self):
        raw = "<step>A<step>B</step><answer>C</answer>"
        self.assertFalse(parse_trace(raw, "boundary")["process_valid"])
        self.assertTrue(parse(raw, "boundary", "gpqa")["process_valid"])


class ProtocolTests(unittest.TestCase):
    def test_greedy_does_not_use_sampling_temperature(self):
        cfg = greedy.make_config(read(ROOT / "configs/e3-mock.json"))
        greedy.validate_config(cfg)
        options = generation_options(cfg["generation"])
        self.assertFalse(options["do_sample"])
        for name in ("temperature", "top_p", "top_k"):
            self.assertNotIn(name, options)
        bad = deepcopy(cfg)
        bad["generation"]["do_sample"] = True
        with self.assertRaises(ValueError):
            greedy.validate_config(bad)
        with self.assertRaises(ValueError):
            generation_options(bad["generation"])
        self.assertEqual(cfg["scoring"]["temperature"], 1.)

    def test_official_source_no_dag_no_content_dedup(self):
        row = {"question": "Q?", "choices": ["A", "B", "C", "D"], "answer": 0}
        problems, gold = greedy_data.adapt_subject([row, row], "anatomy", greedy_data.MMLU_REVISION)
        self.assertEqual(len(problems), 2)
        self.assertNotEqual(problems[0]["problem_id"], problems[1]["problem_id"])
        self.assertNotIn("answer", problems[0])
        self.assertNotIn("reference", str(messages(problems[0], "native-greedy-prompt-v1")))
        self.assertEqual(len(greedy_data.SUBJECTS), 57)
        self.assertEqual(sum(greedy_data.COUNTS.values()), 15898)
        ambiguous = {**row, "choices": ["A", "A", "C", "D"]}
        p, g = greedy_data.adapt_subject([ambiguous], "anatomy", greedy_data.MMLU_REVISION)
        self.assertFalse(g[p[0]["problem_id"]]["outcome_eligible"])


class LifecycleTests(unittest.TestCase):
    def test_full_mock_roundtrip_all_models_eight_shards(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fixture(Path(temp))
            for slot in greedy.SLOTS:
                for shard in range(8):
                    self.assertTrue(greedy.gpu_worker(run, slot, shard, barrier=False))
                    greedy.evaluate(run, slot, shard)
                self.assertEqual(greedy.audit(run, slot)["items"], 5)
                audit = read(run / slot / "audit.json")
                self.assertFalse(audit["scientific_evidence"])
                self.assertTrue(all(r["summary"]["M"] == .2 or abs(r["summary"]["M"] - .2) < 1e-9 for r in audit["items"]))

    def test_completed_raw_never_regenerated(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fixture(Path(temp))
            m, c, p, batches = greedy.load(run, "qwen25")
            backend = E3MockBackend("", c)
            greedy.process_batch(run, "qwen25", batches[0], backend, m, p)
            with patch.object(backend, "generate_batch", side_effect=AssertionError("must not regenerate")):
                greedy.process_batch(run, "qwen25", batches[0], backend, m, p)

    def test_uncertain_attempt_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fixture(Path(temp))
            m, c, p, batches = greedy.load(run, "qwen25")
            b = batches[0]
            save(run / "qwen25/attempts" / (b["batch_id"] + ".json"), {"protocol_id": m["protocol_id"], "batch": b})
            with self.assertRaisesRegex(ValueError, "UNCERTAIN_GENERATION"):
                greedy.process_batch(run, "qwen25", b, E3MockBackend("", c), m, p)

    def test_evaluator_infrastructure_failure_is_preserved_and_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fixture(Path(temp))
            greedy.gpu_worker(run, "qwen25", 0, barrier=False)
            with patch.object(greedy, "evaluate_answer", return_value={"status": "infrastructure_error", "correct": None}):
                with self.assertRaisesRegex(ValueError, "infrastructure failure"):
                    greedy.evaluate(run, "qwen25", 0)
            files = list((run / "qwen25/outcomes").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(read(files[0])["status"], "infrastructure_error")
            with patch.object(greedy, "evaluate_answer", side_effect=AssertionError("do not retry")):
                with self.assertRaisesRegex(ValueError, "prior evaluator"):
                    greedy.evaluate(run, "qwen25", 0)

    def test_wrong_raw_hash_and_config_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fixture(Path(temp))
            m, c, p, bs = greedy.load(run, "qwen25")
            greedy.process_batch(run, "qwen25", bs[0], E3MockBackend("", c), m, p)
            raw = run / "qwen25/raw" / (bs[0]["batch_id"] + ".json")
            raw.write_text(raw.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "sealed"):
                greedy.raw_batch(run / "qwen25", bs[0], m["protocol_id"])
            (run / "qwen25/config.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Integrity"):
                greedy.load(run, "qwen25")

    def test_missing_not_zero_and_question_weighted(self):
        rows = [{"summary": {"G": g, "M": 0., "NLL": 1.}, "process_valid": True,
                 "process_reason": None, "step_count": steps, "correct": None,
                 "outcome_status": "not_evaluated"} for g, steps in ((1., 2), (3., 20))]
        rows.append({**rows[0], "summary": {"G": None}, "step_count": 1})
        report = group_summary(rows)
        self.assertEqual(report["G"]["mean"], 2.)
        self.assertEqual(report["scoreable"], 2)
        self.assertEqual(report["total"], 3)
        self.assertIsNone(estimate([])["mean"])

    def test_reference_is_recomputed_not_trusted(self):
        config = greedy.make_config(read(ROOT / "configs/e3-mock.json"))
        receipt = greedy.reference(E3MockBackend("", config), {"problem_id": "synthetic"}, "protocol")
        greedy.verify_reference(receipt, "protocol", False)
        receipt["first"]["score"]["g"] += 1
        with self.assertRaisesRegex(ValueError, "arithmetic"):
            greedy.verify_reference(receipt, "protocol", False)


class SchedulerTests(unittest.TestCase):
    def test_failed_init_cannot_launch(self):
        for output in ("10|COMPLETED|0:0\n10.batch|FAILED|1:0\n10.0|COMPLETED|0:0",
                       "10|COMPLETED|0:0\n10.batch|COMPLETED|0:0"):
            with patch.object(SUBMIT.subprocess, "check_output", return_value=output):
                with self.assertRaisesRegex(ValueError, "CPU init"):
                    SUBMIT.completed("10")

    def test_chain_is_serial_and_cannot_be_submitted_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            fence = Path(temp).resolve()
            root, repo, prepared = [fence / name for name in ("run", "repo", "prepared")]
            for path in (root, repo, prepared):
                path.mkdir()
            (root / "preflight").mkdir()
            (root / "experiment").mkdir()
            save(root / "experiment/manifest.json", {"protocol_id": "test"})
            save(root / "preflight/selftest.json", {"status": "PASS"})
            save(root / "preflight/context-checks.json", {})
            save(root / "preflight/init.json", {"status": "PASS", "job_id": "10", "run": str(root / "experiment"),
                "protocol_id": "test", "selftest_sha256": sha256(root / "preflight/selftest.json"),
                "context_checks_sha256": sha256(root / "preflight/context-checks.json")})
            commands = []
            def fake_command(command, **kwargs):
                if command[0] == "sacct":
                    return "10|COMPLETED|0:0\n10.batch|COMPLETED|0:0\n10.0|COMPLETED|0:0"
                if command[0] in ("git", "squeue"):
                    return ""
                self.assertEqual(command[0], "sbatch")
                commands.append(command)
                return str(100 + len(commands))
            with patch.object(SUBMIT, "profile", return_value=replace(profile(), root=str(fence))), patch.object(SUBMIT, "load", return_value=(
                    {"protocol_id": "test"}, None, None, None)), patch.object(
                    SUBMIT.subprocess, "check_output", side_effect=fake_command):
                jobs = SUBMIT.submit(root, repo, prepared, "10")
                self.assertEqual(len(jobs), 6)
                self.assertEqual(sum("--gpus-per-node=8" in c for c in commands), 3)
                for index, command in enumerate(commands[1:]):
                    self.assertIn("--dependency=afterok:" + jobs[index]["job_id"], command)
                    self.assertIn("--kill-on-invalid-dep=yes", command)
                with self.assertRaisesRegex(ValueError, "already attempted"):
                    SUBMIT.submit(root, repo, prepared, "10")
                self.assertEqual(len(commands), 6)


if __name__ == "__main__":
    unittest.main()
