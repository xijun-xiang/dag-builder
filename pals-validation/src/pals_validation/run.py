"""Immutable runs, independently resumable Slurm shards, explicit completion."""
import os
import platform
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from . import __version__
from .backend import HFBackend, MockBackend
from .io import digest, read, save, sha256, verify
from .metrics import repeats
from .locking import exclusive_lock
from .model_policy import local_code_policy


def now():
    return datetime.now(timezone.utc).isoformat()


def code_hashes():
    return {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def validate_config(config):
    required = {"model_path", "model_revision", "temperatures", "repeats", "seed", "device", "dtype",
                "attention", "max_context", "max_new_tokens", "chat_template_kwargs", "backend"}
    if required - set(config):
        raise ValueError(f"Missing configuration keys: {sorted(required - set(config))}")
    if config["backend"] not in ("hf", "mock") or config["dtype"] not in ("bfloat16", "float16", "float32"):
        raise ValueError("Unsupported backend/dtype")
    for key in ("repeats", "max_context", "max_new_tokens", "seed"):
        if type(config[key]) is not int or config[key] < (2 if key == "repeats" else 1):
            raise ValueError(f"Invalid {key}")
    ts = config["temperatures"]
    if not ts or len(set(ts)) != len(ts) or any(type(t) not in (int, float) or not 0 < t <= 2 for t in ts):
        raise ValueError("Invalid or duplicate temperatures")
    if not isinstance(config["chat_template_kwargs"], dict):
        raise ValueError("Explicit chat_template_kwargs required")
    if config["backend"] == "hf" and (not config["model_revision"] or "REPLACE" in config["model_revision"]):
        raise ValueError("Pin the actual model revision before initialization")
    if config.get("generation_prompt_version", "v1") not in ("v1", "humaneval-single-step-v2"):
        raise ValueError("Unknown generation prompt version")
    if config.get("campaign_experiment", "all") not in ("e1", "e2", "all"):
        raise ValueError("Invalid campaign experiment")
    if config.get("canary_coverage_policy", "complete") not in ("complete", "report_invalid"):
        raise ValueError("Unknown canary coverage policy")
    minimum = config.get("canary_min_valid_repeats", 2)
    if type(minimum) is not int or not 2 <= minimum <= config["repeats"]:
        raise ValueError("Canary minimum must allow a sample standard deviation")


def init_run(prepared, config_path, output, experiment, shards):
    """Publish initialization atomically; interrupted staging is never a run."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(output.parent / (output.name + ".init.lock")):
        if output.exists():
            raise FileExistsError(output)
        stage = Path(tempfile.mkdtemp(prefix="." + output.name + "-init-", dir=output.parent))
        result = _init_run(prepared, config_path, stage / "run", experiment, shards)
        os.rename(stage / "run", output)
        stage.rmdir()  # only our now-empty initialization directory
        return {**result, "run": str(output)}


def init_or_resume(prepared, config_path, output, experiment, shards):
    """Resume only exactly matching frozen inputs, code, config and shard count."""
    output, prepared = Path(output), Path(prepared)
    if not output.exists():
        return init_run(prepared, config_path, output, experiment, shards)
    protocol, frozen = validate_run(output)
    requested = read(config_path)
    if requested["backend"] == "hf":
        requested["model_path"] = str(Path(requested["model_path"]).resolve(strict=True))
    if frozen != requested or protocol["experiment"] != experiment or protocol["shards"] != shards:
        raise ValueError("Resume configuration/experiment/shards mismatch")
    manifest = read(prepared / "manifest.json")
    verify(prepared, manifest["files"])
    if read(output / "inputs/manifest.json") != manifest:
        raise ValueError("Resume prepared input mismatch")
    return {"status": "resumed", "run": str(output)}


def _init_run(prepared, config_path, output, experiment, shards):
    prepared, output = Path(prepared), Path(output)
    config = read(config_path)
    validate_config(config)
    if shards < 1 or experiment not in ("e1", "e2"):
        raise ValueError("Invalid experiment/shard count")
    manifest = read(prepared / "manifest.json")
    verify(prepared, manifest["files"])
    if (manifest.get("frozen_source_experiment") is not None
            and manifest["frozen_source_experiment"] != experiment):
        raise ValueError("Frozen cohort source experiment does not match requested run")
    compatible = manifest.get("compatible_experiments")
    if compatible is not None and (
            not isinstance(compatible, list)
            or compatible != ["e1", "e2"]
            or experiment not in compatible):
        raise ValueError("Frozen cohort experiment compatibility mismatch")
    jobs = []
    for base in read(prepared / "jobs.json"):
        if base["kind"] != experiment:
            continue
        for temperature in config["temperatures"] if experiment == "e2" else [None]:
            job = {**base, "temperature": temperature}
            job["job_id"] = digest(job)
            jobs.append(job)
    if not jobs:
        raise ValueError("No applicable jobs")
    model_files = {}
    if config["backend"] == "hf":
        model = Path(config["model_path"]).resolve(strict=True)
        if not model.is_dir():
            raise ValueError("model_path must be an existing local snapshot directory")
        if not (model / "config.json").is_file():
            raise ValueError("Missing model config")
        local_code_policy(model, config)
        for p in sorted(model.rglob("*")):
            if p.is_file() and ".cache" not in p.relative_to(model).parts and ".git" not in p.relative_to(model).parts:
                model_files[str(p.relative_to(model))] = sha256(p)
        if not any(n.endswith((".safetensors", ".bin")) for n in model_files):
            raise ValueError("No model weight files found")
        config["model_path"] = str(model)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ("inputs", "source", "results", "generations", "workers", "analysis"):
        (output / name).mkdir(mode=0o700)
    for p in sorted(Path(__file__).parent.glob("*.py")):
        shutil.copyfile(p, output / "source" / p.name)
    for name in ("cases.json", "selection.json", "inventory.json", "manifest.json"):
        save(output / "inputs" / name, read(prepared / name))
    save(output / "jobs.json", jobs)
    save(output / "config.json", config)
    files = {str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()}
    protocol = {"version": __version__, "experiment": experiment, "shards": shards,
                "jobs": len(jobs), "created": now(), "code_hashes": code_hashes(),
                "files": files, "model_files": model_files,
                "scientific_evidence": config["backend"] == "hf"}
    save(output / "protocol.json", {**protocol, "protocol_id": digest(protocol)})
    return {"jobs": len(jobs), "shards": shards, "run": str(output), "scientific_evidence": protocol["scientific_evidence"]}


def validate_run(run):
    protocol = read(run / "protocol.json")
    base = {k: v for k, v in protocol.items() if k != "protocol_id"}
    if digest(base) != protocol["protocol_id"]:
        raise ValueError("Protocol identity mismatch")
    verify(run, protocol["files"])
    if code_hashes() != protocol["code_hashes"]:
        raise ValueError("Installed source changed since initialization; create a new run")
    return protocol, read(run / "config.json")


def worker(run, shard):
    run = Path(run).resolve()
    protocol, config = validate_run(run)
    if not 0 <= shard < protocol["shards"]:
        raise ValueError("Invalid shard")
    if config["backend"] == "hf" and str(config["device"]).startswith("cuda"):
        if not os.environ.get("SLURM_JOB_ID"):
            raise ValueError("CUDA inference requires a Slurm allocation; never run on login node")
        approved = Path("/work/projects/polyullm/xxj/PALS").resolve()
        if approved not in run.parents:
            raise ValueError("B1 GPU outputs must be inside approved PALS root")
    folder = run / "workers" / str(shard)
    folder.mkdir(exist_ok=True, mode=0o700)
    with exclusive_lock(folder / "ACTIVE.lock"):
        return _locked_worker(run, shard, protocol, config, folder)


def _locked_worker(run, shard, protocol, config, folder):
    try:
        jobs = [j for i, j in enumerate(read(run / "jobs.json")) if i % protocol["shards"] == shard]
        if (folder / "completion.json").exists():
            done = read(folder / "completion.json")
            if done["status"] != "complete" or set(done["result_hashes"]) != {j["job_id"] for j in jobs}:
                raise ValueError("Completed shard identity mismatch")
            for job_id, expected in done["result_hashes"].items():
                if sha256(run / "results" / (job_id + ".json")) != expected:
                    raise ValueError("Completed result changed")
            return {"status": "already_complete", "shard": shard}
        for job in jobs:
            path = run / "results" / (job["job_id"] + ".json")
            if path.exists():
                saved = read(path)
                if saved["job"] != job or saved["protocol_id"] != protocol["protocol_id"]:
                    raise ValueError("Existing result identity mismatch")
        cases = {c["item_id"]: c for c in read(run / "inputs/cases.json")}
        pending = [j for j in jobs if not (run / "results" / (j["job_id"] + ".json")).exists()]
        backend = None
        if pending:
            if config["backend"] == "hf":
                # Each worker verifies the frozen local model; no implicit downloads.
                for name, expected in protocol["model_files"].items():
                    if sha256(Path(config["model_path"]) / name) != expected:
                        raise ValueError("Model snapshot changed: " + name)
            backend = (HFBackend if config["backend"] == "hf" else MockBackend)(config["model_path"], config)
            first = pending[0]
            case = cases[first["item_id"]]
            probe = first.get("target", "A deterministic scoring check.")
            a = backend.score(case, first["prefix_ids"], first["deleted_id"], probe)
            b = backend.score(case, first["prefix_ids"], first["deleted_id"], probe)
            error = max(abs(x - y) for key in ("full_logprobs", "deleted_logprobs")
                        for x, y in zip(a["evidence"][key], b["evidence"][key]))
            if error > 1e-5:
                raise ValueError("Repeated scoring gate failed")
            save(folder / ("runtime-" + str(os.getpid()) + "-" + now().replace(":", "") + ".json"),
                 {"versions": backend.versions, "gate_max_error": error, "python": platform.python_version(),
                  "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "started": now()})
        for job in pending:
            case = cases[job["item_id"]]
            if job["kind"] == "e1":
                result = {**backend.score(case, job["prefix_ids"], job["deleted_id"], job["target"]), "status": "ok"}
            else:
                path = run / "generations" / (job["job_id"] + ".json")
                if not path.exists():
                    seed = int(digest([config["seed"], job["job_id"]])[:8], 16)
                    generation = backend.generate(case, job["prefix_ids"], job["temperature"], config["repeats"], seed)
                    save(path, {"job_id": job["job_id"], "protocol_id": protocol["protocol_id"], **generation})
                generation = read(path)
                if generation["job_id"] != job["job_id"] or generation["protocol_id"] != protocol["protocol_id"]:
                    raise ValueError("Generation checkpoint mismatch")
                rows = []
                for generated in generation["rows"]:
                    valid = generated["parse"]["valid"] and generated["finish_reason"] == "boundary"
                    row = {**generated, "status": "ok" if valid else "invalid_generation"}
                    if valid:
                        row.update(backend.score(case, job["prefix_ids"], job["deleted_id"], generated["parse"]["body"]))
                    rows.append(row)
                result = {"status": "ok", "rows": rows, "summary": repeats(rows, config["repeats"]),
                          "generation_sha256": sha256(path)}
            save(run / "results" / (job["job_id"] + ".json"),
                 {"job": job, "protocol_id": protocol["protocol_id"], "scientific_evidence": protocol["scientific_evidence"], **result})
            print(job["job_id"], "complete", flush=True)
        result_hashes = {j["job_id"]: sha256(run / "results" / (j["job_id"] + ".json")) for j in jobs}
        save(folder / "completion.json", {"status": "complete", "jobs": len(jobs),
                                           "result_hashes": result_hashes, "ended": now()})
        return {"status": "complete", "shard": shard, "jobs": len(jobs)}
    except Exception as error:
        save(folder / ("failure-" + now().replace(":", "") + ".json"),
             {"type": type(error).__name__, "message": str(error), "time": now()})
        raise
