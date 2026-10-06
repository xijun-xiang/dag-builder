"""Local CPU acceptance with immutable old E3 evidence; never runs a model."""
import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import sys

from pals_validation.e3 import greedy_data
from pals_validation.e3.decoupled import parse_decoupled
from pals_validation.e3.greedy_parse import parse
from pals_validation.e3.protocol import score_text_pair
from pals_validation.metrics import pair
from pals_validation.io import digest, read, save, sha256


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--old-evidence", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(repo / "tests"), "-q"],
                            capture_output=True, text=True, cwd=repo, env={**os.environ, "PYTHONPATH": str(repo / "src")})
    (args.output / "tests.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("CPU unit tests failed; inspect tests.log")
    meta, problems = greedy_data.validate(args.prepared)
    inventory = read(args.old_evidence / "inventory.json")
    evidence = args.old_evidence / "evidence"
    for name, entry in inventory["files"].items():
        if sha256(evidence / name) != entry["sha256"]:
            raise ValueError("old evidence changed: " + name)
    counts, methods, reasons = Counter(), Counter(), Counter()
    max_error, steps = 0., 0
    for slot in ("qwen25", "phi4mini", "qwen3"):
        by_id = {p["problem_id"]: p for p in read(evidence / "sources" / slot / "inputs/problems.json")}
        for path in sorted((evidence / slot / "generation_batches").glob("*.json")):
            for row in read(path)["rows"]:
                old = parse_decoupled(row["raw_text"], row["finish_reason"], by_id[row["problem_id"]]["benchmark"])
                new = parse(row["raw_text"], row["finish_reason"], by_id[row["problem_id"]]["benchmark"])
                counts["outputs"] += 1
                counts["old_process_valid"] += old["process_valid"]
                counts["new_process_valid"] += new["process_valid"]
                counts["new_scoreable"] += new["process_valid"] and len(new["steps"]) >= 2
                methods.update(new["segmentation_methods"])
                if not new["process_valid"]:
                    reasons.update([new["reason"]])
                if old["process_valid"]:
                    if old["steps"] != new["steps"]:
                        raise ValueError("old accepted segmentation changed")
                    saved = read(evidence / slot / "scores" / (digest(row["problem_id"]) + ".json"))
                    for entry in saved["steps"]:
                        full, deleted, target = score_text_pair(row["prompt"], new, entry["index"])
                        ev = entry["evidence"]
                        if (full, deleted, target) != (entry["full_context_text"], entry["deleted_context_text"], ev["target_text"]):
                            raise ValueError("existing score text changed")
                        computed = pair(ev["full_logprobs"], ev["deleted_logprobs"])
                        max_error = max(max_error, *(abs(computed[k] - v) for k, v in entry["score"].items()))
                        steps += 1
    if max_error > 1e-8:
        raise ValueError("old arithmetic differs")
    audit = {"status": "PASS", "test_log_sha256": sha256(args.output / "tests.log"),
        "prepared_manifest_sha256": sha256(args.prepared / "manifest.json"), "counts": meta["counts"],
        "mmlu_subjects": len(meta["subjects"]), "per_model": len(problems), "three_models": 3 * len(problems),
        "old_evidence_files": len(inventory["files"]), "old_trace_replay": dict(counts),
        "segmentation_methods": dict(methods), "unrecovered_reasons": dict(reasons),
        "old_scored_steps_replayed": steps, "max_arithmetic_error": max_error,
        "new_generation": 0, "note": "Old T=.7 replay is engineering evidence, not new greedy experiment results."}
    save(args.output / "audit.json", audit)
    print(audit, flush=True)


if __name__ == "__main__":
    main()
