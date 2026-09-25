"""Versioned continuation of a *partially* paused checked CALIBRI repair.

Only items with an evidenced operational pause enter the new run. The complete
old run is frozen separately, including terminal verdicts, failed HTTP attempts
and uncertain requests. A returned, valid stage response is copied verbatim and
replayed by the normal pipeline; a failed transport stage starts a fresh attempt
in the new run. Neither a semantic failure nor a returned invalid response is
eligible for resampling.
"""

from hashlib import sha256
from pathlib import Path

from .calibri_normalize import public_input
from .calibri_pipeline import CAMPAIGN_CALL_LIMIT, verify_prepared
from .calibri_repair import (
    CHECKED_PROTOCOL, DNS_RECOVERY_PROTOCOL, _used_budget, apply_versioned_repair,
    assemble_repaired_graph, dependency_data, validate_repair_audit,
)
from .config import Config
from .response_contract import check_response
from .run_status import paused_items
from .schemas import InvalidOutput, parse_object, require
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "calibri-lcb-repair-partial-transport-v1"
# An implementation guard, not an authorization to spend this amount. Preparation
# requires an explicit per-run campaign ceiling and a separate new-call allocation.
MAX_CAMPAIGN_TOKEN_LIMIT = 64_000_000
STAGES = ("repair", "dependencies", "review_dag")


def _source_files(source):
    for path in sorted(source.rglob("*")):
        require(not path.is_symlink(), "symlinked source evidence")
        if path.is_file() and path.name != ".lock":
            yield path


def _stage_attempts(stage_dir, expected, config):
    """Replay the exact request and contract of every historical attempt."""
    requests = sorted(stage_dir.glob("attempt-*/request.json"))
    require({path.parent for path in requests} == set(stage_dir.glob("attempt-*")),
            "unpaired historical attempt directory")
    responses = []
    for path in requests:
        require(path.parent.name.startswith("attempt-") and path.parent.name[8:].isdigit(),
                "invalid historical attempt name")
        request = read_json(path)
        require(request["payload"] == expected and type(request["reserved_tokens"]) is int
                and request["reserved_tokens"] > 0, "historical request changed")
        response = path.parent / "response.json"
        error = path.parent / "error.json"
        require(response.exists() != error.exists(), "ambiguous or unrecorded historical outcome")
        if response.exists():
            body = read_json(response)["body"]
            contract = check_response(expected, body, request["reserved_tokens"],
                strict=config.strict_response_contract,
                content_gated=config.content_gated_response)
            require(not contract["violations"], "historical response contract violation")
            responses.append(body)
        else:
            failure = read_json(error)
            require(failure["category"] in ("uncertain_remote_state", "rate_limit"),
                    "non-transport historical failure cannot be retried")
    require(len(responses) <= 1, "semantic response was resampled")
    return requests, responses


def _stage_output(directory, stage, data, config, validator):
    """Validate every recorded attempt and replay one returned semantic output."""
    stage_dir = directory / stage
    if not stage_dir.exists():
        return None
    expected = payload(stage, data, config)
    require(read_json(stage_dir / "input.json") == {
        "input": data, "payload_sha256": digest(expected)}, "source stage input changed")
    requests, responses = _stage_attempts(stage_dir, expected, config)
    # A worker stopped by the shared gate may persist input.json but reserve
    # no attempt. That is a missing stage, not an API outcome to resample.
    if not requests:
        require(not (stage_dir / "output.json").exists()
                and not (stage_dir / "validation.json").exists(),
                "unrecorded source stage outcome")
        return None
    output = stage_dir / "output.json"
    if not output.exists():
        # A returned response without an output is a semantic/parse/truncation
        # outcome, never a transport retry candidate.
        require(not responses and not (stage_dir / "validation.json").exists(),
                "returned or invalid stage cannot be transport-resumed")
        return None
    require(len(responses) == 1 and not (stage_dir / "validation.json").exists(),
            "completed stage has ambiguous raw evidence")
    choices = responses[0].get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop",
            "completed stage has non-stop response")
    parsed = parse_object(choices[0].get("message", {}).get("content"))
    require(parsed == read_json(output), "parsed stage differs from raw content")
    validator(parsed)
    return parsed


