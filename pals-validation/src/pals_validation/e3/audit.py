"""Replay immutable E3 evidence without loading a model or running code."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..io import digest, read, save, sha256
from ..metrics import pair
from .metrics import summarize_trace
from .protocol import parse_trace, score_text_pair
from .run import canary_batches, validate_run
from .schema import require


def token_encoder(config: dict):
    """Reload only the frozen tokenizer; no model forward pass or network access."""
    if config["backend"] == "mock":
        return lambda text: list(text.encode())
    from transformers import AutoTokenizer
    from ..model_policy import local_code_policy
    model_path = config["model"]["path"]
    runtime = {**config["hf_runtime"], "model_revision": config["model"]["revision"]}
    reviewed = local_code_policy(model_path, runtime)
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=reviewed)
    return lambda text: tokenizer.encode(text, add_special_tokens=False)


def _score_matches(saved: dict, parsed: dict, base: str, encode) -> None:
    require(saved["status"] == ("ok" if parsed["process_valid"] else "invalid_process"),
            "score validity differs from raw parse")
    if not parsed["process_valid"]:
        require(saved["summary"] is None and not saved["steps"] and
                saved["reason"] == parsed["reason"], "invalid score not faithfully recorded")
        return
    require(len(saved["steps"]) == max(0, len(parsed["steps"]) - 1), "missing score step")
    rows = []
    for index, step in enumerate(saved["steps"], start=1):
        require(step["index"] == index, "score step order changed")
        full, deleted, target = score_text_pair(base, parsed, index)
        require((step["full_context_text"], step["deleted_context_text"],
                 step["evidence"]["target_text"]) == (full, deleted, target),
                "saved score text changed")
        evidence = step["evidence"]
        require(evidence["target_ids"] == encode(target) and
                evidence["full_context_ids"] == encode(full) and
                evidence["deleted_context_ids"] == encode(deleted),
                "stored token IDs disagree with frozen tokenizer")
        require(len(evidence["target_ids"]) == len(evidence["full_logprobs"]) ==
                len(evidence["deleted_logprobs"]) > 0, "target token evidence mismatch")
        require(bool(evidence["full_context_ids"]) and bool(evidence["deleted_context_ids"]),
                "empty scoring context")
        expected = pair(evidence["full_logprobs"], evidence["deleted_logprobs"])
        require(all(abs(step["score"][key] - value) <= 1e-8
                    for key, value in expected.items()), "step score cannot be replayed")
        rows.append(expected)
    expected = summarize_trace(rows)
    require(saved["summary"] == expected, "G/M/W cannot be replayed")


def _expected_worker_completion(run: Path, protocol: dict, batches: list[dict],
                                stage: str, shards: int, canary: bool = False) -> None:
    canary_ids = canary_batches(batches) if canary else None
    for shard in range(shards):
        selected = [batch for index, batch in enumerate(batches)
                    if index % shards == shard and
                    (canary_ids is None or batch["batch_id"] in canary_ids)]
        label = stage + ("-canary" if canary else "")
        expected = {"stage": label, "shard": shard, "batches": len(selected),
                    "problems": sum(len(b["problem_ids"]) for b in selected),
                    "protocol_id": protocol["protocol_id"]}
        require(read(run / "workers" / f"{label}-{shard}.json") == expected,
                "worker completion missing or differs: " + stage + ":" + str(shard))


def audit_run(run: str | Path, stage: str, output: str | Path) -> dict:
    run, output = Path(run), Path(output)
    require(stage in ("generate", "score", "evaluate", "all", "canary"), "unknown audit stage")
    protocol, config, batches, problems = validate_run(run)
    encode = token_encoder(config) if stage in ("score", "all", "canary") else None
    shards = config["execution"]["shards"]
    canary = stage == "canary"
    required = ("generate",) if stage == "generate" else (
        ("generate", "score") if stage == "score" else
        ("generate", "evaluate") if stage == "evaluate" else
        ("generate", "score", "evaluate"))
    for name in required:
        _expected_worker_completion(run, protocol, batches, name, shards, canary)
    if canary:
        selected_ids = canary_batches(batches)
        batches = [batch for batch in batches if batch["batch_id"] in selected_ids]
    require(not output.exists(), "audit output already exists")
    expected_batch_ids = {batch["batch_id"] for batch in batches}
    require({p.stem for p in (run / "attempts").glob("*.json")} == expected_batch_ids and
            {p.stem for p in (run / "generation_batches").glob("*.json")} == expected_batch_ids,
            "generation attempt/raw set is incomplete or has extra entries")
    expected_item_files = {digest(item) for batch in batches for item in batch["problem_ids"]}
    if "score" in required:
        require({p.stem for p in (run / "scores").glob("*.json")} == expected_item_files and
                {p.stem for p in (run / "parsed").glob("*.json")} == expected_item_files,
                "score/parse set incomplete or has extra entries")
    if "evaluate" in required:
        require({p.stem for p in (run / "outcomes").glob("*.json")} == expected_item_files,
                "outcome set incomplete or has extra entries")
    seen, counts = set(), Counter()
    rows = []
    for batch in batches:
        raw_file = run / "generation_batches" / (batch["batch_id"] + ".json")
        raw = read(raw_file)
        require(raw["batch"] == batch and raw["protocol_id"] == protocol["protocol_id"] and
                raw["seed"] == batch["seed"] and raw["batch_size"] == len(batch["problem_ids"]),
                "generation batch identity changed")
        require([r["problem_id"] for r in raw["rows"]] == batch["problem_ids"],
                "generation batch membership changed")
        attempt = read(run / "attempts" / (batch["batch_id"] + ".json"))
        require(attempt["batch_id"] == batch["batch_id"] and
                attempt["protocol_id"] == protocol["protocol_id"], "attempt identity changed")
        for generated in raw["rows"]:
            item = generated["problem_id"]
            require(item not in seen, "question generated more than once")
            seen.add(item)
            parsed = parse_trace(generated["raw_text"], generated["finish_reason"])
            require(parsed == generated["parse"], "raw parse changed")
            counts["planned"] += 1
            counts["complete_process"] += int(parsed["process_valid"])
            counts["answer_valid"] += int(parsed["answer"]["valid"])
            row = {"problem_id": item, "batch_id": batch["batch_id"],
                   "raw_sha256": sha256(raw_file), "process_valid": parsed["process_valid"],
                   "process_reason": parsed["reason"], "answer_valid": parsed["answer"]["valid"],
                   "step_count": len(parsed["steps"]), "finish_reason": generated["finish_reason"]}
            if "score" in required:
                stored_parse = read(run / "parsed" / (digest(item) + ".json"))
                require(stored_parse == {"problem_id": item, "raw_sha256": sha256(raw_file),
                                         "parse": parsed}, "separate parse changed")
                scored = read(run / "scores" / (digest(item) + ".json"))
                require(scored["protocol_id"] == protocol["protocol_id"] and
                        scored["raw_sha256"] == sha256(raw_file) and
                        scored["problem_id"] == item, "score identity changed")
                _score_matches(scored, parsed, generated["prompt"], encode)
                row["summary"] = scored["summary"]
                counts["G_valid"] += int(bool(scored["summary"]) and scored["summary"]["G"] is not None)
                counts["W_valid"] += int(bool(scored["summary"]) and scored["summary"]["W"] is not None)
            if "evaluate" in required:
                outcome = read(run / "outcomes" / (digest(item) + ".json"))
                require(outcome["problem_id"] == item and
                        outcome["protocol_id"] == protocol["protocol_id"] and
                        outcome["raw_sha256"] == sha256(raw_file) and
                        outcome["answer"] == parsed["answer"], "outcome identity changed")
                row["outcome"] = outcome["status"]
                row["correct"] = outcome["correct"]
                counts["outcome_" + outcome["status"]] += 1
            rows.append(row)
    expected_items = ({item for batch in batches for item in batch["problem_ids"]}
                      if canary else {p["problem_id"] for p in problems})
    require(seen == expected_items, "run misses selected problems")
    result = {"schema_version": "pals_e3_audit_v1", "stage": stage,
              "run_protocol_id": protocol["protocol_id"],
              "scientific_evidence": protocol["scientific_evidence"],
              "model_id": config["model"]["id"], "counts": dict(counts),
              "items": rows, "source_run": str(run.resolve())}
    output.mkdir(mode=0o700, parents=True)
    save(output / "audit.json", result)
    return {k: result[k] for k in ("stage", "model_id", "counts", "scientific_evidence")}
