"""Tokenize every frozen E1/E2 input on CPU without loading model weights.

This diagnoses capacity only. It neither excludes questions nor modifies the
prepared interventions, output budgets, prompts, or inference configuration.
"""
from functools import lru_cache

from .backend import HFBackend
from .io import read, verify


def audit_prepared(tokenizer, prepared, config):
    manifest = read(prepared / "manifest.json")
    verify(prepared, manifest["files"])
    cases = {c["item_id"]: c for c in read(prepared / "cases.json")}
    jobs = read(prepared / "jobs.json")
    # Only the existing prompt-rendering method is used; __init__ would load
    # weights and is intentionally never called in this CPU capacity audit.
    renderer = object.__new__(HFBackend)
    renderer.tokenizer, renderer.config = tokenizer, config

    @lru_cache(maxsize=8192)
    def length(text):
        return len(tokenizer.encode(text, add_special_tokens=False))

    rows = {kind: {"planned_jobs": 0, "max_tokens": 0, "violations": []} for kind in ("e1", "e2")}
    for job in jobs:
        case = cases[job["item_id"]]
        row = rows[job["kind"]]
        row["planned_jobs"] += 1
        prefix = job["prefix_ids"]
        if prefix.count(job["deleted_id"]) != 1:
            raise ValueError("Frozen deletion is not exactly one prefix node")
        if job["kind"] == "e1":
            full = renderer.context(case, prefix)
            deleted = renderer.context(case, [i for i in prefix if i != job["deleted_id"]])
            target = length(job["target"])
            full_n, deleted_n = length(full) + target, length(deleted) + target
            n = max(full_n, deleted_n)
            details = {"variant": job["variant"], "full_tokens": full_n,
                       "deleted_tokens": deleted_n, "target_tokens": target}
        else:
            prompt_n = length(renderer.context(case, prefix, for_generation=True))
            n = prompt_n + config["max_new_tokens"]
            details = {"prompt_tokens": prompt_n, "reserved_output": config["max_new_tokens"]}
        row["max_tokens"] = max(row["max_tokens"], n)
        if n > config["max_context"]:
            row["violations"].append({"item_id": job["item_id"], "job_id": job["job_id"], **details})
    return {"status": "PASS" if all(not r["violations"] for r in rows.values()) else "CAPACITY_REVIEW_REQUIRED",
            "questions": len(cases), "max_context": config["max_context"],
            "generation_prompt_version": config.get("generation_prompt_version", "v1"),
            "max_new_tokens": config["max_new_tokens"], "arms": rows,
            "note": "CPU token counts only; no generation, no scoring, no exclusions or budget changes."}
