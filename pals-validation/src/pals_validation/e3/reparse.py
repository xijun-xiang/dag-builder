"""Offline replay into new derived files; no generation, model load or code execution.

Usage: python -m pals_validation.e3.reparse --source-manifest INPUT --out NEW_DIR
The input enumerates complete canary runs, not a hand-picked list of failures.
"""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path

from ..io import digest, read, save, sha256, verify
from .audit import _expected_worker_completion
from .decoupled import PARSER_VERSION, parse_decoupled
from .protocol import parse_trace, score_text_pair
from .run import canary_batches
from .schema import require


def inspect_source(run: Path) -> tuple[dict, dict, list[dict], dict]:
    """Verify an explicitly limited generation-only mirror, not a full run audit."""
    protocol = read(run / "protocol.json")
    require(protocol["protocol_id"] == digest({k: v for k, v in protocol.items()
                                             if k != "protocol_id"}), "source protocol changed")
    names = ("config.json", "batches.json", "inputs/problems.json")
    verify(run, {name: protocol["input_files"][name] for name in names})
    config, batches = read(run / "config.json"), read(run / "batches.json")
    require(digest(batches) == protocol["batches_sha256"], "source batches changed")
    require(config["parser_version"] == "strict-tag-v1" and
            config["prompt_version"] in ("native-trace-prompt-v1", "native-trace-prompt-v2"),
            "unsupported source parser/prompt")
    require(config["scoring_version"] == "adjacent-deletion-explicit-boundary-v1",
            "source scoring version differs")
    selected = canary_batches(batches, config.get("canary_batch_index", 0))
    require({p.stem for p in (run / "attempts").glob("*.json")} == selected and
            {p.stem for p in (run / "generation_batches").glob("*.json")} == selected,
            "canary source missing, uncertain or has extra batches")
    _expected_worker_completion(run, protocol, batches, "generate", 8, True,
                                config.get("canary_batch_index", 0))
    files = {name: sha256(run / name) for name in (*names, "protocol.json")}
    for folder in ("attempts", "generation_batches"):
        files.update({str(p.relative_to(run)): sha256(p) for p in sorted((run / folder).glob("*.json"))})
    for shard in range(8):
        name = f"workers/generate-canary-{shard}.json"
        files[name] = sha256(run / name)
    problems = {p["problem_id"]: p for p in read(run / "inputs/problems.json")}
    rows, seen = [], set()
    for batch in batches:
        if batch["batch_id"] not in selected:
            continue
        name = "generation_batches/" + batch["batch_id"] + ".json"
        raw = read(run / name)
        attempt = read(run / "attempts" / (batch["batch_id"] + ".json"))
        require(attempt["batch_id"] == batch["batch_id"] and
                attempt["protocol_id"] == protocol["protocol_id"] and
                attempt["state"] == "attempt_started", "source attempt differs")
        require(raw["batch"] == batch and raw["protocol_id"] == protocol["protocol_id"] and
                raw["seed"] == batch["seed"] and raw["batch_size"] == len(batch["problem_ids"]) and
                raw["scientific_evidence"] == protocol["scientific_evidence"] and
                [r["problem_id"] for r in raw["rows"]] == batch["problem_ids"],
                "source raw identity differs")
        for row in raw["rows"]:
            item = row["problem_id"]
            require(item not in seen and item in problems, "duplicate or unknown source question")
            seen.add(item)
            strict = parse_trace(row["raw_text"], row["finish_reason"])
            require(strict == row["parse"], "source strict parse differs from replay")
            rows.append({"row": row, "benchmark": problems[item]["benchmark"],
                         "raw_file": name, "raw_sha256": files[name]})
    return protocol, config, rows, files


