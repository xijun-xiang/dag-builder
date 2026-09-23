"""CPU-gated t2ance explanation DAGs using the existing strict step review.

This source has its own provenance and budget. It deliberately reuses the
CALIBRI structural validators, not CALIBRI's source or acceptance labels.
"""

import json
from pathlib import Path

from .calibri_pipeline import CALIBRIPipeline
from .config import Config
from .livecodebench_dag import sha256, verify_execution
from .livecodebench_source import select_canary
from .pipeline import Pipeline
from .schemas import require
from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .t2ance_source import inspect_candidate

PROTOCOL = "t2ance-lcb-normalize-v1"
CALL_LIMIT = 240
TOKEN_LIMIT = 16000000


def prepare(source, execution, expected_manifest, root, *, limit=None,
            development_run=None, max_calls=24, max_reserved_tokens=2000000,
            prior_calls=0, prior_reserved_tokens=0):
    """Freeze reviewed input only after the same independent CPU gate passes."""
    source, execution, root = Path(source), Path(execution), private_dir(root)
    require(type(max_calls) is int and type(prior_calls) is int and prior_calls >= 0
            and 0 < max_calls <= CALL_LIMIT - prior_calls, "t2ance call allocation exceeded")
    require(type(max_reserved_tokens) is int and type(prior_reserved_tokens) is int
            and prior_reserved_tokens >= 0
            and 0 < max_reserved_tokens <= TOKEN_LIMIT - prior_reserved_tokens,
            "t2ance token allocation exceeded")
    manifest = read_json(source / "t2ance-manifest.json")
    source_items = read_json(source / "items.json")
    require(manifest["protocol"] == "t2ance-lcb-source-v1"
            and digest(source_items) == manifest["items_sha256"], "source selection changed")
    cpu, completion = verify_execution(execution, expected_manifest)
    cpu_manifest = read_json(expected_manifest)
    require(cpu_manifest["t2ance_manifest_sha256"] == digest(manifest)
            and set(cpu) == {i["item_id"] for i in source_items},
            "execution is not bound to the t2ance source")
    passed, exclusions = [], []
    for item in source_items:
        original, result = cpu[item["item_id"]]
        require(original["code"] == item["reference_code"]
                and all(original[k] == item[k] for k in ("tests_sha256", "source_content_sha256")),
                "source differs from executed program")
        raw = read_json(source / "source-rows" / (item["item_id"] + ".json"))
        require(digest(raw) == item["origin"]["selected_columns_sha256"]
                and raw["solution_code"] == item["reference_code"]
                and raw["full_response"] == item["raw_output"]
                # The CPU proof below already requires the harness's exact
                # static policy; source recheck here is non-executing.
                and inspect_candidate(raw, item, lambda code: None)["candidate"],
                "frozen upstream response changed")
        if result["status"] != "passed":
            exclusions.append({"item_id": item["item_id"],
                               "reason": "reference_execution_not_passed"})
            continue
        evidence = {"status": "passed", "job_id": completion["job_id"],
                    "completion_sha256": sha256(execution / "completion.json"),
                    "manifest_sha256": sha256(expected_manifest),
                    "result_sha256": digest(result),
                    "code_sha256": original["code_sha256"],
                    "test_count": len(result["tests"])}
        passed.append({**item, "reference_execution": "passed_frozen_tests_not_exhaustive_proof",
                       "execution_evidence": evidence})
    require(passed, "no independent CPU-passing references")
    prior_ids = set()
    if development_run is not None:
        development_run = Path(development_run)
        config = Config.load(development_run / "run_config.json")
        require(read_json(development_run / "completion.json")["status"] == "processed",
                "development run must be completed before exclusion")
        prior_items = verify_prepared(development_run, config)
        prior_ids = {i["item_id"] for i in prior_items}
        require(prior_ids <= {i["item_id"] for i in passed}
                and read_json(development_run / "t2ance-normalization-manifest.json")[
                    "source_manifest_sha256"] == digest(manifest),
                "development run source differs")
    remaining = [i for i in passed if i["item_id"] not in prior_ids]
    if limit is not None:
        require(type(limit) is int and 1 <= limit <= len(remaining), "invalid bounded cohort")
        items = select_canary(remaining, limit, 20260923)
    else:
        items = remaining
    require(items, "no unprocessed CPU-passing candidates")
    exclusions.extend({"item_id": item_id, "reason": "development_run_already_processed"}
                      for item_id in sorted(prior_ids))
    selected_ids = {i["item_id"] for i in items}
    exclusions.extend({"item_id": i["item_id"], "reason": "held_for_later_batch"}
                      for i in remaining if i["item_id"] not in selected_ids)
    selection = {"source_candidates": len(source_items), "cpu_passed": len(passed),
                 "selected_ids": [i["item_id"] for i in items], "excluded": exclusions,
                 "selection": "stratified fixed-hash canary" if limit is not None else
                              "all remaining independent-CPU-passing candidates",
                 "development_run": str(development_run) if development_run else None,
                 "no_score_selection": True}
    proof = {"protocol": PROTOCOL, "prompt_version": PROTOCOL,
             "source_manifest_sha256": digest(manifest),
             "items_sha256": digest(items), "selection_sha256": digest(selection),
             "execution_completion_sha256": sha256(execution / "completion.json"),
             "execution_manifest_sha256": sha256(expected_manifest),
             "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens,
             "prior_calls": prior_calls, "prior_reserved_tokens": prior_reserved_tokens,
             "campaign_call_limit": CALL_LIMIT, "campaign_token_limit": TOKEN_LIMIT,
             "formal_eligible": False}
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(root / "t2ance-normalization-manifest.json", proof)
    write_bytes_once(root / "evidence/source-manifest.json",
                     (source / "t2ance-manifest.json").read_bytes())
    for name in ("input-manifest.json", "harness-selftest.json", "completion.json"):
        write_bytes_once(root / "evidence" / name, (execution / name).read_bytes())
    for item in items:
        name = item["item_id"] + ".json"
        write_bytes_once(root / "evidence/results" / name,
                         (execution / "results" / name).read_bytes())
    return selection


