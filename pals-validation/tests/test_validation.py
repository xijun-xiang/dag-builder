"""No real benchmark text, network, model download or GPU needed."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from pals_validation.analyze import analyze, check_score, e1_analysis
from pals_validation.backend import target_positions
from pals_validation.data import normalize, topological
from pals_validation.graph import e2_anchor, forest, violations
from pals_validation.io import read, read_jsonl, save
from pals_validation.metrics import pair, repeats, trajectory
from pals_validation.prepare import prepare
from pals_validation.protocol import BoundaryTracker, parse_step
from pals_validation.run import init_run, worker


def fixture():
    # Two multi-node branches with a shared root; merging conclusion and answer.
    parents = {1: [], 2: [1], 3: [2], 4: [1], 5: [4], 6: [3, 5], 7: [6]}
    nodes = [{"node_id": i, "parents": ps, "statement": f"Synthetic statement {i}.",
              "kind": "answer" if i == 7 else "derived"} for i, ps in parents.items()]
    source = {"question": "Synthetic fixture question?", "choices": ["A", "B", "C", "D"],
              "subset": "gpqa_diamond", "row": 1, "domain": "Synthetic"}
    return {"item_id": "fixture", "model_accepted": True, "status": "model_accepted",
            "source": source, "dag": {"source": copy.deepcopy(source), "nodes": nodes}}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.steps = normalize(fixture())["steps"]

    def test_legal_keeps_edges_and_text(self):
        before = copy.deepcopy(self.steps)
        f = forest(self.steps, 20260915, "fixture")
        self.assertEqual(f["baseline"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(f["legal"], [1, 4, 5, 2, 3, 6])
        self.assertEqual(violations(self.steps, f["legal"]), [])
        self.assertEqual(self.steps, before)

    def test_chain_has_no_legal_branch_swap(self):
        nodes = [{"node_id": i, "parents": [i - 1] if i > 1 else []} for i in range(1, 6)]
        self.assertIsNone(forest(nodes, 1, "x")["legal"])

    def test_shared_ancestors_stay_in_prefix(self):
        nodes = [{"node_id": i, "parents": ps} for i, ps in
                 {1: [], 2: [1], 3: [2], 4: [2], 5: [3, 4]}.items()]
        self.assertEqual(forest(nodes, 1, "x")["fixed_prefix"], [1, 2])

    def test_anchor_uses_ancestors_not_full_linear_prefix(self):
        for seed in range(20):
            a = e2_anchor(self.steps, "fixture", seed)
            self.assertNotIn(a["target_id"], a["prefix_ids"])
            self.assertIn(a["deleted_id"], a["target_parents"])
            self.assertEqual(a, e2_anchor(self.steps, "fixture", seed))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            topological([{"node_id": 1, "parents": []}] * 2)

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            topological([{"node_id": 1, "parents": [2]}, {"node_id": 2, "parents": [1]}])

    def test_unreviewed_rejected(self):
        r = fixture(); r["model_accepted"] = False
        with self.assertRaises(ValueError):
            normalize(r)

    def test_non_gpqa_rejected(self):
        r = fixture(); r["source"]["subset"] = "gsm8k"
        with self.assertRaises(ValueError):
            normalize(r)

    def test_original_order_not_silently_fixed(self):
        r = fixture(); r["dag"]["nodes"][0], r["dag"]["nodes"][1] = r["dag"]["nodes"][1], r["dag"]["nodes"][0]
        with self.assertRaises(ValueError):
            normalize(r)


class NumericalTests(unittest.TestCase):
    def test_pair_identity(self):
        r = pair([-1., -2.], [-2., -2.])
        self.assertEqual(r["g"], .5)
        self.assertAlmostEqual(r["g"], r["deleted_nll"] - r["full_nll"])

    def test_negative_part_before_mean(self):
        rows = [{"g": -1., "v": 1., "full_nll": 2.}, {"g": 1., "v": 0., "full_nll": 2.}]
        self.assertEqual(trajectory(rows)["M"], .5)
        self.assertEqual(trajectory(rows)["G"], 0.)

    def test_missing_is_not_zero(self):
        self.assertIsNone(trajectory([])["M"])
        self.assertIsNone(repeats([], 8)["D"])

    def test_sample_standard_deviation(self):
        rows = [{"status": "ok", "score": {"g": x, "v": 0., "full_nll": 1., "deleted_nll": 1.+x}}
                for x in [0., 2.]]
        self.assertAlmostEqual(repeats(rows, 2)["D"], math.sqrt(2))

    def test_invalid_logprobs(self):
        for x in (math.nan, math.inf, .1):
            with self.assertRaises(ValueError):
                pair([x], [-1.])

    def test_causal_shift(self):
        s = target_positions(3, 2)
        self.assertEqual(list(range(8))[s], [2, 3])

    def test_fair_comparison_common_three_way_targets(self):
        rows = []
        for label, targets in {"forest_baseline": {2: 1, 3: 2}, "legal": {1: 100, 3: 3}, "forest_break": {2: 100, 3: 5}}.items():
            for tid, value in targets.items():
                rows.append({"job": {"item_id": "q", "variant": label, "target_id": tid},
                             "score": {"g": -value, "v": value, "full_nll": value}})
        result = e1_analysis(rows)["paired_questions"]["fair_break_minus_legal"][0]
        self.assertEqual(result["common_ids"], [3])
        self.assertEqual(result["delta_M"], 2)


class BoundaryTests(unittest.TestCase):
    def test_spill_not_target(self):
        p = parse_step("hello</step>TAIL")
        self.assertEqual(p["body"], "hello")
        self.assertEqual(p["spill"], "TAIL")

    def test_invalid_structures(self):
        for text in ("", "x", "</step>", "<step>x</step>", "RESULT: 4</step>", "<think>x</think></step>"):
            self.assertFalse(parse_step(text)["valid"])

    def test_independent_rows_and_multitoken_boundary(self):
        class Tokenizer:
            def decode(self, tokens, **kwargs):
                return "".join(tokens)
        b = BoundaryTracker(Tokenizer(), 1, 2)
        self.assertEqual(b.update([["p", "x</", "step>"], ["p", "a", "b"]]), [True, False])
        self.assertEqual(b.update([["p", "x</", "step>", "ignored"], ["p", "a", "b", "</step>"]]), [True, True])
        self.assertEqual(b.lengths, [2, 3])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.jsonl"
        self.source.write_text(json.dumps(fixture()) + "\n", encoding="utf-8")
        self.prepared = self.root / "prepared"
        prepare(self.source, self.prepared, parent_probe=True)
        self.config = Path(__file__).resolve().parents[1] / "configs/mock.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_has_no_target_leak_in_e2_job(self):
        jobs = read(self.prepared / "jobs.json")
        self.assertTrue(any(j["variant"] == "legal" for j in jobs if j["kind"] == "e1"))
        for job in jobs:
            if job["kind"] == "e2":
                self.assertNotIn("target", job)
                self.assertNotIn("target_id", job)

    def test_duplicate_question_rejected(self):
        self.source.write_text((json.dumps(fixture()) + "\n") * 2)
        with self.assertRaises(ValueError):
            prepare(self.source, self.root / "bad")

    def test_outputs_never_overwritten(self):
        path = self.root / "one.json"
        save(path, {"a": 1})
        with self.assertRaises(FileExistsError):
            save(path, {"a": 2})
        self.assertEqual(read(path), {"a": 1})

    def test_unicode_line_separator(self):
        path = self.root / "unicode.jsonl"
        path.write_text('{"text":"a\u2028b"}\n', encoding="utf-8")
        self.assertEqual(len(read_jsonl(path)), 1)

    def test_both_experiments_end_to_end_and_resume(self):
        for mode in ("e1", "e2"):
            run = self.root / mode
            init_run(self.prepared, self.config, run, mode, 2)
            with self.assertRaises(ValueError):
                analyze(run, self.root / (mode + "premature"))
            worker(run, 0)
            worker(run, 1)
            self.assertEqual(worker(run, 0)["status"], "already_complete")
            result = analyze(run, self.root / (mode + "analysis"))
            self.assertFalse(result["scientific_evidence"])
            if mode == "e2":
                self.assertEqual(result["complete_questions"], 1)
            else:
                self.assertEqual(len(result["paired_questions"]["fair_break_minus_legal"]), 1)

    def test_input_tampering_detected(self):
        run = self.root / "tamper"
        init_run(self.prepared, self.config, run, "e2", 1)
        (run / "jobs.json").write_text("[]")
        with self.assertRaises(ValueError):
            worker(run, 0)

    def test_wrong_source_digest(self):
        with self.assertRaises(ValueError):
            prepare(self.source, self.root / "wrong", expected_sha="bad")


if __name__ == "__main__":
    unittest.main()