def _failed_stage(directory, stage, data, config, validator):
    """Reproduce a scientific needs-review verdict from its raw content."""
    stage_dir = directory / stage
    require(stage_dir.is_dir(), "terminal failed stage is missing")
    expected = payload(stage, data, config)
    require(read_json(stage_dir / "input.json") == {
        "input": data, "payload_sha256": digest(expected)}, "terminal stage input changed")
    requests, responses = _stage_attempts(stage_dir, expected, config)
    require(bool(requests) and len(responses) == 1
            and not (stage_dir / "output.json").exists(),
            "terminal validation lacks one raw semantic response")
    try:
        choices = responses[0].get("choices", [])
        if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
            raise InvalidOutput("missing, ambiguous or non-stop completion; no truncated text is accepted")
        message = choices[0].get("message", {})
        require(isinstance(message, dict), "invalid assistant message")
        validator(parse_object(message.get("content")))
    except (InvalidOutput, TypeError, KeyError, AttributeError) as error:
        reason = str(error) if isinstance(error, InvalidOutput) else "invalid response schema"
    else:
        raise InvalidOutput("terminal failure does not reproduce from raw content")
    require(read_json(stage_dir / "validation.json") == {
        "status": "needs_review", "reason": reason},
        "terminal validation differs from raw content")
    return reason


def replay_terminal_item(source, item, config):
    """Reconstruct a checked-repair terminal without trusting its labels.

    Run on the old source before copying, its frozen copy before any new
    request, and every continued item before a merged scientific summary.
    """
    directory = source / "items" / item["item_id"]
    result = read_json(directory / "result.json")
    require(result["item_id"] == item["item_id"]
            and result["status"] in ("model_accepted", "needs_review", "rejected")
            and result["stage"] in STAGES,
            "invalid historical terminal result")
    seed = read_json(source / "repair-seeds" / (item["item_id"] + ".json"))
    data = public_input(item)
    repair_data = {**data, **seed}
    repair_validator = lambda value: apply_versioned_repair(
        value, seed["previous_normalized"], item, CHECKED_PROTOCOL)
    failed = result["status"] == "needs_review" and (
        directory / result["stage"] / "validation.json").exists()
    if failed and result["stage"] == "repair":
        reason = _failed_stage(directory, "repair", repair_data, config, repair_validator)
        require(not (directory / "normalization.json").exists()
                and not (directory / "repair-record.json").exists()
                and not (directory / "dependencies").exists()
                and not (directory / "review_dag").exists(),
                "later stage exists after rejected repair")
    else:
        repair = _stage_output(directory, "repair", repair_data, config, repair_validator)
        require(repair is not None, "terminal repair has no returned output")
        normalized, record = repair_validator(repair)
        require(read_json(directory / "normalization.json") == normalized
                and read_json(directory / "repair-record.json") == record,
                "terminal repair mapping differs from raw response")
        deps_data = dependency_data(data, normalized, record)
        deps_validator = lambda value: assemble_repaired_graph(value, normalized, item, record)
        if failed and result["stage"] == "dependencies":
            reason = _failed_stage(directory, "dependencies", deps_data, config, deps_validator)
            require(not (directory / "review_dag").exists(),
                    "review exists after rejected dependencies")
        else:
            deps = _stage_output(directory, "dependencies", deps_data, config, deps_validator)
            require(deps is not None, "terminal dependencies have no returned output")
            graph = deps_validator(deps)
            review_data = {**data, "normalized": normalized, "candidate": graph,
                           "previous_normalized": seed["previous_normalized"],
                           "repair_record": record}
            review_validator = lambda value: validate_repair_audit(value, CHECKED_PROTOCOL)
            if failed and result["stage"] == "review_dag":
                reason = _failed_stage(directory, "review_dag", review_data,
                                       config, review_validator)
            else:
                review = _stage_output(directory, "review_dag", review_data,
                                       config, review_validator)
                require(review is not None, "terminal review has no returned output")
                expected_status = {"accept": "model_accepted", "reject": "rejected",
                                   "needs_review": "needs_review"}[review["decision"]]
                require(result["status"] == expected_status
                        and result["stage"] == "review_dag", "terminal review verdict changed")
                reason = ("pending release audit" if expected_status == "model_accepted"
                          else review["reason"])
                if expected_status == "model_accepted":
                    dag = read_json(directory / "dag.json")
                    recovery = {"parent_seed_sha256": digest(seed), "repair_round": 1,
                                "repair_mode": "checked_first_pass"}
                    if "transport_recovery" in read_json(source / "calibri-repair-manifest.json"):
                        recovery["transport_recovery_protocol"] = DNS_RECOVERY_PROTOCOL
                    continuation = read_json(source / "calibri-repair-manifest.json").get(
                        "partial_transport_continuation")
                    if continuation is not None:
                        recovery["partial_transport_continuation_protocol"] = continuation["protocol"]
                    expected_dag = {
                        "schema_version": "reference_dag_v1",
                        "construction_protocol": CHECKED_PROTOCOL,
                        "item_id": item["item_id"], "source": item,
                        "nodes": graph["nodes"], "normalization": normalized,
                        "repair_record": record, "recovery_provenance": recovery,
                        "dag_review": review,
                        "execution_evidence": item["execution_evidence"],
                        "calculation_check": {"status": "reference_tests_passed"},
                        "formal_eligible": False,
                        "quality_status": "model_reviewed_pending_release_audit",
                        "limitation": "Source-bound repaired explanation; unchanged tested code; same-model review, not native CoT or official/human gold",
                    }
                    require(dag == expected_dag
                            and result["dag_sha256"] == digest(dag),
                            "historical accepted DAG contradicts raw stage outputs")
    require(result["reason"] == reason, "terminal reason contradicts raw stage output")
    if result["status"] != "model_accepted":
        require(result["dag_sha256"] is None
                and not (directory / "dag.json").exists(),
                "failed historical item published a DAG")
    return {"item_id": item["item_id"], "question_id": item["question_id"],
            "io_type": item["io_type"], "status": result["status"],
            "stage": result["stage"], "reason": result["reason"],
            "dag_sha256": result["dag_sha256"]}