def verify_prepared(root, config):
    """Offline gate repeated before every request and by the release audit."""
    proof = read_json(root / "t2ance-normalization-manifest.json")
    items, selection = read_json(root / "items.json"), read_json(root / "selection.json")
    require(proof["protocol"] == PROTOCOL and digest(items) == proof["items_sha256"]
            and digest(selection) == proof["selection_sha256"], "prepared t2ance input changed")
    require(config.task_type == "livecodebench" and config.prompt_version == PROTOCOL
            and config.solution_source == "t2ance_reference_normalization",
            "wrong t2ance protocol")
    require(config.max_calls <= proof["max_calls"] <= CALL_LIMIT - proof["prior_calls"]
            and config.max_reserved_tokens <= proof["max_reserved_tokens"]
            <= TOKEN_LIMIT - proof["prior_reserved_tokens"], "shared t2ance budget exceeded")
    evidence = root / "evidence"
    require(digest(read_json(evidence / "source-manifest.json")) == proof["source_manifest_sha256"]
            and sha256(evidence / "completion.json") == proof["execution_completion_sha256"]
            and sha256(evidence / "input-manifest.json") == proof["execution_manifest_sha256"],
            "source or CPU evidence changed")
    completion = read_json(evidence / "completion.json")
    require(completion["status"] == "processed"
            and completion["input_manifest_sha256"] == proof["execution_manifest_sha256"],
            "unbound CPU completion")
    require([r["status"] for r in read_json(evidence / "harness-selftest.json")]
            == ["passed", "wrong_answer", "passed", "wrong_answer", "passed"],
            "CPU isolation controls failed")
    for item in items:
        path = evidence / "results" / (item["item_id"] + ".json")
        result = read_json(path)
        record = item["execution_evidence"]
        require(sha256(path) == completion["results"][path.name]
                and digest(result) == record["result_sha256"]
                and result["status"] == record["status"] == "passed"
                and record["job_id"] == completion["job_id"], "CPU result changed")
        require(record["completion_sha256"] == proof["execution_completion_sha256"]
                and record["manifest_sha256"] == proof["execution_manifest_sha256"]
                and result["code_sha256"] == record["code_sha256"] == digest(item["reference_code"])
                and result["tests_sha256"] == item["tests_sha256"]
                and result["source_content_sha256"] == item["source_content_sha256"],
                "executed source/code mismatch")
        require(len(result["tests"]) == record["test_count"] > 0
                and all(t["status"] == "passed" for t in result["tests"])
                and {t["split"] for t in result["tests"]} == {"public", "private"},
                "partial CPU test proof")
    return items


class T2ancePipeline(CALIBRIPipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version == PROTOCOL and through == "review_dag",
                "wrong t2ance run protocol")
        verify_prepared(self.root, self.config)
        return Pipeline.run(self, limit, progress, through)


def main():
    import argparse
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "execution", "expected-manifest", "root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--development-run", type=Path)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-reserved-tokens", type=int, default=2000000)
    parser.add_argument("--prior-calls", type=int, default=0)
    parser.add_argument("--prior-reserved-tokens", type=int, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare(args.source, args.execution, args.expected_manifest,
            args.root, limit=args.limit, development_run=args.development_run,
            max_calls=args.max_calls, max_reserved_tokens=args.max_reserved_tokens,
            prior_calls=args.prior_calls, prior_reserved_tokens=args.prior_reserved_tokens),
            ensure_ascii=False))


if __name__ == "__main__":
    main()
