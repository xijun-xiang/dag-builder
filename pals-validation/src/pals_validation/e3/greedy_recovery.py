"""Versioned, once-only import of sealed greedy outputs; never regenerate raw.

Source evidence is copied byte-for-byte into the new run and hash-pinned by its
manifest. New protocol envelopes explicitly cite the original envelope. Scores
are reused only when the complete deterministic parse (except version) agrees.
"""
from pathlib import Path
import shutil

from ..io import digest, read, save, sha256, verify
from .greedy_parse import V1_VERSION, VERSION, parse, parse_v1
from .schema import require

def plan(source: Path) -> dict:
    from .greedy import SLOTS, batches_for, raw_batch
    require(source.resolve(strict=True) == source, "recovery source symlink")
    old = read(source / "manifest.json")
    require(old["protocol_id"] == digest({k: v for k, v in old.items() if k != "protocol_id"}),
            "source manifest identity")
    verify(source, old["files"])
    files = {"manifest.json": sha256(source / "manifest.json"), **old["files"]}
    require(all(not Path(rel).is_absolute() and '..' not in Path(rel).parts for rel in files),
            "unsafe source manifest path")
    slots = {}
    for slot in SLOTS:
        folder = source / slot
        config = read(folder / "config.json")
        require(config["parser_version"] == V1_VERSION, "only frozen v1 imports supported")
        batches = read(folder / "batches.json")
        require(batches == batches_for(read(source / "inputs/problems.json"), config), "source schedule")
        attempts = {p.name for p in (folder / "attempts").glob("*.json")}
        raw = {p.name for p in (folder / "raw").glob("*.json")}
        receipts = {p.name for p in (folder / "receipts").glob("*.json")}
        require(attempts == raw == receipts, "UNCERTAIN_GENERATION_NO_RETRY: source not sealed")
        require(raw <= {b["batch_id"] + ".json" for b in batches}, "extra source batch")
        item_files = set()
        for batch in batches:
            name = batch["batch_id"] + ".json"
            if name not in raw:
                continue
            raw_batch(folder, batch, old["protocol_id"])
            attempt = read(folder / "attempts" / name)
            require(attempt["protocol_id"] == old["protocol_id"] and attempt["batch"] == batch,
                    "source attempt mismatch")
            item_files.update(digest(i) + ".json" for i in batch["problem_ids"])
        for kind in ("scores", "outcomes"):
            require({p.name for p in (folder / kind).glob("*.json")} <= item_files, "orphan source evidence")
        for kind in ("attempts", "raw", "receipts", "scores", "outcomes", "workers"):
            for path in (folder / kind).glob("*.json"):
                require(path.is_file() and not path.is_symlink(), "unsafe source evidence")
                files[str(path.relative_to(source))] = sha256(path)
        audit_path = folder / "audit.json"
        if audit_path.exists():
            audit = read(audit_path)
            require(audit["status"] == "PASS" and audit["protocol_id"] == old["protocol_id"], "source audit")
            verify(folder, audit["files"])
            files[str(audit_path.relative_to(source))] = sha256(audit_path)
        slots[slot] = {"sealed_batches": len(raw), "generated_questions": len(item_files)}
    return {"version": "sealed-greedy-v1-import-v1", "source": str(source),
            "source_protocol_id": old["protocol_id"], "files": files, "slots": slots}


def parse_equivalent(before: dict, after: dict) -> bool:
    return {k: v for k, v in before.items() if k != "parser_version"} == {
        k: v for k, v in after.items() if k != "parser_version"}


def origin(plan: dict, path: str) -> dict:
    return {"source_protocol_id": plan["source_protocol_id"],
            "source_file": path, "source_sha256": plan["files"][path]}


