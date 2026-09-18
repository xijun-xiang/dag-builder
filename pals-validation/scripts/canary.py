"""Bounded real-model acceptance; execute only inside a Slurm allocation."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from pals_validation.analyze import analyze
from pals_validation.backend import HFBackend
from pals_validation.io import read, save
from pals_validation.run import init_run


def reference_check(root):
    import torch
    config = read(root / "config.json")
    backend = HFBackend(config["model_path"], config)
    case = read(root / "prepared/cases.json")[0]
    job = next(j for j in read(root / "prepared/jobs.json") if j["kind"] == "e1")
    scored = backend.score(case, job["prefix_ids"], job["deleted_id"], job["target"])
    ev = scored["evidence"]
    errors = {}
    for label in ("full", "deleted"):
        context, target = ev[label + "_context_ids"], ev["target_ids"]
        ids = torch.tensor([context + target], device=config["device"])
        labels = ids.clone()
        labels[:, :len(context)] = -100
        with torch.inference_mode():
            loss = backend.model(input_ids=ids, labels=labels, use_cache=False).loss.item()
        errors[label] = abs(loss - scored["score"][label + "_nll"])
    if max(errors.values()) > 0.005:
        raise RuntimeError(f"Independent masked-loss reference failed: {errors}")
    save(root / "reference.json", {"absolute_nll_errors": errors, "tolerance": 0.005,
                                  "versions": backend.versions})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--reference", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Slurm required")
    if args.reference:
        reference_check(root)
        return
    subprocess.run([sys.executable, __file__, "--root", str(root), "--reference"], check=True)
    summaries = {}
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "0,1,2,3,4,5,6,7").split(",")
    if len(visible) != 8:
        raise RuntimeError("This canary requires exactly 8 visible allocated GPUs")
    for experiment in ("e1", "e2"):
        run = root / experiment
        init_run(root / "prepared", root / "config.json", run, experiment, 8)
        workers = []
        for shard, device in enumerate(visible):
            env = {**os.environ, "CUDA_VISIBLE_DEVICES": device}
            stream = (root / "logs" / f"{experiment}-{shard}.log").open("x")
            proc = subprocess.Popen([sys.executable, "-m", "pals_validation.cli", "worker",
                                     "--run", str(run), "--shard", str(shard)],
                                    env=env, stdout=stream, stderr=subprocess.STDOUT)
            workers.append((proc, stream))
        codes = []
        for proc, stream in workers:
            codes.append(proc.wait())
            stream.close()
        if any(codes):
            raise RuntimeError(f"{experiment} workers failed: {codes}")
        summaries[experiment] = analyze(run, root / (experiment + "-analysis"))
        # Completed worker must be a no-op, not an extra generation attempt.
        subprocess.run([sys.executable, "-m", "pals_validation.cli", "worker",
                        "--run", str(run), "--shard", "0"], check=True)
    e2 = summaries["e2"]
    if e2["complete_questions"] != e2["planned_questions"]:
        raise RuntimeError("Canary generation coverage is incomplete; do not publish as passed")
    save(root / "canary-completion.json", {"status": "PASS", "e1_jobs": summaries["e1"]["accepted_jobs"],
         "e2_jobs": e2["accepted_jobs"], "e2_complete_questions": e2["complete_questions"],
         "purpose": "engineering acceptance, not validation effect evidence"})


if __name__ == "__main__":
    main()
