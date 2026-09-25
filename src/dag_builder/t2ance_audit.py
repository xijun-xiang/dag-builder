"""Offline audit of a completed, CPU-gated t2ance LiveCodeBench DAG canary."""

import hashlib
from collections import Counter
from pathlib import Path

from .calibri_continuation import _replay_recorded_calls
from .calibri_normalize import assemble_graph, normalize, public_input, validate_audit
from .calibri_repair_transport import _failed_stage, _stage_output
from .config import Config
from .response_contract import check_response
from .schemas import parse_object, require
from .stages import payload
from .storage import digest, read_json, write_once
from .t2ance_pipeline import PROTOCOLS, verify_prepared
from .t2ance_v3 import (PROTOCOL as PROTOCOL_V3, assemble_graph_v3,
                         normalize_v3, validate_audit_v3)
from .t2ance_v4 import (PROTOCOL as PROTOCOL_V4, assemble_graph_v4,
                         normalize_v4, validate_audit_v4)


def _answer_backward_functions(version):
    require(version in (PROTOCOL_V3, PROTOCOL_V4),
            "unknown answer-backward audit protocol")
    return ((normalize_v4, assemble_graph_v4, validate_audit_v4)
            if version == PROTOCOL_V4 else
            (normalize_v3, assemble_graph_v3, validate_audit_v3))


def _replay_v3_recorded_calls(root, config, items):
    """Replay v3 requests without invoking the older CALIBRI validator."""
    normalizer, assembler, reviewer = _answer_backward_functions(config.prompt_version)
    indexed = {item["item_id"]: item for item in items}
    for path in sorted(root.glob("items/*/*/attempt-*/request.json")):
        stage, item_id = path.parts[-3], path.parts[-4]
        require(stage in ("normalize", "dependencies", "review_dag")
                and item_id in indexed, "unknown v3 recorded stage/item")
        input_record = read_json(path.parent.parent / "input.json")
        expected = payload(stage, input_record["input"], config)
        request = read_json(path)
        require(request["payload"] == expected
                and input_record["payload_sha256"] == digest(expected),
                "v3 recorded request differs from frozen input")
        response_path, error_path = path.parent / "response.json", path.parent / "error.json"
        require(response_path.exists() != error_path.exists(),
                "v3 completed run has ambiguous or missing response")
        if error_path.exists():
            continue
        body = read_json(response_path)["body"]
        contract = check_response(expected, body, request["reserved_tokens"],
                                  strict=config.strict_response_contract,
                                  content_gated=config.content_gated_response)
        require(not contract["violations"], "v3 response contract changed")
        output_path = path.parent.parent / "output.json"
        if not output_path.exists():
            continue
        choices = body.get("choices", [])
        require(len(choices) == 1 and choices[0]["finish_reason"] == "stop",
                "v3 non-stop recorded output")
        output = parse_object(choices[0]["message"].get("content"))
        require(output == read_json(output_path), "v3 parsed output changed")
        if stage == "normalize":
            normalizer(output, indexed[item_id])
        elif stage == "dependencies":
            normalized = read_json(path.parent.parent.parent / "normalization.json")
            assembler(output, normalized, indexed[item_id])
        else:
            reviewer(output)