def import_sealed(source: Path, root: Path, planned: dict) -> dict:
    from .greedy import SLOTS
    require(plan(source) == planned, "source changed before recovery")
    manifest = read(root / "manifest.json")
    require(manifest["recovery"] == {"plan_sha256": digest(planned),
            "source_protocol_id": planned["source_protocol_id"]}, "unbound recovery plan")
    recovery = root / "recovery"
    require(not recovery.exists(), "recovery already attempted; do not retry")
    recovery.mkdir(mode=0o700)
    save(recovery / "plan.json", planned)
    evidence = recovery / "source"
    evidence.mkdir(mode=0o700)
    for rel, expected in planned["files"].items():
        target = evidence / rel
        require(target.resolve().is_relative_to(evidence), "unsafe source path")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(sha256(source / rel) == expected, "source changed during copy")
        shutil.copyfile(source / rel, target)
        require(sha256(target) == expected, "copy mismatch")
    old_manifest = read(evidence / "manifest.json")
    old_policy, new_policy = read(evidence / "grading/policy.json"), read(root / "grading/policy.json")
    require({k: v for k, v in old_policy.items() if k != "selftest_sha256"} ==
            {k: v for k, v in new_policy.items() if k != "selftest_sha256"}, "grading policy changed")
    for key in ("counts", "scientific_evidence", "deployment", "initial_batches",
                "generation_attempts_per_question", "coverage_review", "analysis"):
        require(manifest[key] == old_manifest[key], "scientific setting changed: " + key)
    for rel in ("inputs/problems.json", "inputs/prepared-manifest.json", "grading/answers.json"):
        require(sha256(root / rel) == planned["files"][rel], "input cohort changed")
    protocol_id = manifest["protocol_id"]
    stats = {}
    for slot in SLOTS:
        folder = root / slot
        before, after = read(evidence / slot / "config.json"), read(folder / "config.json")
        require({**before, "parser_version": VERSION} == after, "more than parser changed")
        for rel in ("batches.json", "model-files.json"):
            if (folder / rel).exists():
                require(sha256(folder / rel) == planned["files"][f"{slot}/{rel}"], "schedule/model changed")
        stats[slot] = {"imported_raw_questions": 0, "reused_scores": 0,
                       "scores_to_compute": 0, "reused_outcomes": 0, "newly_valid": 0}
        for path in sorted((evidence / slot / "raw").glob("*.json")):
            original = read(path)
            raw_origin = origin(planned, f"{slot}/raw/{path.name}")
            derived = {**original, "protocol_id": protocol_id, "recovery_origin": raw_origin}
            save(folder / "raw" / path.name, derived)
            raw_hash = sha256(folder / "raw" / path.name)
            attempt_rel = f"{slot}/attempts/{path.name}"
            save(folder / "attempts" / path.name, {**read(evidence / attempt_rel),
                 "protocol_id": protocol_id, "recovery_origin": origin(planned, attempt_rel)})
            save(folder / "receipts" / path.name, {"protocol_id": protocol_id, "sha256": raw_hash})
            for row in original["output"]["rows"]:
                stats[slot]["imported_raw_questions"] += 1
                item = row["problem_id"]
                a = parse_v1(row["raw_text"], row["finish_reason"], original["batch"]["benchmark"])
                b = parse(row["raw_text"], row["finish_reason"], original["batch"]["benchmark"])
                stats[slot]["newly_valid"] += int(b["process_valid"] and not a["process_valid"])
                rel = f"{slot}/scores/{digest(item)}.json"
                if rel in planned["files"] and parse_equivalent(a, b):
                    score = read(evidence / rel)
                    require(score["raw_sha256"] == raw_origin["source_sha256"] and
                            score["protocol_id"] == planned["source_protocol_id"] and
                            score["parser_version"] == V1_VERSION and score["problem_id"] == item,
                            "source score provenance")
                    save(folder / "scores" / (digest(item) + ".json"), {**score,
                         "protocol_id": protocol_id, "raw_sha256": raw_hash, "parser_version": VERSION,
                         "recovery_origin": origin(planned, rel)})
                    stats[slot]["reused_scores"] += 1
                else:
                    stats[slot]["scores_to_compute"] += 1
                rel = f"{slot}/outcomes/{digest(item)}.json"
                if rel in planned["files"] and a["answer"] == b["answer"]:
                    outcome = read(evidence / rel)
                    require(outcome["raw_sha256"] == raw_origin["source_sha256"] and
                            outcome["protocol_id"] == planned["source_protocol_id"] and
                            outcome["problem_id"] == item and outcome["status"] != "infrastructure_error",
                            "source outcome provenance")
                    save(folder / "outcomes" / (digest(item) + ".json"), {**outcome,
                         "protocol_id": protocol_id, "raw_sha256": raw_hash,
                         "recovery_origin": origin(planned, rel)})
                    stats[slot]["reused_outcomes"] += 1
    result = {"status": "PASS", "protocol_id": protocol_id, "plan_sha256": digest(planned), "slots": stats}
    save(recovery / "ready.json", result)
    return result


def validate_ready(root: Path, manifest: dict) -> dict:
    planned = read(root / "recovery/plan.json")
    require(manifest["recovery"] == {"plan_sha256": digest(planned),
            "source_protocol_id": planned["source_protocol_id"]}, "recovery plan identity")
    ready = read(root / "recovery/ready.json")
    require(ready["status"] == "PASS" and ready["protocol_id"] == manifest["protocol_id"] and
            ready["plan_sha256"] == digest(planned), "recovery incomplete")
    return planned


def verify_imports(root: Path, slot: str, manifest: dict):
    """CPU final audit: every original hash and every reused field is replayed."""
    planned = validate_ready(root, manifest)
    evidence = root / "recovery/source"
    verify(evidence, planned["files"])
    for kind in ("raw", "attempts", "scores", "outcomes"):
        for path in (root / slot / kind).glob("*.json"):
            rel = f"{slot}/{kind}/{path.name}"
            current = read(path)
            if "recovery_origin" not in current:
                require(kind in ("scores", "outcomes") or rel not in planned["files"],
                        "source generation replaced")
                continue
            require(current["recovery_origin"] == origin(planned, rel), "import origin mismatch")
            original = read(evidence / rel)
            expected = {**original, "protocol_id": manifest["protocol_id"],
                        "recovery_origin": origin(planned, rel)}
            if kind in ("scores", "outcomes"):
                expected["raw_sha256"] = current["raw_sha256"]  # checked against actual raw by core audit
            if kind == "scores":
                expected["parser_version"] = VERSION
            require(current == expected, "imported evidence modified")
