"""Explicit eight-GPU canary -> formal E1/E2; no retries or effect-size gate."""
import argparse
import os
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from .analyze import analyze
from .backend import HFBackend
from .io import read, save, sha256
from .locking import exclusive_lock
from .run import code_hashes, init_or_resume, now


@contextmanager
def terminate_as_exception():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_children(children):
    for proc, stream in children:
        if proc.poll() is None:
            proc.terminate()
    for proc, stream in children:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if stream is not None:
            stream.close()


def workers(logs, run, experiment, visible):
    children = []
    try:
        for shard, gpu in enumerate(visible):
            stream = (logs / f"{experiment}-{shard}.log").open("x")
            command = [sys.executable, "-m", "pals_validation.cli", "worker",
                       "--run", str(run), "--shard", str(shard)]
            try:
                proc = subprocess.Popen(command, env={**os.environ, "CUDA_VISIBLE_DEVICES": gpu},
                                        stdout=stream, stderr=subprocess.STDOUT)
            except BaseException:
                stream.close()
                raise
            children.append((proc, stream))
        codes = [proc.wait() for proc, _ in children]
        if any(codes):
            raise RuntimeError(f"{experiment} worker exit codes: {codes}")
    finally:
        stop_children(children)


def reference_identity(root):
    return {"config": sha256(root / "config.json"),
            "prepared": sha256(root / "prepared/manifest.json"), "code": code_hashes()}


def reference_check(root):
    """Separate-process GPU check against native masked loss."""
    import torch
    config = read(root / "config.json")
    backend = HFBackend(config["model_path"], config)
    cases = {c["item_id"]: c for c in read(root / "prepared/cases.json")}
    job = next(j for j in read(root / "prepared/jobs.json") if j["kind"] == "e1")
    scored = backend.score(cases[job["item_id"]], job["prefix_ids"], job["deleted_id"], job["target"])
    ev, errors = scored["evidence"], {}
    for label in ("full", "deleted"):
        context, target = ev[label + "_context_ids"], ev["target_ids"]
        ids = torch.tensor([context + target], device=config["device"])
        labels = ids.clone()
        labels[:, :len(context)] = -100
        with torch.inference_mode():
            loss = backend.model(input_ids=ids, labels=labels, use_cache=False).loss.item()
        errors[label] = abs(loss - scored["score"][label + "_nll"])
    if not all(0 <= e <= .005 for e in errors.values()):
        raise RuntimeError(f"Native masked-loss reference failed: {errors}")
    budgets = None
    if config.get("campaign_experiment") == "e2":
        prepared = root.parent.parent / "prepared"
        formal_cases = {c["item_id"]: c for c in read(prepared / "cases.json")}
        budgets = []
        for j in read(prepared / "jobs.json"):
            if j["kind"] != "e2":
                continue
            context = backend.context(formal_cases[j["item_id"]], j["prefix_ids"], for_generation=True)
            prompt_tokens = len(backend.tokenizer.encode(context, add_special_tokens=False))
            required = prompt_tokens + config["max_new_tokens"]
            if required > config["max_context"]:
                raise ValueError("Full-cohort E2 context budget overflow: " + j["item_id"])
            budgets.append({"item_id": j["item_id"], "prompt_tokens": prompt_tokens,
                            "reserved_new_tokens": config["max_new_tokens"],
                            "context_headroom": config["max_context"] - required})
        if not budgets:
            raise ValueError("Full-cohort E2 budget inventory is empty")
    save(root / "reference.json", {"absolute_nll_errors": errors, "tolerance": .005,
         "full_cohort_generation_budgets": budgets,
         "versions": backend.versions, "identity": reference_identity(root)})


