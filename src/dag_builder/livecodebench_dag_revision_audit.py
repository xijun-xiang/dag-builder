"""Offline replay of the bounded LiveCodeBench DAG revision canary."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from .config import Config
from .livecodebench_dag_revision import _normalize, verify_prepared
from .calibri_normalize import assemble_graph, public_input
from .response_contract import check_response
from .schemas import parse_object, require
from .stages import payload
from .storage import digest, read_json, write_once
from .t2ance_v4 import assemble_graph_v4, validate_audit_v4


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(root):
    root = Path(root).resolve()
    config = Config.load(root / "run_config.json")
    require(Config.load(root / "config.json") == config,
            "run config differs from prepared config")
    items = verify_prepared(root, config)
    manifest = read_json(root / "revision-manifest.json")
    baseline = Path(manifest["baseline"])
    require(_sha(baseline / "flow_175.jsonl") ==
            read_json(baseline / "manifest.json")["flow_sha256"] ==
            read_json(root / "selection.json")["baseline_flow_sha256"],
            "prior release changed")
    origin = read_json(root / "code_origin.json")
    require(origin == read_json(root / "controller/code/snapshot_origin.json"),
            "run code origin differs from snapshot")
    for name, expected in origin["source_files"].items():
        require(_sha(root / "controller/code/dag_builder" / name) == expected,
                "frozen run code changed")
    completion = read_json(root / "completion.json")
    require(completion["status"] in ("processed", "paused")
            and completion["summary"]["selected"] == len(items),
            "missing or incomplete run completion")
    statuses, attempts, accounted = Counter(), 0, 0
    for item in items:
        item_id = item["item_id"]
        feedback = read_json(root / "feedback" / (item_id + ".json"))
        tier = feedback["source_tier"]
        directory = root / "items" / item_id
        result_path = directory / "result.json"
        require(result_path.is_file() or completion["status"] == "paused",
                "processed item is missing terminal result")
        result = read_json(result_path) if result_path.exists() else None
        if result:
            statuses[result["status"]] += 1
            require(result["item_id"] == item_id, "wrong result item")
        for stage in ("revise", "dependencies", "review_dag"):
            stage_dir = directory / stage
            if not stage_dir.exists():
                continue
            input_record = read_json(stage_dir / "input.json")
            expected = payload(stage, input_record["input"], config)
            require(input_record["payload_sha256"] == digest(expected),
                    "stage input or prompt changed")
            requests = sorted(stage_dir.glob("attempt-*/request.json"))
            for request_file in requests:
                attempts += 1
                request = read_json(request_file)
                require(request["payload"] == expected,
                        "request differs from frozen stage input")
                attempt = request_file.parent
                response_file = attempt / "response.json"
                if not response_file.exists():
                    require((attempt / "error.json").exists()
                            or completion["status"] == "paused",
                            "unknown remote outcome in completed run")
                    accounted += request["reserved_tokens"]
                    continue
                body = read_json(response_file)["body"]
                contract = check_response(expected, body, request["reserved_tokens"],
                    strict=config.strict_response_contract,
                    content_gated=config.content_gated_response)
                require(not contract["violations"] and
                        contract == read_json(attempt / "contract_check-v2.json"),
                        "response contract not reproducible")
                accounted += contract["accounted_tokens"]
                output_file = stage_dir / "output.json"
                if output_file.exists():
                    choices = body.get("choices", [])
                    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop"
                            and read_json(output_file) == parse_object(
                                choices[0]["message"].get("content")),
                            "parsed output is not the raw content")
        if result and result["status"] == "model_accepted":
            revision = read_json(directory / "revise/output.json")
            normalized = _normalize(revision, item, tier)
            require(normalized == read_json(directory / "normalization.json"),
                    "accepted normalization not reproducible")
            dependencies = read_json(directory / "dependencies/output.json")
            graph = (assemble_graph(dependencies, normalized, item)
                     if tier == "CALIBRI" else
                     assemble_graph_v4(dependencies, normalized, item))
            audit_value = read_json(directory / "review_dag/output.json")
            validate_audit_v4(audit_value)
            dag = read_json(directory / "dag.json")
            require(audit_value["decision"] == "accept"
                    and dag["nodes"] == graph["nodes"]
                    and dag["normalization"] == normalized
                    and dag["dag_review"] == audit_value
                    and dag["source"] == item
                    and dag["revision_feedback_sha256"] == digest(feedback)
                    and dag["formal_eligible"] is False
                    and result["dag_sha256"] == digest(dag),
                    "accepted revised DAG does not replay")
    require(attempts == completion["summary"]["api_attempts"],
            "attempt count differs from application completion")
    report = {"protocol": config.prompt_version, "selected": len(items),
              "counts": dict(statuses), "api_attempts": attempts,
              "accounted_tokens": accounted,
              "baseline_flow_sha256": read_json(root / "selection.json")[
                  "baseline_flow_sha256"],
              "mechanical_pass": True,
              "claim": "same-model revision and review only; human_approved=0; formal_eligible=false"}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    write_once(args.root / "offline-audit.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