def replay_sources(source_manifest: str | Path, output: str | Path) -> dict:
    manifest_path, output = Path(source_manifest), Path(output)
    require(not output.exists(), "derived output already exists")
    sources = read(manifest_path)
    require(set(sources) == {"runs"} and bool(sources["runs"]), "source manifest fields")
    labels, paths, items, requests, groups, origins = set(), set(), [], [], {}, []
    for entry in sources["runs"]:
        require(set(entry) == {"label", "path", "remote_source"}, "source entry fields")
        label, source = entry["label"], Path(entry["path"]).resolve(strict=True)
        require(label not in labels and source not in paths, "duplicate source run")
        require(output.resolve() != source and source not in output.resolve().parents,
                "output must not modify a source run")
        labels.add(label)
        paths.add(source)
        protocol, config, raw_rows, hashes = inspect_source(source)
        origins.append({**entry, "path": str(source), "source_protocol_id": protocol["protocol_id"],
                        "source_hashes": hashes, "model_id": config["model"]["id"],
                        "prompt_version": config["prompt_version"],
                        "scientific_evidence": protocol["scientific_evidence"]})
        for source_row in raw_rows:
            row, benchmark = source_row["row"], source_row["benchmark"]
            parsed = parse_decoupled(row["raw_text"], row["finish_reason"], benchmark)
            strict = row["parse"]
            pairs = []
            if parsed["process_valid"]:
                for index in range(1, len(parsed["steps"])):
                    full, deleted, target = score_text_pair(row["prompt"], parsed, index)
                    # This is a byte-for-byte context check, not a new likelihood result.
                    if strict["process_valid"]:
                        require((full, deleted, target) == score_text_pair(row["prompt"], strict, index),
                                "previously valid scoring text changed")
                    pairs.append({"index": index, "full_context_text": full,
                                  "deleted_context_text": deleted, "target_text": target})
            if strict["process_valid"]:
                require(parsed["steps"] == strict["steps"], "previously valid step spans changed")
            key = label + "/" + row["problem_id"]
            item = {"item_key": key, "run_label": label, "problem_id": row["problem_id"],
                    "benchmark": benchmark, "source_protocol_id": protocol["protocol_id"],
                    "raw_file": source_row["raw_file"], "raw_sha256": source_row["raw_sha256"],
                    "prompt_sha256": digest(row["prompt"]), "parse": parsed,
                    "K": len(pairs) if parsed["process_valid"] else None,
                    "score_status": "pending_likelihood" if pairs else (
                        "undefined_K0" if parsed["process_valid"] else "invalid_process"),
                    "G": None, "M": None, "W": None}
            items.append(item)
            requests.append({"item_key": key, "source_protocol_id": protocol["protocol_id"],
                             "raw_sha256": source_row["raw_sha256"],
                             "original_prompt": row["prompt"], "pairs": pairs})
            count = groups.setdefault((label, benchmark), Counter())
            count["total"] += 1
            count["strict_valid"] += int(strict["process_valid"])
            count["process_valid"] += int(parsed["process_valid"])
            count["recovered"] += int(parsed["process_valid"] and not strict["process_valid"])
            count["answer_extractable"] += int(parsed["answer"]["valid"])
            count["G_M_eligible"] += int(bool(pairs))
            count["W_eligible"] += int(len(pairs) >= 2)
            count["score_pairs"] += len(pairs)
            if not parsed["process_valid"]:
                count["invalid:" + parsed["reason"]] += 1
    summaries = [{"run_label": label, "benchmark": benchmark, **dict(count)}
                 for (label, benchmark), count in sorted(groups.items())]
    result = {"schema_version": "pals_e3_reparse_summary_v1", "parser_version": PARSER_VERSION,
              "total": len(items), "groups": summaries,
              "scope": "generation-only structural replay; no new likelihood or correctness results"}
    # Sources must stay byte-identical throughout replay.
    for origin in origins:
        verify(Path(origin["path"]), origin["source_hashes"])
    output.mkdir(mode=0o700, parents=True)
    save(output / "items.json", items)
    save(output / "score-requests.json", requests)
    save(output / "summary.json", result)
    save(output / "manifest.json", {
        "schema_version": "pals_e3_reparse_manifest_v1", "parser_version": PARSER_VERSION,
        "source_manifest_sha256": sha256(manifest_path), "sources": origins,
        "code_hashes": {p.name: sha256(p) for p in
                        (Path(__file__), Path(__file__).with_name("decoupled.py"),
                         Path(__file__).with_name("protocol.py"))},
        "files": {name: sha256(output / name) for name in
                  ("items.json", "score-requests.json", "summary.json")}})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    import json
    print(json.dumps(replay_sources(args.source_manifest, args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