def _completed_prefix(source, item, config):
    """Return the first stage needing a call, after replaying valid raw outputs."""
    directory = source / "items" / item["item_id"]
    seed = read_json(source / "repair-seeds" / (item["item_id"] + ".json"))
    data = public_input(item)
    repair_data = {**data, **seed}
    repair = _stage_output(directory, "repair", repair_data, config,
        lambda value: apply_versioned_repair(value, seed["previous_normalized"], item, CHECKED_PROTOCOL))
    if repair is None:
        require(not (directory / "normalization.json").exists()
                and not (directory / "repair-record.json").exists()
                and not (directory / "dependencies").exists()
                and not (directory / "review_dag").exists(), "later stage without a repair response")
        return "repair"
    normalized, record = apply_versioned_repair(
        repair, seed["previous_normalized"], item, CHECKED_PROTOCOL)
    require(read_json(directory / "normalization.json") == normalized
            and read_json(directory / "repair-record.json") == record,
            "repaired normalization or mapping changed")
    dependencies = _stage_output(directory, "dependencies",
        dependency_data(data, normalized, record), config,
        lambda value: assemble_repaired_graph(value, normalized, item, record))
    if dependencies is None:
        require(not (directory / "review_dag").exists(), "review without dependencies")
        return "dependencies"
    graph = assemble_repaired_graph(dependencies, normalized, item, record)
    audit_data = {**data, "normalized": normalized, "candidate": graph,
                  "previous_normalized": seed["previous_normalized"], "repair_record": record}
    review = _stage_output(directory, "review_dag", audit_data, config,
                           lambda value: validate_repair_audit(value, CHECKED_PROTOCOL))
    return "review_dag" if review is None else "finalize_cached_review"


