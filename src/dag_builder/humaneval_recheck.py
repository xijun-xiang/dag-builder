"""Offline, read-only replay of two diagnosed-v1 contract failures.

This never resumes a pipeline, calls a model, promotes a candidate, or edits the
source run. Passing a changed local contract is NOT passing semantic review.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path

from .config import Config
from .humaneval_quality import CODE_FACT_VERSION, normalize_sources
from .humaneval_recovery import source_lock
from .humaneval_repair import (
    CONTRACT_PROTOCOL, PROTOCOL, HumanEvalRepairPipeline, diagnosis_input,
    diagnosis_prompt, validate_diagnosis,
)
from .pipeline import implementation
from .response_contract import check_response
from .schemas import InvalidOutput, parse_object, require
from .stages import payload, request_controls, validate
from .storage import digest, read_json, run_lock, write_once

FAILURES = {
    ("atomize", "answer label cannot be a reasoning premise"): "reference_code_fact_contract",
    ("diagnose", "unknown diagnosis evidence source"): "diagnosis_evidence_contract",
}


def _files(root):
    hashes = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "symlinked source evidence")
        if path.is_file():
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _response(directory, config):
    responses = sorted(directory.glob("attempt-*/response.json"))
    require(len(responses) == 1, "need exactly one preserved semantic response")
    response_path = responses[0]
    saved = read_json(response_path.parent / "request.json")
    envelope = read_json(directory / "input.json")
    request = saved["payload"]
    data = envelope["input"]
    require(digest(request) == envelope["payload_sha256"]
            and json.loads(request["messages"][1]["content"]) == data, "request/input mismatch")
    body = read_json(response_path)["body"]
    contract = check_response(request, body, saved["reserved_tokens"],
                              strict=config.strict_response_contract,
                              content_gated=config.content_gated_response)
    require(not contract["violations"], "preserved response violates its API contract")
    choices = body.get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop", "incomplete response")
    parsed = parse_object(choices[0]["message"].get("content"))
    return data, request, parsed, response_path


def recheck_contracts(source_root, output_root):
    """Save an immutable audit in a separate directory; no production outputs."""
    destination = Path(output_root).absolute()
    source_path = Path(source_root).absolute()
    require(destination.resolve() == destination and source_path.resolve() == source_path,
            "symlinked audit paths")
    require(not destination.is_relative_to(source_path) and not source_path.is_relative_to(destination),
            "audit output must be separate from source")
    with source_lock(source_path) as source, run_lock(destination):
        before = _files(source)
        config = Config.load(source / "run_config.json")
        verifier = HumanEvalRepairPipeline(source, config, object())
        require(verifier.protocol == PROTOCOL, "this replay only accepts diagnosed-v1 runs")
        frozen = read_json(source / "inputs_manifest.json")
        require(frozen["items_sha256"] == digest(read_json(source / "items.json"))
                and frozen["selection_sha256"] == digest(read_json(source / "selection.json")),
                "frozen inputs changed")
        rows = []
        for item in read_json(source / "items.json"):
            seed = verifier.seed(item)
            directory = source / "items" / item["item_id"]
            result = read_json(directory / "result.json")
            require(result["item_id"] == item["item_id"], "result identity mismatch")
            require(result["status"] in ("model_accepted", "rejected", "needs_review"),
                    "source contains nonterminal items")
            category = FAILURES.get((result["stage"], result["reason"]))
            if result["status"] != "needs_review" or category is None:
                continue
            stage = result["stage"]
            stage_dir = directory / stage
            require(read_json(stage_dir / "validation.json")["reason"] == result["reason"],
                    "result/validation mismatch")
            require(not (stage_dir / "output.json").exists(), "failed stage already has an output")
            data, request, parsed, response_path = _response(stage_dir, config)
            changes = []
            if stage == "diagnose":
                require(data == diagnosis_input(item, seed), "diagnosis differs from its frozen seed")
                expected_request = dict(request_controls(config), messages=[
                    {"role": "system", "content": diagnosis_prompt()},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False, sort_keys=True)}])
                require(request == expected_request, "original diagnosis request changed")
                old_validate = lambda: validate_diagnosis(parsed, data)
                new_data = diagnosis_input(item, seed, protocol=CONTRACT_PROTOCOL)
                new_validate = lambda: validate_diagnosis(parsed, new_data)
                candidate = parsed
            else:
                require(request == payload(stage, data, config), "original atomization request changed")
                require(data["reference_code"] == item["canonical_solution"]
                        and data["question"]["question"] == item["question"], "atomization source mismatch")
                diagnosis = read_json(directory / "diagnose/output.json")
                validate_diagnosis(diagnosis, diagnosis_input(item, seed))
                solve = (seed["stage_outputs"]["solve"] if diagnosis["route"] == "graph_repair"
                         else read_json(directory / "solve/output.json"))
                require(data["solution"]["rationale"] == solve["rationale"], "atomization explanation mismatch")
                candidate, changes = normalize_sources(parsed)
                old_validate = lambda: validate(stage, candidate, data, prompt_version=config.prompt_version)
                new_data = data
                new_validate = lambda: validate(stage, candidate, data, prompt_version=CODE_FACT_VERSION)
            try:
                old_validate()
            except InvalidOutput as error:
                require(str(error) == result["reason"], "old failure is not reproducible")
            else:
                raise InvalidOutput("old failure unexpectedly passed; do not relabel history")
            try:
                new_validate()
                status, reason = "contract_recheck_passed", None
            except InvalidOutput as error:
                status, reason = "still_invalid", str(error)
            row = {"item_id": item["item_id"], "task_id": item["task_id"], "stage": stage,
                   "category": category, "status": status, "remaining_reason": reason,
                   "original_result": result, "original_result_sha256": digest(result),
                   "response_path": str(response_path.relative_to(source)),
                   "response_file_sha256": before[str(response_path.relative_to(source))],
                   "original_input_sha256": digest(data), "recheck_input_sha256": digest(new_data),
                   "parsed_content_sha256": digest(parsed), "candidate_sha256": digest(candidate),
                   "source_alias_changes": changes, "model_accepted": False,
                   "remaining_work": "Fresh semantic reviews and all uncompleted structural stages; not a release"}
            write_once(destination / "items" / item["item_id"] / "recheck.json", row)
            write_once(destination / "items" / item["item_id"] / "candidate.json", candidate)
            rows.append(row)
        require(_files(source) == before, "source evidence changed during offline replay")
        report = {"protocol": "humaneval-contract-recheck-v1", "source_root": str(source),
                  "source_protocol": PROTOCOL, "target_repair_protocol": CONTRACT_PROTOCOL,
                  "target_construction_protocol": CODE_FACT_VERSION,
                  "implementation": implementation(), "api_calls": 0, "new_model_accepted": 0,
                  "source_unchanged": True, "source_file_count": len(before),
                  "source_file_hashes_sha256": digest(before), "selected": len(rows),
                  "counts": dict(Counter(row["status"] for row in rows)),
                  "categories": dict(Counter(row["category"] for row in rows)), "items": rows,
                  "interpretation": "Retrospective checks of unchanged v1 responses, not new v2/v5 model outputs or accepted DAGs"}
        write_once(destination / "source_file_hashes.json", before)
        write_once(destination / "report.json", report)
        return {key: report[key] for key in ("selected", "counts", "categories", "api_calls",
                                            "new_model_accepted", "source_unchanged")}
