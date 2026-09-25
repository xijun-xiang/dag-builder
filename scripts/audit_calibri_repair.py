"""Replay completed repair contracts offline; never call APIs or execute code.

This verifies provenance and mechanical contracts, not mathematical correctness.
Semantic case review remains necessary before expanding or releasing a dataset.
"""

import argparse
from collections import Counter
from hashlib import sha256
from pathlib import Path

from dag_builder.calibri_normalize import assemble_graph, public_input
from dag_builder.calibri_repair import (
    apply_versioned_repair, assemble_repaired_graph, dependency_data, validate_repair_audit, verify_repair,
)
from dag_builder.config import Config
from dag_builder.response_contract import check_response
from dag_builder.schemas import parse_object, require
from dag_builder.stages import payload
from dag_builder.storage import digest, read_json, write_once


def audit(root):
    config = Config.load(root / "run_config.json")
    items = verify_repair(root, config)
    require(read_json(root / "completion.json")["status"] == "processed", "run has not completed")
    code = read_json(root / "code_origin.json")
    for name, expected in code["source_files"].items():
        require(sha256((root / "controller/code/dag_builder" / name).read_bytes()).hexdigest() == expected,
                "frozen implementation changed")
    calls = reserved = reported = 0
    rows = []
    for item in items:
        directory = root / "items" / item["item_id"]
        seed = read_json(root / "repair-seeds" / (item["item_id"] + ".json"))
        result = read_json(directory / "result.json")
        row = {"item_id": item["item_id"], "question_id": item["question_id"],
               "io_type": item["io_type"], "status": result["status"], "stage": result["stage"],
               "reason": result["reason"], "additions": None, "removals": None}
        data = public_input(item)
        inputs = {"repair": {**data, **seed}}
        normalized = record = graph = None
        if (directory / "repair-record.json").exists():
            normalized, record = apply_versioned_repair(read_json(directory / "repair/output.json"),
                                              seed["previous_normalized"], item, config.prompt_version)
            require(normalized == read_json(directory / "normalization.json")
                    and record == read_json(directory / "repair-record.json"), "repair replay mismatch")
            row.update(additions=sum(c["operation"] == "add_question_premise" for c in record["changes"]),
                       removals=sum(c["operation"] == "remove" for c in record["changes"]),
                       original_nodes=len(seed["previous_normalized"]["nodes"]),
                       repaired_nodes=len(normalized["nodes"]))
            inputs["dependencies"] = dependency_data(data, normalized, record)
        if (directory / "dependencies/output.json").exists():
            graph = assemble_repaired_graph(read_json(directory / "dependencies/output.json"), normalized, item, record)
            inputs["review_dag"] = {**data, "normalized": normalized, "candidate": graph,
                                   "previous_normalized": seed["previous_normalized"], "repair_record": record}
        for stage in ("repair", "dependencies", "review_dag"):
            stage_dir = directory / stage
            if not stage_dir.exists():
                continue
            expected = payload(stage, inputs[stage], config)
            require(read_json(stage_dir / "input.json") == {
                "input": inputs[stage], "payload_sha256": digest(expected)}, "stage input mismatch")
            responses = 0
            for path in stage_dir.glob("attempt-*/request.json"):
                request = read_json(path)
                require(request["payload"] == expected, "request differs from frozen stage payload")
                calls += 1
                reserved += request["reserved_tokens"]
                response_path = path.parent / "response.json"
                if not response_path.exists():
                    require((path.parent / "error.json").exists(), "unresolved remote request")
                    continue
                responses += 1
                body = read_json(response_path)["body"]
                contract = check_response(expected, body, request["reserved_tokens"],
                    strict=config.strict_response_contract, content_gated=config.content_gated_response)
                require(not contract["violations"], "response contract violation")
                reported += contract["reported_tokens"]
                if (stage_dir / "output.json").exists():
                    choices = body["choices"]
                    require(len(choices) == 1 and choices[0]["finish_reason"] == "stop", "non-stop accepted")
                    require(parse_object(choices[0]["message"]["content"]) == read_json(stage_dir / "output.json"),
                            "parsed output differs from raw content")
            require(responses <= 1, "semantic resampling detected")
            if (stage_dir / "output.json").exists():
                require(responses == 1, "accepted parsed output has no raw response")
        if result["status"] == "model_accepted":
            dag = read_json(directory / "dag.json")
            review = read_json(directory / "review_dag/output.json")
            validate_repair_audit(review, config.prompt_version)
            require(review["decision"] == "accept" and dag["dag_review"] == review, "unaccepted semantic audit")
            require(dag["nodes"] == graph["nodes"] and dag["normalization"] == normalized
                    and dag["repair_record"] == record and dag["source"] == item
                    and dag["execution_evidence"] == item["execution_evidence"]
                    and digest(dag) == result["dag_sha256"] and dag["formal_eligible"] is False,
                    "DAG does not match audited pipeline")
        else:
            require(not (directory / "dag.json").exists() and result["dag_sha256"] is None,
                    "failed item published a DAG")
        rows.append(row)
    require(calls <= config.max_calls and reserved <= config.max_reserved_tokens, "allocation exceeded")
    proof = read_json(root / "calibri-normalization-manifest.json")
    return {"protocol": "calibri-repair-offline-audit-v1", "mechanical_pass": True,
            "semantic_certification": False, "git_commit": code["git_commit"],
            "statuses": dict(Counter(r["status"] for r in rows)), "rows": rows,
            "requests": calls, "reserved_tokens": reserved, "reported_tokens": reported,
            "cumulative_requests": proof["prior_calls"] + calls,
            "cumulative_reserved_tokens": proof["prior_reserved_tokens"] + reserved,
            "completion_sha256": digest(read_json(root / "completion.json")),
            "note": "Mechanical replay only; manual semantic case review and release audit still required."}


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.root.resolve())
    write_once(args.root / "offline-audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
