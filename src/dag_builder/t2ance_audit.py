"""Offline audit of a completed, CPU-gated t2ance LiveCodeBench DAG canary."""

import hashlib
from collections import Counter
from pathlib import Path

from .calibri_continuation import _replay_recorded_calls
from .calibri_normalize import validate_audit
from .config import Config
from .schemas import require
from .storage import digest, read_json, write_once
from .t2ance_pipeline import PROTOCOL, verify_prepared


def audit_completed(root):
    root = Path(root)
    config = Config.load(root / "config.json")
    require(config.prompt_version == PROTOCOL, "not a t2ance DAG run")
    items = verify_prepared(root, config)
    completion = read_json(root / "completion.json")
    summary = completion["summary"]
    require(completion["status"] == "processed" and completion["global_stop"] is False
            and summary["selected"] == len(items), "t2ance run is unfinished")
    requests = sorted(root.glob("items/*/*/attempt-*/request.json"))
    responses = sorted(root.glob("items/*/*/attempt-*/response.json"))
    errors = sorted(root.glob("items/*/*/attempt-*/error.json"))
    require(len(requests) == summary["api_attempts"]
            and len(requests) <= config.max_calls
            and len(responses) + len(errors) == len(requests),
            "t2ance request/response inventory mismatch")
    reserved = sum(read_json(path)["reserved_tokens"] for path in requests)
    require(reserved <= config.max_reserved_tokens, "t2ance budget exceeded")
    _replay_recorded_calls(root, config)
    origin = read_json(root / "code_origin.json")
    require(origin == read_json(root / "controller/code/snapshot_origin.json"),
            "t2ance code origin changed")
    for name, expected in origin["source_files"].items():
        path = root / "controller/code/dag_builder" / name
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                "t2ance frozen source changed")
    statuses = Counter()
    accepted = []
    for item in items:
        item_id = item["item_id"]
        directory = root / "items" / item_id
        result = read_json(directory / "result.json")
        require(result["item_id"] == item_id
                and result["status"] in ("model_accepted", "needs_review", "rejected"),
                "t2ance result missing or invalid")
        statuses[result["status"]] += 1
        if result["status"] == "model_accepted":
            dag = read_json(directory / "dag.json")
            require(result["dag_sha256"] == digest(dag)
                    and dag["item_id"] == item_id and dag["source"] == item
                    and dag["construction_protocol"] == PROTOCOL
                    and dag["formal_eligible"] is False,
                    "t2ance accepted graph/source mismatch")
            validate_audit(dag["dag_review"])
            require(dag["dag_review"]["decision"] == "accept",
                    "t2ance graph lacks accepting review")
            accepted.append(item_id)
        else:
            require(not (directory / "dag.json").exists(),
                    "t2ance unaccepted graph was published")
    require(dict(statuses) == summary["counts"], "t2ance summary/result counts differ")
    return {"protocol": "t2ance-lcb-dag-offline-audit-v1", "mechanical_pass": True,
            "selected": len(items), "accepted_ids": accepted,
            "counts": dict(statuses), "api_attempts": len(requests),
            "reserved_tokens": reserved,
            "completion_sha256": digest(completion),
            "prepared_manifest_sha256": digest(read_json(
                root / "t2ance-normalization-manifest.json")),
            "claim": "CPU-bound, same-model reviewed candidates; not official or human gold"}


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    report = audit_completed(args.root)
    write_once(args.root / "offline-audit.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