def attempt(root, identity, resume):
    state = root / "campaign.json"
    if state.exists():
        if not resume:
            raise FileExistsError("Existing campaign requires explicit --resume")
        if read(state)["identity"] != identity:
            raise ValueError("Campaign inputs/source/runtime declaration changed")
    else:
        save(state, {"identity": identity, "created": now()})
    folder = root / "attempts" / (now().replace(":", "") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700)
    save(folder / "started.json", {"started": now(), "resume": resume,
         "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "command": sys.argv})
    return folder


def phases(root, prepared, visible, logs, prefix="", experiments=("e1", "e2")):
    summaries = {}
    for experiment in experiments:
        run = root / (prefix + experiment)
        init_or_resume(prepared, root / "config.json", run, experiment, len(visible))
        workers(logs, run, experiment, visible)
        summaries[experiment] = analyze(run, logs / (experiment + "-analysis"))
    return summaries


def selected_experiments(config, experiment):
    if experiment not in ("all", "e1", "e2"):
        raise ValueError("Unknown campaign experiment")
    if config.get("campaign_experiment", "all") != experiment:
        raise ValueError("Campaign experiment differs from frozen configuration")
    return ("e1", "e2") if experiment == "all" else (experiment,)


def canary_coverage(e2, config):
    """Numerical/record integrity is checked separately; invalid draws stay invalid."""
    policy = config.get("canary_coverage_policy", "complete")
    minimum = config.get("canary_min_valid_repeats", 2)
    cells = e2["cells"]
    if not cells or any(c["attempted"] != config["repeats"] or c["planned"] != config["repeats"] for c in cells):
        raise RuntimeError("Canary repeat slots incomplete")
    report = {"policy": policy, "planned": sum(c["planned"] for c in cells),
              "valid": sum(c["valid"] for c in cells), "complete_questions": e2["complete_questions"],
              "planned_questions": e2["planned_questions"], "minimum_valid_repeats_per_cell": minimum}
    if policy == "complete":
        if e2["complete_questions"] != e2["planned_questions"]:
            raise RuntimeError("Canary generation coverage incomplete; no extra sampling")
    elif policy == "report_invalid":
        if any(c["valid"] < minimum for c in cells):
            raise RuntimeError("Canary has too few valid draws to check repeat scoring; no extra sampling")
    else:
        raise ValueError("Unknown canary coverage policy")
    report["invalid"] = report["planned"] - report["valid"]
    report["coverage_warning"] = report["invalid"] / report["planned"] >= .05
    return report


def run_canary(root, visible, resume=False, experiment="all"):
    config = read(root / "config.json")
    experiments = selected_experiments(config, experiment)
    with exclusive_lock(root / "CAMPAIGN.lock"):
        logs = attempt(root, reference_identity(root), resume)
        try:
            path = root / "reference.json"
            if path.exists():
                reference = read(path)
                if reference["identity"] != reference_identity(root):
                    raise ValueError("Reference identity mismatch")
                if not all(0 <= e <= .005 for e in reference["absolute_nll_errors"].values()):
                    raise ValueError("Invalid stored reference")
            else:
                proc = subprocess.Popen([sys.executable, "-m", "pals_validation.campaign",
                                         "--root", str(root), "--reference"])
                try:
                    if proc.wait():
                        raise RuntimeError("Native masked-loss reference failed")
                finally:
                    stop_children([(proc, None)])
            summaries = phases(root, root / "prepared", visible, logs, experiments=experiments)
            coverage = canary_coverage(summaries["e2"], config) if "e2" in summaries else None
            result = {"status": "PASS", "experiments": list(experiments),
                      "e1_jobs": summaries.get("e1", {}).get("accepted_jobs"),
                      "e2_jobs": summaries.get("e2", {}).get("accepted_jobs"),
                      "e2_complete_questions": summaries.get("e2", {}).get("complete_questions"),
                      "generation_coverage": coverage,
                      "identity": reference_identity(root), "purpose": "engineering only, no effect gate"}
            if (root / "canary-completion.json").exists():
                if read(root / "canary-completion.json") != result:
                    raise ValueError("Canary completion changed")
            else:
                save(root / "canary-completion.json", result)
            save(logs / "completion.json", result)
            return result
        except BaseException as error:
            save(logs / "failure.json", {"type": type(error).__name__, "message": str(error), "ended": now()})
            raise


def lock_probe(root):
    """Check lock exclusion/release on the actual output filesystem."""
    path = root / "filesystem-probe.lock"
    code = ("import sys; from pathlib import Path; from pals_validation.locking import exclusive_lock; "
            "\nwith exclusive_lock(Path(sys.argv[1])): pass")
    with exclusive_lock(path):
        blocked = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
        if blocked.returncode == 0 or "Active owner holds lock" not in blocked.stderr:
            raise RuntimeError("Output filesystem flock exclusion could not be verified")
    subprocess.run([sys.executable, "-c", code, str(path)], check=True)


def run_campaign(root, visible, resume=False, experiment="all"):
    experiments = selected_experiments(read(root / "config.json"), experiment)
    identity = {"config": sha256(root / "config.json"), "code": code_hashes(),
                "prepared": sha256(root.parent / "prepared/manifest.json"),
                "canary": reference_identity(root / "canary")}
    with exclusive_lock(root / "CAMPAIGN.lock"):
        logs = attempt(root, identity, resume)
        try:
            lock_probe(logs)
            with (logs / "pip-freeze.txt").open("x") as stream:
                subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout=stream, check=True)
            run_canary(root / "canary", visible, resume=resume, experiment=experiment)
            summaries = phases(root, root.parent / "prepared", visible, logs, "formal-", experiments=experiments)
            result = {"status": "artifact_checks_passed_scheduler_pending", "ended": now(),
                      "analysis": str(logs), "results": {k: {"accepted_jobs": v["accepted_jobs"],
                      "protocol_id": v["protocol_id"], "complete_questions": v.get("complete_questions"),
                      "planned_questions": v.get("planned_questions")} for k, v in summaries.items()},
                      "effect_direction_used_for_gate": False}
            save(logs / "completion.json", result)
            if not (root / "completion.json").exists():
                save(root / "completion.json", result)
            return result
        except BaseException as error:
            save(logs / "failure.json", {"type": type(error).__name__, "message": str(error), "ended": now()})
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--canary-only", action="store_true")
    parser.add_argument("--reference", action="store_true")
    parser.add_argument("--experiment", choices=("all", "e1", "e2"), default="all")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve(strict=True)
    fence = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    if fence not in root.parents or not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("PALS output path and Slurm allocation required")
    with terminate_as_exception():
        if args.reference:
            reference_check(root)
        else:
            visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
            if len(visible) != 8 or len(set(visible)) != 8 or any(not x for x in visible):
                raise RuntimeError("Exactly eight distinct allocated GPUs required")
            (run_canary if args.canary_only else run_campaign)(root, visible, args.resume, args.experiment)


if __name__ == "__main__":
    main()