def _source_state(source):
    from .calibri_repair import verify_repair

    config = Config.load(source / "run_config.json")
    require(config.prompt_version == CHECKED_PROTOCOL,
            "partial continuation requires checked repair")
    manifest = read_json(source / "calibri-repair-manifest.json")
    require(manifest.get("repair_mode") == "checked_first_pass"
            and "partial_transport_continuation" not in manifest,
            "cannot chain or revise a partial continuation")
    require(read_json(source / "completion.json")["status"] == "paused",
            "source is not a completed operational pause")
    items = verify_repair(source, config)
    pauses = paused_items(source)
    require(bool(pauses), "source has no evidenced pauses")
    by_id = {item["item_id"]: item for item in items}
    require(set(pauses) <= set(by_id), "pause outside frozen cohort")
    terminal = []
    for item in items:
        item_id = item["item_id"]
        result = source / "items" / item_id / "result.json"
        if item_id in pauses:
            require(not result.exists() and pauses[item_id]["status"] == "paused",
                    "paused item already has a terminal verdict")
        else:
            require(result.is_file(), "unclassified source item")
            replay_terminal_item(source, item, config)
            terminal.append(item_id)
    actions = {item_id: _completed_prefix(source, by_id[item_id], config)
               for item_id in pauses}
    for item_id, action in actions.items():
        require(pauses[item_id]["stage"] == (
            "review_dag" if action == "finalize_cached_review" else action),
            "pause stage does not match raw response prefix")
    return config, items, pauses, terminal, actions


def _imported_stage_files(source, selected, actions):
    """Only returned-and-validated prefix stages become active cached calls."""
    for item in selected:
        item_id = item["item_id"]
        end = STAGES.index(actions[item_id]) if actions[item_id] in STAGES else len(STAGES)
        directory = source / "items" / item_id
        for stage in STAGES[:end]:
            for path in sorted((directory / stage).rglob("*")):
                require(not path.is_symlink(), "symlinked stage evidence")
                if path.is_file():
                    yield path
        if end >= 1:
            yield directory / "normalization.json"
            yield directory / "repair-record.json"


def _account_imported(files, config):
    calls = tokens = 0
    for path in files:
        if path.name != "request.json":
            continue
        request = read_json(path)
        account = request["reserved_tokens"]
        if (path.parent / "response.json").exists():
            check = check_response(request["payload"],
                read_json(path.parent / "response.json")["body"], account,
                strict=config.strict_response_contract,
                content_gated=config.content_gated_response)
            require(not check["violations"], "imported response violates contract")
            account = check["accounted_tokens"]
        calls += 1
        tokens += account
    return calls, tokens


