"""Explicitly authorized full-denominator continuation; no resampling.

Keep the v2 parser and all scientific inputs. Coverage is descriptive, while
numerical, source-integrity and isolation failures remain fatal. Fixed batches
are claimed once by idle workers; their membership/seed/budget never changes.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

from ..io import digest, read, read_jsonl, save, sha256, verify
from .schema import require

VERSION = "full-denominator-continuation-v1"
EXECUTION = {"version": VERSION, "coverage_policy": "report_invalid",
             "dispatch": "shared_fixed_batch_queue", "startup_gate": "all_worker_numerical",
             "uncertain_source_attempts": "infrastructure_na_no_retry"}


def plan(source: Path) -> dict:
    from .greedy import batches_for, model_slots, raw_batch, validate_config
    from .repair_review import inventory
    require(source.resolve(strict=True) == source, "continuation source symlink")
    old = read(source / "manifest.json")
    require(old["protocol_id"] == digest({k: v for k, v in old.items() if k != "protocol_id"}),
            "source protocol changed")
    require("recovery" not in old and "full_continuation" not in old, "only original extension source supported")
    slots = model_slots(old)
    require(len(slots) == 1 and next(iter(slots)) in ("llama3", "internlm3"), "single extension model required")
    slot = next(iter(slots))
    files = {"manifest.json": sha256(source / "manifest.json"), **old["files"]}
    require(all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in files), "unsafe source path")
    verify(source, files)
    cfg = read(source / slot / "config.json")
    validate_config(cfg)
    batches = read(source / slot / "batches.json")
    require(batches == batches_for(read(source / "inputs/problems.json"), cfg), "source batch schedule")
    inv = inventory(source / slot)
    states = {r["batch_id"]: r["state"] for r in inv["items"]}
    sealed_items = set()
    for batch in batches:
        if states[batch["batch_id"]] == "sealed":
            raw_batch(source / slot, batch, old["protocol_id"])
            sealed_items.update(digest(i) + ".json" for i in batch["problem_ids"])
    for kind in ("scores", "outcomes"):
        require({p.name for p in (source / slot / kind).glob("*.json")} <= sealed_items,
                "orphan source score/outcome")
    for kind in ("attempts", "raw", "receipts", "scores", "outcomes", "workers"):
        for path in (source / slot / kind).glob("*.json"):
            require(path.is_file() and not path.is_symlink(), "unsafe source evidence")
            files[str(path.relative_to(source))] = sha256(path)
    return {"version": VERSION, "source": str(source), "source_protocol_id": old["protocol_id"],
            "slot": slot, "files": files, "batch_states": states, "counts": inv["counts"],
            "planned": inv["planned"]}


def binding(planned):
    return {"plan_sha256": digest(planned), "source_protocol_id": planned["source_protocol_id"],
            "execution": EXECUTION}


def origin(planned, rel):
    return {"source_protocol_id": planned["source_protocol_id"], "source_file": rel,
            "source_sha256": planned["files"][rel]}


def unavailable_record(planned, manifest, batch):
    rel = f"{planned['slot']}/attempts/{batch['batch_id']}.json"
    return {"protocol_id": manifest["protocol_id"], "batch": batch,
            "status": "infrastructure_interrupted_na", "generation_permitted": False,
            "source_attempt": origin(planned, rel)}


def import_source(source: Path, root: Path, planned):
    require(plan(source) == planned, "source changed before import")
    manifest = read(root / "manifest.json")
    require(manifest["full_continuation"] == binding(planned), "unbound continuation")
    recovery = root / "recovery"
    require(not recovery.exists(), "import already attempted; inspect, never retry")
    recovery.mkdir(mode=0o700)
    save(recovery / "plan.json", planned)
    evidence = recovery / "source"
    evidence.mkdir(mode=0o700)
    for rel, expected in planned["files"].items():
        target = evidence / rel
        require(target.resolve().is_relative_to(evidence), "unsafe import path")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(sha256(source / rel) == expected, "source changed during copy")
        shutil.copyfile(source / rel, target)
        require(sha256(target) == expected, "import copy mismatch")
    slot = planned["slot"]
    old = read(evidence / "manifest.json")
    for key in ("counts", "scientific_evidence", "deployment", "initial_batches",
                "generation_attempts_per_question", "coverage_review", "analysis", "model_slots", "deployment_slot"):
        require(manifest[key] == old[key], "scientific setting changed: " + key)
    for rel in ("inputs/problems.json", "inputs/prepared-manifest.json", "grading/answers.json",
                f"{slot}/config.json", f"{slot}/batches.json", f"{slot}/model-files.json"):
        if rel in planned["files"]:
            require(sha256(root / rel) == planned["files"][rel], "input/config/model changed: " + rel)
    a, b = read(evidence / "grading/policy.json"), read(root / "grading/policy.json")
    require({k:v for k,v in a.items() if k != "selftest_sha256"} ==
            {k:v for k,v in b.items() if k != "selftest_sha256"}, "grading policy changed")
    folder = root / slot
    (folder / "unavailable").mkdir(mode=0o700)
    (folder / "dispatch").mkdir(mode=0o700)
    stats = {"imported_raw": 0, "reused_scores": 0, "reused_outcomes": 0, "infrastructure_na": 0}
    for batch in read(folder / "batches.json"):
        bid = batch["batch_id"]
        state = planned["batch_states"][bid]
        if state == "untouched":
            continue
        if state == "infrastructure_interrupted_na":
            save(folder / "unavailable" / (bid + ".json"), unavailable_record(planned, manifest, batch))
            stats["infrastructure_na"] += len(batch["problem_ids"])
            continue
        require(state == "sealed", "unknown source state")
        for kind in ("attempts", "raw"):
            rel = f"{slot}/{kind}/{bid}.json"
            save(folder / kind / (bid + ".json"), {**read(evidence / rel),
                 "protocol_id": manifest["protocol_id"], "recovery_origin": origin(planned, rel)})
        raw_hash = sha256(folder / "raw" / (bid + ".json"))
        save(folder / "receipts" / (bid + ".json"), {"protocol_id": manifest["protocol_id"], "sha256": raw_hash})
        stats["imported_raw"] += len(batch["problem_ids"])
        for item in batch["problem_ids"]:
            for kind in ("scores", "outcomes"):
                rel = f"{slot}/{kind}/{digest(item)}.json"
                if rel not in planned["files"]:
                    continue
                value = read(evidence / rel)
                require(value["problem_id"] == item and value["protocol_id"] == planned["source_protocol_id"] and
                        value["raw_sha256"] == planned["files"][f"{slot}/raw/{bid}.json"], "old score/outcome provenance")
                require(value["status"] != "infrastructure_error", "old evaluator infrastructure failure")
                if kind == "scores":
                    require(value["parser_version"] == read(folder / "config.json")["parser_version"],
                            "old score parser differs")
                save(folder / kind / (digest(item) + ".json"), {**value, "protocol_id": manifest["protocol_id"],
                     "raw_sha256": raw_hash, "recovery_origin": origin(planned, rel)})
                stats["reused_" + kind] += 1
    # Detect a concurrent source writer before authorizing any new generation.
    require(plan(source) == planned, "source changed during import")
    result = {"status": "PASS", "protocol_id": manifest["protocol_id"],
              "plan_sha256": digest(planned), "stats": stats}
    save(recovery / "ready.json", result)
    return result


def validate_ready(root, manifest):
    planned = read(root / "recovery/plan.json")
    require(manifest["full_continuation"] == binding(planned), "continuation contract changed")
    ready = read(root / "recovery/ready.json")
    require(ready["status"] == "PASS" and ready["protocol_id"] == manifest["protocol_id"] and
            ready["plan_sha256"] == digest(planned), "import not ready")
    return planned


def missing_batches(root, slot, manifest):
    if "full_continuation" not in manifest:
        return set()
    planned = validate_ready(root, manifest)
    require(planned["slot"] == slot, "import slot mismatch")
    return {bid for bid, state in planned["batch_states"].items() if state == "infrastructure_interrupted_na"}


def missing_rows(root, slot, batch, manifest):
    planned = validate_ready(root, manifest)
    path = root / slot / "unavailable" / (batch["batch_id"] + ".json")
    require(read(path) == unavailable_record(planned, manifest, batch), "unavailable source changed")
    require(planned["batch_states"][batch["batch_id"]] == "infrastructure_interrupted_na", "unauthorized missing batch")
    for kind in ("raw", "receipts", "attempts"):
        require(not (root / slot / kind / path.name).exists(), "interrupted source was regenerated")
    return [{"problem_id": i, "benchmark": batch["benchmark"], "process_valid": False,
             "steps": 0, "G_defined": False, "finish_reason": "infrastructure_interrupted_na"}
            for i in batch["problem_ids"]]


def missing_score(manifest, batch, item, source_hash):
    return {"protocol_id": manifest["protocol_id"], "problem_id": item, "raw_sha256": None,
            "status": "infrastructure_interrupted_na", "reason": "source_attempt_without_raw_no_retry",
            "steps": [], "summary": None, "unavailable_sha256": source_hash}


def missing_outcome(manifest, item, source_hash):
    return {"protocol_id": manifest["protocol_id"], "problem_id": item, "raw_sha256": None,
            "status": "infrastructure_interrupted_na", "correct": None, "unavailable_sha256": source_hash}


def verify_imports(root, slot, manifest):
    planned = validate_ready(root, manifest)
    evidence = root / "recovery/source"
    verify(evidence, planned["files"])
    for kind in ("raw", "attempts", "scores", "outcomes"):
        for path in (root / slot / kind).glob("*.json"):
            rel = f"{slot}/{kind}/{path.name}"
            value = read(path)
            if "recovery_origin" not in value:
                require(rel not in planned["files"], "imported evidence replaced")
                continue
            require(value["recovery_origin"] == origin(planned, rel), "import origin changed")
            expected = {**read(evidence / rel), "protocol_id": manifest["protocol_id"],
                        "recovery_origin": origin(planned, rel)}
            if kind in ("scores", "outcomes"):
                expected["raw_sha256"] = value["raw_sha256"]  # core audit binds actual raw
            require(value == expected, "reused evidence changed")


def claim_batch(folder, batches, protocol_id, shard, job):
    """Short blocking lock; append-only journal. No reassignment after failures."""
    directory = folder / "dispatch"
    fd = os.open(directory / "queue.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        journal = directory / "claims.jsonl"
        rows = read_jsonl(journal) if journal.exists() else []
        index = len(rows)
        require(index <= len(batches), "too many claims")
        if rows:
            require(rows[-1]["index"] == index - 1 and rows[-1]["protocol_id"] == protocol_id and
                    rows[-1]["job_id"] == job, "prior/uncertain queue ownership")
        if index == len(batches):
            return None
        record = {"protocol_id": protocol_id, "job_id": job, "shard": shard, "index": index,
                  "batch_id": batches[index]["batch_id"]}
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return index, batches[index]
    finally:
        os.close(fd)


def gpu_work(root, slot, shard, backend, manifest, problems, batches, deadline, barrier):
    from .greedy import process_batch
    folder = root / slot
    job = os.environ.get("SLURM_JOB_ID", "mock")
    release = folder / "startup" / f"{job}-numeric-go.json"
    if barrier:
        while not release.exists():
            require(time.time() < deadline, "numerical gate deadline")
            time.sleep(1)
        require(read(release) == {"protocol_id": manifest["protocol_id"], "status": "PASS"}, "wrong numerical release")
    completed = []
    while time.time() < deadline:
        claimed = claim_batch(folder, batches, manifest["protocol_id"], shard, job)
        if claimed is None:
            save(folder / "workers" / f"gpu-complete-{job}-{shard}.json",
                 {"status": "PASS", "protocol_id": manifest["protocol_id"], "batch_indices": completed})
            return True
        index, batch = claimed
        rows = process_batch(root, slot, batch, backend, manifest, problems)
        save(folder / "dispatch" / f"batch-{index:06d}.json", {"protocol_id": manifest["protocol_id"],
             "job_id": job, "shard": shard, "index": index, "batch_id": batch["batch_id"], "rows": rows})
        completed.append(index)
        print(f"{slot} shard={shard} batch={index} persisted", flush=True)
    return False


def verify_dispatch(folder, batches, manifest):
    claims = read_jsonl(folder / "dispatch/claims.jsonl")
    require(len(claims) == len(batches), "incomplete dispatch")
    expected_files = {f"batch-{i:06d}.json" for i in range(len(batches))}
    require({p.name for p in (folder / "dispatch").glob("batch-*.json")} == expected_files, "incomplete dispatch receipts")
    assignments = {i: [] for i in range(8)}
    jobs = set()
    for i, (claim, batch) in enumerate(zip(claims, batches)):
        require(claim == {"protocol_id": manifest["protocol_id"], "job_id": claim["job_id"],
                "shard": claim["shard"], "index": i, "batch_id": batch["batch_id"]} and
                claim["shard"] in assignments, "dispatch claim changed")
        receipt = read(folder / "dispatch" / f"batch-{i:06d}.json")
        require({k:v for k,v in receipt.items() if k != "rows"} == claim and
                [r["problem_id"] for r in receipt["rows"]] == batch["problem_ids"], "dispatch receipt mismatch")
        assignments[claim["shard"]].append(i)
        jobs.add(claim["job_id"])
    require(len(jobs) == 1, "multiple GPU allocation claims")
    job = next(iter(jobs))
    for shard in range(8):
        require((folder / "workers" / f"gpu-complete-{job}-{shard}.json").is_file(),
                "dispatch/completion allocation mismatch")
    return assignments
