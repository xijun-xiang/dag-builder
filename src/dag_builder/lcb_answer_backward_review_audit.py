"""Offline replay of one-pass structural pruning and fresh semantic review."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from .config import Config
from .lcb_answer_backward_review import verify_prepared
from .livecodebench_dag_revision_audit import audit as audit_previous
from .response_contract import check_response
from .schemas import parse_object, require
from .stages import payload
from .storage import digest, read_json, write_once
from .t2ance_v4 import validate_audit_v4


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(root):
    root = Path(root).resolve()
    config = Config.load(root / "run_config.json")
    require(Config.load(root / "config.json") == config,
            "run config differs from prepared config")
    items = verify_prepared(root, config)
    selection = read_json(root / "selection.json")
    previous = Path(selection["previous_run"])
    prior_audit = audit_previous(previous)
    require(read_json(previous / "offline-audit.json") == prior_audit
            and digest(prior_audit) == selection["previous_audit_sha256"],
            "previous raw revision changed")
    origin = read_json(root / "code_origin.json")
    require(origin == read_json(root / "controller/code/snapshot_origin.json"),
            "run code origin differs from snapshot")
    for name, expected in origin["source_files"].items():
        require(_sha(root / "controller/code/dag_builder" / name) == expected,
                "frozen review code changed")
    completion = read_json(root / "completion.json")
    require(completion["status"] in ("processed", "paused")
            and completion["summary"]["selected"] == len(items),
            "missing or incomplete review completion")
    counts, attempts, accounted = Counter(), 0, 0
    for item in items:
        item_id = item["item_id"]
        directory = root / "items" / item_id
        result_path = directory / "result.json"
        require(result_path.is_file() or completion["status"] == "paused",
                "processed item lacks terminal result")
        result = read_json(result_path) if result_path.exists() else None
        if result:
            require(result["item_id"] == item_id, "result item mismatch")
            counts[result["status"]] += 1
        stage = directory / "review_dag"
        if not stage.exists():
            continue
        entry = read_json(stage / "input.json")
        expected = payload("review_dag", entry["input"], config)
        require(entry["payload_sha256"] == digest(expected)
                and entry["input"]["candidate"] == read_json(
                    root / "candidates" / (item_id + ".json")),
                "review input changed")
        for request_file in sorted(stage.glob("attempt-*/request.json")):
            attempts += 1
            request = read_json(request_file)
            require(request["payload"] == expected, "request differs from frozen input")
            response_file = request_file.parent / "response.json"
            if not response_file.exists():
                require((request_file.parent / "error.json").exists()
                        or completion["status"] == "paused",
                        "unknown remote outcome in completed review")
                accounted += request["reserved_tokens"]
                continue
            body = read_json(response_file)["body"]
            contract = check_response(expected, body, request["reserved_tokens"],
                strict=config.strict_response_contract,
                content_gated=config.content_gated_response)
            require(not contract["violations"] and
                    contract == read_json(request_file.parent / "contract_check-v2.json"),
                    "response contract not reproducible")
            accounted += contract["accounted_tokens"]
            output_path = stage / "output.json"
            if output_path.exists():
                choices = body.get("choices", [])
                require(len(choices) == 1 and choices[0].get("finish_reason") == "stop"
                        and read_json(output_path) == parse_object(
                            choices[0]["message"].get("content")),
                        "review output is not the raw content")
        if result and result["status"] == "model_accepted":
            review = read_json(stage / "output.json")
            validate_audit_v4(review)
            dag = read_json(directory / "dag.json")
            require(review["decision"] == "accept"
                    and dag["source"] == item
                    and dag["nodes"] == entry["input"]["candidate"]["nodes"]
                    and dag["graph_transformation"] == entry["input"]["candidate"][
                        "transformation"]
                    and dag["dag_review"] == review
                    and dag["formal_eligible"] is False
                    and result["dag_sha256"] == digest(dag),
                    "accepted answer-backward DAG does not replay")
    require(attempts == completion["summary"]["api_attempts"],
            "attempt count differs from completion")
    return {"protocol": config.prompt_version, "selected": len(items),
            "counts": dict(counts), "api_attempts": attempts,
            "accounted_tokens": accounted,
            "previous_audit_sha256": digest(prior_audit),
            "mechanical_pass": True,
            "claim": "Only off-answer nodes deleted; same-model review, not human gold"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root)
    write_once(args.root / "offline-audit.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