def _replay_v3_terminal_item(root, item, config):
    """Derive every terminal label and accepted graph from returned content."""
    normalize_fn, assemble_fn, review_fn = _answer_backward_functions(config.prompt_version)
    directory = root / "items" / item["item_id"]
    result = read_json(directory / "result.json")
    require(result["item_id"] == item["item_id"]
            and result["status"] in ("model_accepted", "needs_review", "rejected")
            and result["stage"] in ("normalize", "dependencies", "review_dag"),
            "invalid v3 terminal result")
    data = public_input(item)
    normalizer = lambda value: normalize_fn(value, item)
    failed = (result["status"] == "needs_review"
              and (directory / result["stage"] / "validation.json").exists())
    if failed and result["stage"] == "normalize":
        reason = _failed_stage(directory, "normalize", data, config, normalizer)
        require(not (directory / "normalization.json").exists()
                and not (directory / "dependencies").exists()
                and not (directory / "review_dag").exists(),
                "v3 stage exists after failed normalization")
    else:
        proposal = _stage_output(directory, "normalize", data, config, normalizer)
        require(proposal is not None, "v3 terminal has no normalized response")
        normalized = normalizer(proposal)
        require(read_json(directory / "normalization.json") == normalized,
                "v3 normalization differs from returned content")
        dependency_data = {**data, "normalized": normalized}
        graph_builder = lambda value: assemble_fn(value, normalized, item)
        if failed and result["stage"] == "dependencies":
            reason = _failed_stage(directory, "dependencies", dependency_data,
                                   config, graph_builder)
            require(not (directory / "review_dag").exists(),
                    "v3 review exists after failed dependencies")
        else:
            dependencies = _stage_output(directory, "dependencies", dependency_data,
                                         config, graph_builder)
            require(dependencies is not None, "v3 terminal has no dependency response")
            graph = graph_builder(dependencies)
            review_data = {**data, "normalized": normalized, "candidate": graph}
            if failed and result["stage"] == "review_dag":
                reason = _failed_stage(directory, "review_dag", review_data,
                                       config, review_fn)
            else:
                review = _stage_output(directory, "review_dag", review_data,
                                       config, review_fn)
                require(review is not None, "v3 terminal has no review response")
                expected_status = {"accept": "model_accepted", "reject": "rejected",
                                   "needs_review": "needs_review"}[review["decision"]]
                require(result["status"] == expected_status
                        and result["stage"] == "review_dag", "v3 verdict changed")
                reason = ("pending release audit" if expected_status == "model_accepted"
                          else review["reason"])
                if expected_status == "model_accepted":
                    dag = read_json(directory / "dag.json")
                    expected_dag = {
                        "schema_version": "reference_dag_v1",
                        "construction_protocol": config.prompt_version,
                        "item_id": item["item_id"], "source": item,
                        "nodes": graph["nodes"], "normalization": normalized,
                        "graph_transformation": graph["transformation"],
                        "dag_review": review,
                        "execution_evidence": item["execution_evidence"],
                        "calculation_check": {"status": "reference_tests_passed"},
                        "formal_eligible": False,
                        "quality_status": "model_reviewed_pending_release_audit",
                        "limitation": (
                            "t2ance-derived and answer-backward canonicalized explanation; "
                            "frozen code passed tests; same-model semantic review, "
                            "not native CoT or official/human gold"),
                    }
                    require(dag == expected_dag and result["dag_sha256"] == digest(dag),
                            "v3 accepted DAG contradicts raw stage outputs")
    require(result["reason"] == reason, "v3 terminal reason changed")
    if result["status"] != "model_accepted":
        require(result["dag_sha256"] is None and not (directory / "dag.json").exists(),
                "v3 failed item published a DAG")
    return result


def audit_completed(root):
    root = Path(root)
    config = Config.load(root / "config.json")
    require(config.prompt_version in PROTOCOLS, "not a t2ance DAG run")
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
    (_replay_v3_recorded_calls(root, config, items)
     if config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4) else
     _replay_recorded_calls(root, config))
    normalizer = (normalize_v4 if config.prompt_version == PROTOCOL_V4 else normalize_v3)
    assembler = (assemble_graph_v4 if config.prompt_version == PROTOCOL_V4 else assemble_graph_v3)
    reviewer = (validate_audit_v4 if config.prompt_version == PROTOCOL_V4 else validate_audit_v3)
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
        if config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4):
            _replay_v3_terminal_item(root, item, config)
        require(result["item_id"] == item_id
                and result["status"] in ("model_accepted", "needs_review", "rejected"),
                "t2ance result missing or invalid")
        normalized, graph = None, None
        normalization_output = directory / "normalize/output.json"
        normalization_path = directory / "normalization.json"
        if normalization_output.exists():
            normalized = (normalizer(read_json(normalization_output), item)
                          if config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4) else
                          normalize(read_json(normalization_output), item,
                                    prompt_version=config.prompt_version))
            require(normalization_path.is_file()
                    and read_json(normalization_path) == normalized,
                    "t2ance normalization differs from recorded response")
        else:
            require(not normalization_path.exists(), "orphaned t2ance normalization")
        dependencies_output = directory / "dependencies/output.json"
        review_input = directory / "review_dag/input.json"
        if dependencies_output.exists():
            require(normalized is not None and review_input.is_file(),
                    "t2ance graph lacks normalization or review input")
            graph = (assembler(read_json(dependencies_output), normalized, item)
                     if config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4) else
                     assemble_graph(read_json(dependencies_output), normalized, item))
            require(read_json(review_input)["input"]["candidate"] == graph,
                    "t2ance review candidate differs from recorded dependencies")
        else:
            require(not review_input.exists(), "orphaned t2ance review input")
        statuses[result["status"]] += 1
        if result["status"] == "model_accepted":
            dag = read_json(directory / "dag.json")
            require(result["dag_sha256"] == digest(dag)
                    and dag["item_id"] == item_id and dag["source"] == item
                    and dag["construction_protocol"] == config.prompt_version
                    and graph is not None and dag["nodes"] == graph["nodes"]
                    and (config.prompt_version not in (PROTOCOL_V3, PROTOCOL_V4) or
                         dag.get("graph_transformation") == graph["transformation"])
                    and dag["normalization"] == normalized
                    and dag["dag_review"] == read_json(directory / "review_dag/output.json")
                    and dag["formal_eligible"] is False,
                    "t2ance accepted graph/source mismatch")
            (reviewer if config.prompt_version in (PROTOCOL_V3, PROTOCOL_V4)
             else validate_audit)(dag["dag_review"])
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
