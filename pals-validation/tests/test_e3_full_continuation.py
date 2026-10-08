"""CPU-only recovery, full denominators, scheduling and fail-closed regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.e3 import greedy, full_continuation as full
from pals_validation.e3.backend import E3MockBackend
from pals_validation.e3.greedy_report import group_summary
from pals_validation.io import read, save, sha256, digest
from test_e3_extension import inputs


def setup(base, slot="llama3", invalid=False):
    prepared, configs = inputs(base)
    source = (base / "old").resolve()
    greedy.init(prepared, {slot: configs[slot]}, {}, source, deployment_slot=slot)
    m, c, p, batches = greedy.load(source, slot)
    backend = E3MockBackend("", c)
    if invalid:
        original = backend.generate_batch
        def bad(problems, seed):
            result = original(problems, seed)
            for row in result["rows"]:
                row["raw_text"] = "no explicit step or answer"
                row["generated_token_ids"] = list(row["raw_text"].encode())
                row["stop_token_length"] = len(row["generated_token_ids"])
                row["finish_reason"] = "eos"
            return result
        backend.generate_batch = bad
    greedy.process_batch(source, slot, batches[0], backend, m, p)
    # A whole attempted batch without raw is explicitly accounted for, never retried.
    save(source / slot / "attempts" / (batches[1]["batch_id"] + ".json"),
         {"protocol_id": m["protocol_id"], "batch": batches[1], "job_id": "old"})
    return prepared, configs, source, batches


def continuation(base, prepared, configs, source, slot="llama3"):
    planned = full.plan(source)
    run = base / "new"
    greedy.init(prepared, {slot: configs[slot]}, {}, run, deployment_slot=slot, continuation_plan=planned)
    full.import_source(source, run, planned)
    return run


class FullContinuationTests(unittest.TestCase):
    def test_complete_denominator_no_retry_and_imported_scores_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            before = {str(p.relative_to(source)): sha256(p) for p in source.rglob("*.json")}
            run = continuation(base, prepared, configs, source)
            generated = []
            original = E3MockBackend.generate_batch
            def record(backend, problems, seed):
                generated.extend(p["problem_id"] for p in problems)
                return original(backend, problems, seed)
            with patch.object(E3MockBackend, "generate_batch", record):
                for shard in range(8):
                    greedy.gpu_worker(run, "llama3", shard, barrier=False)
                    greedy.evaluate(run, "llama3", shard)
            greedy.audit(run, "llama3")
            audit = read(run / "llama3/audit.json")
            self.assertEqual(len(audit["items"]), 5)
            self.assertEqual(audit["infrastructure_na"], 1)
            self.assertEqual(audit["coverage_policy"], "report_invalid")
            self.assertFalse(audit["coverage_review"]["coverage_gate"])
            self.assertEqual(set(generated), {i for b in batches[2:] for i in b["problem_ids"]})
            missing = [r for r in audit["items"] if r["process_reason"] == "infrastructure_interrupted_na"]
            self.assertIsNone(missing[0]["summary"])
            self.assertIsNone(missing[0]["correct"])
            self.assertEqual(group_summary(audit["items"])["total"], 5)
            after = {str(p.relative_to(source)): sha256(p) for p in source.rglob("*.json")}
            self.assertEqual(before, after)
            self.assertEqual(read(run / "recovery/ready.json")["stats"]["reused_scores"], 1)

    def test_zero_coverage_does_not_stop_authorized_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base, "internlm3", invalid=True)
            run = continuation(base, prepared, configs, source, "internlm3")
            # All remaining outputs are a single valid step: no g can be defined.
            original = E3MockBackend.generate_batch
            def single(backend, problems, seed):
                result = original(backend, problems, seed)
                for row in result["rows"]:
                    row["raw_text"] = "<step>one</step><answer>A</answer>"
                    row["generated_token_ids"] = list(row["raw_text"].encode())
                    row["stop_token_length"] = len(row["generated_token_ids"])
                return result
            with patch.object(E3MockBackend, "generate_batch", single):
                for shard in range(8):
                    greedy.gpu_worker(run, "internlm3", shard, barrier=False)
            greedy.audit(run, "internlm3", include_outcomes=False)
            rows = read(run / "internlm3/gpu-audit.json")["items"]
            self.assertEqual(group_summary(rows)["scoreable"], 0)

    def test_import_tampering_fails_not_silently_downgraded_to_na(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            planned = full.plan(source)
            run = base / "new"
            greedy.init(prepared, {"llama3": configs["llama3"]}, {}, run,
                        deployment_slot="llama3", continuation_plan=planned)
            p = source / "llama3/raw" / (batches[0]["batch_id"] + ".json")
            p.write_text(p.read_text() + " ")
            with self.assertRaises(ValueError):
                full.import_source(source, run, planned)
            self.assertFalse((run / "recovery/ready.json").exists())

    def test_missing_records_cannot_be_replaced_by_new_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            run = continuation(base, prepared, configs, source)
            manifest, cfg, p, _ = greedy.load(run, "llama3")
            save(run / "llama3/raw" / (batches[1]["batch_id"] + ".json"), {})
            with self.assertRaisesRegex(ValueError, "regenerated"):
                greedy.process_batch(run, "llama3", batches[1], E3MockBackend("", cfg), manifest, p)

    def test_prompt_or_budget_change_rejected_during_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            planned = full.plan(source)
            run = base / "new"
            greedy.init(prepared, {"llama3": configs["llama3"]}, {}, run,
                        deployment_slot="llama3", continuation_plan=planned)
            (run / "llama3/config.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "input/config/model changed"):
                full.import_source(source, run, planned)

    def test_current_uncertain_generation_is_still_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            run = continuation(base, prepared, configs, source)
            manifest, cfg, problems, _ = greedy.load(run, "llama3")
            b = batches[2]
            save(run / "llama3/attempts" / (b["batch_id"] + ".json"), {"protocol_id": manifest["protocol_id"], "batch": b})
            with self.assertRaisesRegex(ValueError, "UNCERTAIN_GENERATION"):
                greedy.process_batch(run, "llama3", b, E3MockBackend("", cfg), manifest, problems)

    def test_shared_queue_assigns_each_fixed_batch_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "dispatch").mkdir()
            batches = [{"batch_id": str(i)} for i in range(100)]
            def consume(shard):
                result = []
                while (claimed := full.claim_batch(folder, batches, "p", shard, "123")) is not None:
                    result.append(claimed[0])
                return result
            with ThreadPoolExecutor(max_workers=8) as pool:
                outputs = list(pool.map(consume, range(8)))
            self.assertEqual(sorted(i for rows in outputs for i in rows), list(range(100)))
            with self.assertRaisesRegex(ValueError, "ownership"):
                full.claim_batch(folder, batches, "p", 0, "another-job")

    def test_numeric_gate_cannot_be_skipped_in_real_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, batches = setup(base)
            run = continuation(base, prepared, configs, source)
            with self.assertRaisesRegex(ValueError, "numerical gate deadline"):
                greedy.gpu_worker(run, "llama3", 0, deadline=0, barrier=True)
            self.assertFalse((run / "llama3/dispatch/claims.jsonl").exists())

    def test_reused_score_mutation_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            prepared, configs, source, _ = setup(base)
            run = continuation(base, prepared, configs, source)
            m, _, _, _ = greedy.load(run, "llama3")
            p = next((run / "llama3/scores").glob("*.json"))
            value = read(p)
            value["summary"]["G"] += 1
            import json
            p.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "reused evidence changed"):
                full.verify_imports(run, "llama3", m)


if __name__ == "__main__":
    unittest.main()
