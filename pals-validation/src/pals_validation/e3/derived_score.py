"""Score existing E3 raw only, in a new derived directory. No generate/evaluate API."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..io import digest, read, save, sha256, verify
from ..locking import exclusive_lock
from .audit import _score_matches, token_encoder
from .decoupled import parse_decoupled
from .metrics import summarize_trace
from .protocol import parse_trace, score_text_pair
from .reparse import inspect_source
from .run import _backend, code_hashes
from .schema import require

# Reviewed b384ae4 -> f60618c: only base_prompt passes the explicit prompt
# version instead of using messages()'s v1 default. score_pair is byte-identical.
# Every actual base prompt is additionally compared with its frozen raw below.
_REVIEWED_E3_BACKEND_PAIR = (
    "68892228ed5bfbc9901105644616e263b324d7c4d7fd0181ba6148d27811e871",
    "a8c3e20f66c984db545a347006191d5b215afd5c0bf588e77621e2089ebce693",
)


def load_inputs(replay: Path, source: Path, label: str):
    manifest = read(replay / "manifest.json")
    verify(replay, manifest["files"])
    for name, expected in manifest["code_hashes"].items():
        require(sha256(Path(__file__).with_name(name)) == expected, "replay implementation changed")
    matches = [origin for origin in manifest["sources"] if origin["label"] == label]
    require(len(matches) == 1, "unknown or repeated source label")
    origin = matches[0]
    verify(source, origin["source_hashes"])
    protocol, config, raw_rows, hashes = inspect_source(source)
    require(protocol["protocol_id"] == origin["source_protocol_id"] and
            hashes == origin["source_hashes"], "source identity differs")
    current = code_hashes()
    script_root = Path(__file__).resolve().parents[3] / "scripts"
    launcher_hashes = {name: sha256(script_root / name) for name in
                       ("b1-e3-derived-score.sbatch", "e3_launch_derived.py")}
    for name in ("backend.py", "metrics.py", "model_policy.py", "e3/metrics.py"):
        require(current[name] == protocol["code_hashes"][name], "likelihood kernel changed")
    backend_pair = (protocol["code_hashes"]["e3/backend.py"], current["e3/backend.py"])
    require(backend_pair[0] == backend_pair[1] or
            (config["prompt_version"] == "native-trace-prompt-v1" and
             backend_pair == _REVIEWED_E3_BACKEND_PAIR), "unreviewed E3 backend change")
    items = [item for item in read(replay / "items.json") if item["run_label"] == label]
    requests = {item["item_key"]: item for item in read(replay / "score-requests.json")}
    require(len(items) == len(raw_rows) and len({i["problem_id"] for i in items}) == len(items),
            "derived coverage differs")
    by_id = {item["problem_id"]: item for item in items}
    for original in raw_rows:
        raw = original["row"]
        item = by_id[raw["problem_id"]]
        parsed = parse_decoupled(raw["raw_text"], raw["finish_reason"], original["benchmark"])
        require(item["parse"] == parsed and item["source_protocol_id"] == protocol["protocol_id"] and
                item["raw_sha256"] == original["raw_sha256"] and
                item["benchmark"] == original["benchmark"] and
                item["item_key"] == label + "/" + raw["problem_id"], "derived parse differs")
        pairs = []
        if parsed["process_valid"]:
            for index in range(1, len(parsed["steps"])):
                full, deleted, target = score_text_pair(raw["prompt"], parsed, index)
                pairs.append({"index": index, "full_context_text": full,
                              "deleted_context_text": deleted, "target_text": target})
        require(item["K"] == (len(pairs) if parsed["process_valid"] else None),
                "derived K differs")
        require(requests[item["item_key"]] == {
            "item_key": item["item_key"], "source_protocol_id": protocol["protocol_id"],
            "raw_sha256": original["raw_sha256"], "original_prompt": raw["prompt"], "pairs": pairs},
            "derived scoring request differs from raw")
    identity = {"source_protocol_id": protocol["protocol_id"], "run_label": label,
                "replay_manifest_sha256": sha256(replay / "manifest.json"),
                "source_reference_sha256": sha256(source / "reference.json") if config["backend"] == "hf" else None,
                "code_hashes": current, "launcher_hashes": launcher_hashes,
                "scientific_evidence": protocol["scientific_evidence"]}
    return identity, config, items, requests


def score_shard(replay: Path, source: Path, label: str, output: Path, shard: int):
    require(0 <= shard < 8, "exactly eight fixed shards")
    identity, config, items, requests = load_inputs(replay, source, label)
    require(source.resolve() not in output.resolve().parents and source.resolve() != output.resolve(),
            "derived output cannot modify original run")
    require(replay.resolve() not in output.resolve().parents and replay.resolve() != output.resolve(),
            "score output cannot modify frozen replay")
    if config["backend"] == "hf":
        allowed = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
        require(bool(os.environ.get("SLURM_JOB_ID")) and allowed in output.resolve().parents,
                "real scoring requires a PALS Slurm allocation")
        reference = read(source / "reference.json")
        require(reference["status"] == "PASS" and
                reference["protocol_id"] == identity["source_protocol_id"] and
                reference["repeat_max_abs"] <= 1e-5 and
                all(reference["masked_loss_errors"][side] <= .005 for side in ("full", "deleted")),
                "original numerical reference did not pass")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    with exclusive_lock(output / f"shard-{shard}.lock"):
        path = output / f"shard-{shard}.json"
        require(not path.exists(), "derived shard already exists; no silent rerun")
        selected = items[shard::8]
        backend = _backend(config) if any(item["K"] for item in selected) else None
        problems = {p["problem_id"]: p for p in read(source / "inputs/problems.json")}
        results = []
        for item in selected:
            request = requests[item["item_key"]]
            steps = []
            if item["parse"]["process_valid"]:
                if item["K"]:
                    require(backend.base_prompt(problems[item["problem_id"]]) == request["original_prompt"],
                            "original generation prompt changed")
                for pair in request["pairs"]:
                    scored = backend.score_pair(pair["full_context_text"], pair["deleted_context_text"], pair["target_text"])
                    steps.append({k: v for k, v in pair.items() if k != "target_text"} | scored)
                result = {"status": "ok", "reason": None, "steps": steps,
                          "summary": summarize_trace([step["score"] for step in steps])}
            else:
                result = {"status": "invalid_process", "reason": item["parse"]["reason"],
                          "steps": [], "summary": None}
            results.append({"item_key": item["item_key"], "problem_id": item["problem_id"],
                            "benchmark": item["benchmark"], "raw_sha256": item["raw_sha256"], **result})
        payload = {"schema_version": "pals_e3_derived_scores_v1", "identity": identity,
                   "shard": shard, "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "items": results}
        save(path, payload)
    return {"shard": shard, "items": len(results)}


def audit_scores(replay: Path, source: Path, label: str, output: Path):
    identity, config, items, requests = load_inputs(replay, source, label)
    require(not (output / "audit.json").exists(), "derived audit already exists")
    require({p.name for p in output.glob("shard-*.json")} ==
            {f"shard-{n}.json" for n in range(8)}, "derived shard set differs")
    encode = token_encoder(config)
    collected = []
    for shard in range(8):
        result = read(output / f"shard-{shard}.json")
        require(result["identity"] == identity and result["shard"] == shard,
                "derived score identity changed")
        expected = items[shard::8]
        require([r["item_key"] for r in result["items"]] == [r["item_key"] for r in expected],
                "derived score coverage changed")
        for scored, item in zip(result["items"], expected):
            require(scored["raw_sha256"] == item["raw_sha256"] and
                    scored["problem_id"] == item["problem_id"] and scored["benchmark"] == item["benchmark"],
                    "derived score source mismatch")
            _score_matches(scored, item["parse"], requests[item["item_key"]]["original_prompt"], encode)
            row = {k: v for k, v in scored.items() if k != "steps"}
            baseline = source / "scores" / (digest(item["problem_id"]) + ".json")
            if baseline.is_file() and item["parse"]["strict_process_valid"]:
                old = read(baseline)
                require(old["raw_sha256"] == item["raw_sha256"] and
                        old["protocol_id"] == identity["source_protocol_id"], "old score identity changed")
                strict = parse_trace(item["parse"]["raw_text"], item["parse"]["finish_reason"])
                _score_matches(old, strict, requests[item["item_key"]]["original_prompt"], encode)
                row["baseline_comparison"] = {
                    "sha256": sha256(baseline),
                    "max_abs_g_delta": max((abs(a["score"]["g"] - b["score"]["g"])
                                            for a, b in zip(old["steps"], scored["steps"])), default=0.)}
            collected.append(row)
    result = {"status": "PASS", "identity": identity, "items": collected,
              "files": {f"shard-{n}.json": sha256(output / f"shard-{n}.json") for n in range(8)},
              "scope": "likelihood replay only; no new generation or answer correctness evaluation"}
    save(output / "audit.json", result)
    return {"status": "PASS", "items": len(collected)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("score", "audit"))
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--shard", type=int)
    args = parser.parse_args(argv)
    os.umask(0o077)
    import json
    common = (args.replay, args.source_run, args.run_label, args.out)
    if args.stage == "score":
        require(args.shard is not None, "score requires shard")
        result = score_shard(*common, args.shard)
    else:
        result = audit_scores(*common)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