def prepare_partial_transport(source, root, *, max_new_calls, max_new_tokens,
                              campaign_token_limit):
    """Freeze a paused run into a new private run; no API call occurs here."""
    source, root = Path(source).resolve(), private_dir(root)
    require(source != root and not root.is_relative_to(source), "new sibling run required")
    require(not any(path.name != ".lock" for path in root.iterdir()),
            "continuation root must be empty")
    require(all(type(value) is int and value > 0 for value in (
        max_new_calls, max_new_tokens, campaign_token_limit)), "explicit positive budget required")
    require(campaign_token_limit <= MAX_CAMPAIGN_TOKEN_LIMIT,
            "campaign ceiling exceeds implementation guard")
    source_config, original, pauses, terminal, actions = _source_state(source)
    selected = [item for item in original if item["item_id"] in pauses]
    imported_paths = list(_imported_stage_files(source, selected, actions))
    imported_calls, imported_tokens = _account_imported(imported_paths, source_config)
    source_calls, source_tokens = _used_budget(source, source_config)
    require(imported_calls <= source_calls and imported_tokens <= source_tokens,
            "imported calls exceed source ledger")
    require(source_calls + max_new_calls <= CAMPAIGN_CALL_LIMIT
            and source_tokens + max_new_tokens <= campaign_token_limit,
            "explicit campaign allocation exceeded")
    config = Config(**{**source_config.to_dict(),
                       "max_calls": imported_calls + max_new_calls,
                       "max_reserved_tokens": imported_tokens + max_new_tokens})
    selection = {"selected_ids": [i["item_id"] for i in selected],
                 "selected_count": len(selected), "source_count": len(original),
                 "terminal_ids": terminal,
                 "sampling": "all evidenced operational pauses; no terminal or semantic resampling"}
    old_proof = read_json(source / "calibri-normalization-manifest.json")
    proof = {**old_proof, "items_sha256": digest(selected),
             "selection_sha256": digest(selection),
             "prior_calls": source_calls - imported_calls,
             "prior_reserved_tokens": source_tokens - imported_tokens,
             "max_calls": config.max_calls,
             "max_reserved_tokens": config.max_reserved_tokens,
             "campaign_token_limit": campaign_token_limit}
    old_manifest = read_json(source / "calibri-repair-manifest.json")
    source_hashes = {}
    for path in _source_files(source):
        name = str(path.relative_to(source))
        content = path.read_bytes()
        source_hashes[name] = sha256(content).hexdigest()
        write_bytes_once(root / "source-run-evidence" / name, content)
    # Recheck against the live source after the copy: a concurrent writer must
    # never produce a mixed snapshot that we later call frozen provenance.
    require({str(path.relative_to(source)): sha256(path.read_bytes()).hexdigest()
             for path in _source_files(source)} == source_hashes,
            "source changed while being frozen")
    frozen = root / "source-run-evidence"
    for folder in ("evidence", "parent-evidence"):
        for path in sorted((frozen / folder).rglob("*")):
            if path.is_file():
                write_bytes_once(root / path.relative_to(frozen), path.read_bytes())
    for item in selected:
        name = item["item_id"] + ".json"
        write_bytes_once(root / "repair-seeds" / name,
                         (frozen / "repair-seeds" / name).read_bytes())
    active_hashes = {}
    for path in imported_paths:
        name = str(path.relative_to(source))
        content = (frozen / name).read_bytes()
        active_hashes[name] = sha256(content).hexdigest()
        write_bytes_once(root / name, content)
    continuation = {
        "protocol": PROTOCOL, "source_run": str(source),
        "source_files": source_hashes, "active_imports": active_hashes,
        "source_selected_ids": [i["item_id"] for i in original],
        "paused_selected_ids": selection["selected_ids"],
        "terminal_ids": terminal, "actions": actions,
        "source_calls": source_calls, "source_accounted_tokens": source_tokens,
        "imported_calls": imported_calls, "imported_accounted_tokens": imported_tokens,
        "max_new_calls": max_new_calls, "max_new_tokens": max_new_tokens,
        "campaign_token_limit": campaign_token_limit,
        "source_completion_sha256": source_hashes["completion.json"],
        "source_manifest_sha256": source_hashes["calibri-repair-manifest.json"],
        "policy": "transport-only continuation; prior terminal and failed attempts frozen, returned outputs reused"}
    manifest = {**old_manifest,
                "seed_sha256": {i["item_id"]: old_manifest["seed_sha256"][i["item_id"]]
                                for i in selected},
                "items_sha256": digest(selected),
                "selection_sha256": digest(selection),
                "normalization_manifest_sha256": digest(proof),
                "partial_transport_continuation": continuation}
    manifest.pop("transport_recovery", None)
    for name, value in (("items.json", selected), ("selection.json", selection),
                        ("config.json", config.to_dict()),
                        ("calibri-normalization-manifest.json", proof),
                        ("calibri-repair-manifest.json", manifest)):
        write_once(root / name, value)
    return {"selected_count": len(selected), "terminal_count": len(terminal),
            "actions": actions, "source_calls": source_calls,
            "source_accounted_tokens": source_tokens,
            "imported_calls": imported_calls,
            "new_call_allocation": max_new_calls,
            "new_token_allocation": max_new_tokens}


