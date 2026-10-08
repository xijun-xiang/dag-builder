"""Eight persistent workers; startup checks are inside the full allocation."""
import os
from pathlib import Path
import subprocess
import sys
import time

from pals_validation.e3.greedy import coverage, load, verify_reference
from pals_validation.e3.run import _model_files
from pals_validation.e3.schema import require
from pals_validation.io import read, save, digest


def main():
    root = Path(os.environ["PALS_EXPERIMENT"])
    slot, stage, job = os.environ["PALS_SLOT"], os.environ["PALS_STAGE"], os.environ["SLURM_JOB_ID"]
    require(stage in ("gpu", "evaluate"), "unknown stage")
    manifest, config, _, batches = load(root, slot)
    continued = "full_continuation" in manifest
    devices = [s for s in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if s]
    require((len(devices) == 8 if stage == "gpu" else not devices), "wrong allocation")
    if stage == "gpu":
        require(_model_files(config) == read(root / slot / "model-files.json"), "model changed after CPU preflight")
    # End before the Slurm limit; atomic batches are retained. No automatic restart.
    deadline = float(os.environ["PALS_DEADLINE"])
    workers, handles, released = [], [], stage != "gpu"
    startup_reported = stage != "gpu"
    try:
        for shard in range(8):
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = devices[shard] if devices else ""
            path = root / slot / "logs" / f"{job}-{stage}-{shard}.log"
            stream = path.open("x")
            handles.append(stream)
            args = [sys.executable, "-m", "pals_validation.e3.greedy_cli", stage + "-worker",
                    "--root", str(root), "--slot", slot, "--shard", str(shard), "--deadline", str(deadline)]
            workers.append(subprocess.Popen(args, env=env, stdout=stream, stderr=subprocess.STDOUT))
        while True:
            codes = [proc.poll() for proc in workers]
            require(not any(c is not None and c != 0 for c in codes), "worker failure/checkpoint: " + str(codes))
            if continued and not released:
                files = [root / slot / "workers" / f"reference-{job}-{i}.json" for i in range(8)]
                if all(p.exists() for p in files):
                    for path in files:
                        verify_reference(read(path), manifest["protocol_id"], config["backend"] == "hf")
                    save(root / slot / "startup" / f"{job}-numeric-go.json",
                         {"protocol_id": manifest["protocol_id"], "status": "PASS"})
                    released = True
            if continued and released and not startup_reported:
                files = [root / slot / "dispatch" / f"batch-{i:06d}.json" for i in range(manifest["initial_batches"])]
                if all(p.exists() for p in files):
                    rows = [r for path in files for r in read(path)["rows"]]
                    expected = [i for b in batches[:manifest["initial_batches"]] for i in b["problem_ids"]]
                    require(sorted(r["problem_id"] for r in rows) == sorted(expected), "startup question set")
                    save(root / slot / "startup" / f"{job}-coverage.json",
                         {**coverage(rows, manifest["coverage_review"]), "policy": "report_invalid"})
                    startup_reported = True
            if not continued and not released:
                files = [root / slot / "startup" / f"{job}-{i}.json" for i in range(8)]
                if all(p.exists() for p in files):
                    receipts = [read(p) for p in files]
                    require(all(r["protocol_id"] == manifest["protocol_id"] for r in receipts), "startup identity")
                    rows = [row for r in receipts for row in r["rows"]]
                    expected = [i for b in batches[:manifest["initial_batches"]] for i in b["problem_ids"]]
                    require(sorted(r["problem_id"] for r in rows) == sorted(expected), "startup question set")
                    check = coverage(rows, manifest["coverage_review"])
                    save(root / slot / "startup" / f"{job}-coverage.json", check)
                    require(check["coverage_gate"], "SYSTEMATIC_FORMAT_FAILURE; raw retained, no resampling")
                    save(root / slot / "startup" / f"{job}-go.json", {"protocol_id": manifest["protocol_id"]})
                    released = True
            if all(c is not None for c in codes):
                break
            require(time.time() < deadline + 3300, "allocation deadline")
            time.sleep(1)
        require(released and (not continued or startup_reported), "startup evidence missing")
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.terminate()
        for proc in workers:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for handle in handles:
            handle.close()
    if stage == "evaluate":
        subprocess.run([sys.executable, "-m", "pals_validation.e3.greedy_cli", "audit", "--root", str(root), "--slot", slot], check=True)


if __name__ == "__main__":
    main()