def verify_partial_transport(root, config):
    """Fail closed before every paid request, including a resumed worker."""
    root = Path(root).resolve()
    manifest = read_json(root / "calibri-repair-manifest.json")
    continuation = manifest["partial_transport_continuation"]
    require(continuation["protocol"] == PROTOCOL and config.prompt_version == CHECKED_PROTOCOL
            and manifest["protocol"] == CHECKED_PROTOCOL
            and manifest["max_repair_rounds"] == 1
            and manifest.get("repair_mode") == "checked_first_pass"
            and "transport_recovery" not in manifest,
            "invalid partial-transport protocol")
    require(Config.load(root / "config.json") == config,
            "continuation config changed")
    for name, expected in continuation["source_files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts,
                "unsafe source evidence path")
        path = root / "source-run-evidence" / name
        require(path.is_file() and not path.is_symlink()
                and sha256(path.read_bytes()).hexdigest() == expected,
                "source evidence changed")
    source = root / "source-run-evidence"
    require({str(path.relative_to(source)): sha256(path.read_bytes()).hexdigest()
             for path in _source_files(source)} == continuation["source_files"],
            "source evidence inventory changed")
    source_config, original, pauses, terminal, actions = _source_state(source)
    require(continuation["source_manifest_sha256"] == continuation["source_files"]["calibri-repair-manifest.json"]
            and continuation["source_completion_sha256"] == continuation["source_files"]["completion.json"],
            "source manifest or completion binding changed")
    items = verify_prepared(root, config,
        campaign_token_limit=continuation["campaign_token_limit"])
    selected = [item for item in original if item["item_id"] in pauses]
    selection = read_json(root / "selection.json")
    require(items == selected and selection["selected_ids"] == continuation["paused_selected_ids"]
            and selection["terminal_ids"] == terminal
            and continuation["source_selected_ids"] == [i["item_id"] for i in original]
            and continuation["actions"] == actions,
            "continued cohort or stage actions changed")
    require(digest(items) == manifest["items_sha256"]
            and digest(selection) == manifest["selection_sha256"]
            and digest(read_json(root / "calibri-normalization-manifest.json"))
                == manifest["normalization_manifest_sha256"],
            "repair continuation manifest changed")
    for name, expected in manifest["parent_files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts
                and sha256((root / "parent-evidence" / name).read_bytes()).hexdigest() == expected,
                "normalization parent evidence changed")
    for item in items:
        item_id = item["item_id"]
        seed = read_json(root / "repair-seeds" / (item_id + ".json"))
        require(digest(seed) == manifest["seed_sha256"][item_id]
                and seed == read_json(source / "repair-seeds" / (item_id + ".json")),
                "repair seed changed")
    expected_imports = {str(path.relative_to(source)): sha256(path.read_bytes()).hexdigest()
                        for path in _imported_stage_files(source, selected, actions)}
    require(expected_imports == continuation["active_imports"],
            "imported stage selection changed")
    for name, expected in expected_imports.items():
        path = root / name
        require(path.is_file() and not path.is_symlink()
                and sha256(path.read_bytes()).hexdigest() == expected,
                "cached stage response or output changed")
    source_calls, source_tokens = _used_budget(source, source_config)
    imported_calls, imported_tokens = _account_imported(
        list(_imported_stage_files(source, selected, actions)), source_config)
    proof = read_json(root / "calibri-normalization-manifest.json")
    require((source_calls, source_tokens, imported_calls, imported_tokens) == (
            continuation["source_calls"], continuation["source_accounted_tokens"],
            continuation["imported_calls"], continuation["imported_accounted_tokens"])
            and (proof["prior_calls"], proof["prior_reserved_tokens"]) == (
                source_calls - imported_calls, source_tokens - imported_tokens)
            and (config.max_calls, config.max_reserved_tokens) == (
                imported_calls + continuation["max_new_calls"],
                imported_tokens + continuation["max_new_tokens"])
            and source_calls + continuation["max_new_calls"] <= CAMPAIGN_CALL_LIMIT
            and source_tokens + continuation["max_new_tokens"]
                <= continuation["campaign_token_limit"] <= MAX_CAMPAIGN_TOKEN_LIMIT,
            "partial continuation ledger or allocation changed")
    return items


def main():
    import argparse
    import json
    import os
    from .storage import run_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-new-calls", type=int, required=True)
    parser.add_argument("--max-new-tokens", type=int, required=True)
    parser.add_argument("--campaign-token-limit", type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare_partial_transport(args.source, args.root,
            max_new_calls=args.max_new_calls,
            max_new_tokens=args.max_new_tokens,
            campaign_token_limit=args.campaign_token_limit)))


if __name__ == "__main__":
    main()
