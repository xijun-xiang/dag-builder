"""One source-bound repair pass over failed CALIBRI dependency proposals.

This is a separate immutable run, not a retry-until-accepted loop. Existing
statements and their order cannot change. Only explicit question premises may
be inserted; omissions and every ID mapping remain auditable. Edges are then
recomputed and the complete candidate receives a fresh-context semantic audit.
"""

from hashlib import sha256
from pathlib import Path

from .calibri_normalize import (
    assemble_graph, normalize, public_input, source_units, validate_audit,
)
from .calibri_pipeline import (
    CAMPAIGN_CALL_LIMIT, CAMPAIGN_TOKEN_LIMIT, verify_prepared,
)
from .client import CallFailure
from .config import Config
from .pipeline import Pipeline
from .response_contract import check_response
from .schemas import InvalidOutput, parse_object, require, text
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "calibri-lcb-repair-v1"
CHECKED_PROTOCOL = "calibri-lcb-repair-v2"
DNS_RECOVERY_PROTOCOL = "calibri-lcb-dns-transport-recovery-v1"
DNS_RECOVERY_TOKEN_LIMIT = 32_000_000
STEP_FIELDS = ("kind", "statement", "source_refs", "support_type", "normalization_note")
REPAIR_CHECKS = (
    "added_premises_explicit_in_question", "removed_nodes_not_necessary",
    "retained_statements_unchanged", "no_fabricated_closure_edges",
    "all_required_question_premises_explicit",
)


class NoDependencyProposal(ValueError):
    """A recorded dependency response failed parsing; there is no graph to repair."""


def apply_repair(value, previous, item):
    """Apply a narrow edit contract. Semantic validity remains a separate gate."""
    require(isinstance(value, dict) and set(value) == {"additions", "removals", "reason"}
            and text(value["reason"]), "invalid repair proposal")
    additions, removals = value["additions"], value["removals"]
    nodes = previous["nodes"]
    original = {"steps": [{k: n[k] for k in STEP_FIELDS} for n in nodes],
                "omissions": previous["omissions"]}
    require(normalize(original, item) == previous, "previous normalization is not source-bound")
    require(isinstance(additions, list) and len(additions) <= 8, "at most eight explicit premise additions")
    require(isinstance(removals, list) and len(removals) < len(nodes), "cannot remove every node")
    ids = {n["node_id"] for n in nodes}
    removed = {}
    for row in removals:
        require(isinstance(row, dict) and set(row) == {"node_id", "reason"}
                and type(row["node_id"]) is int and row["node_id"] in ids
                and row["node_id"] not in removed and text(row["reason"]), "invalid/duplicate removal")
        removed[row["node_id"]] = row["reason"]
    units = {u["unit_id"]: u for u in source_units(item)}
    inserted = {}
    for row in additions:
        require(isinstance(row, dict) and set(row) == {
            "before_node_id", "statement", "source_refs", "reason"}, "invalid premise addition")
        position, refs = row["before_node_id"], row["source_refs"]
        require(type(position) is int and position in ids - removed.keys(), "insert before a retained node")
        require(text(row["statement"]) and text(row["reason"]), "premise and reason required")
        require(isinstance(refs, list) and bool(refs) and all(
            isinstance(r, str) and r in units and units[r]["source_field"] == "question" for r in refs),
            "new premises must cite original question units only")
        inserted.setdefault(position, []).append(row)
    steps, mapping, changes = [], [], []
    omissions = list(previous["omissions"])
    for node in nodes:
        old_id = node["node_id"]
        if old_id in removed:
            mapping.append({"old_node_id": old_id, "new_node_id": None})
            omissions.append({"source_refs": node["source_refs"],
                              "reason": "Repair omission: " + removed[old_id]})
            changes.append({"operation": "remove", "old_node_id": old_id,
                            "source_refs": node["source_refs"], "reason": removed[old_id]})
            continue
        for added in inserted.get(old_id, []):
            steps.append({"kind": "given", "statement": added["statement"],
                          "source_refs": added["source_refs"], "support_type": "source_supported",
                          "normalization_note": "Repair: restore explicit question premise. " + added["reason"]})
            changes.append({"operation": "add_question_premise", "new_node_id": len(steps), **added})
        steps.append({k: node[k] for k in STEP_FIELDS})
        mapping.append({"old_node_id": old_id, "new_node_id": len(steps)})
    normalized = normalize({"steps": steps, "omissions": omissions}, item)
    record = {"protocol": PROTOCOL, "repair_round": 1,
              "previous_normalization_sha256": digest(previous),
              "repaired_normalization_sha256": digest(normalized),
              "code_sha256": digest(item["reference_code"]),
              "question_sha256": digest(item["question"]),
              "original_output_sha256": digest(item["raw_output"]),
              "node_mapping": mapping, "changes": changes, "reason": value["reason"],
              "dependencies": "recomputed in a separate stage; no automatic edge copying or closure"}
    return normalized, record


def apply_versioned_repair(value, previous, item, protocol=PROTOCOL):
    if protocol == CHECKED_PROTOCOL:
        from .calibri_repair_plan import apply_checked_repair
        return apply_checked_repair(value, previous, item)
    require(protocol == PROTOCOL, "unknown repair protocol")
    return apply_repair(value, previous, item)


def assemble_repaired_graph(value, normalized, item, record):
    graph = assemble_graph(value, normalized, item)
    if record["protocol"] == CHECKED_PROTOCOL:
        from .calibri_repair_plan import validate_premise_paths
        validate_premise_paths(graph, record)
    return graph


def dependency_data(data, normalized, record):
    result = {**data, "normalized": normalized}
    if record["protocol"] == CHECKED_PROTOCOL:
        result["premise_inventory"] = record["premise_inventory"]
    return result


def validate_repair_audit(value, protocol=PROTOCOL):
    validate_audit(value)
    checks = REPAIR_CHECKS
    if protocol == CHECKED_PROTOCOL:
        from .calibri_repair_plan import AUDIT_CHECKS
        checks += AUDIT_CHECKS
    require(all(k in value["checks"] and (type(value["checks"][k]) is bool or value["checks"][k] is None)
                for k in checks), "missing repair audit check")
    if value["decision"] == "accept":
        require(all(value["checks"][k] is True for k in checks), "unresolved repair audit")


def _failed_seed(parent, item, config):
    directory = parent / "items" / item["item_id"]
    result = read_json(directory / "result.json")
    require(result["item_id"] == item["item_id"] and result["status"] == "needs_review"
            and result["stage"] == "dependencies" and result["dag_sha256"] is None,
            "repair only a completed dependency-stage failure")
    normalized = read_json(directory / "normalization.json")
    require(normalize(read_json(directory / "normalize/output.json"), item) == normalized,
            "normalization output changed")
    data = {**public_input(item), "normalized": normalized}
    expected = payload("dependencies", data, config)
    require(read_json(directory / "dependencies/input.json") == {
        "input": data, "payload_sha256": digest(expected)}, "dependency input changed")
    responses = list((directory / "dependencies").glob("attempt-*/response.json"))
    require(len(responses) == 1, "exactly one semantic dependency response required")
    response_path = responses[0]
    request = read_json(response_path.parent / "request.json")
    body = read_json(response_path)["body"]
    require(request["payload"] == expected, "dependency request changed")
    contract = check_response(expected, body, request["reserved_tokens"],
        strict=config.strict_response_contract, content_gated=config.content_gated_response)
    require(not contract["violations"], "cannot repair an API contract failure")
    choices = body.get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop", "non-stop source response")
    try:
        dependencies = parse_object(choices[0]["message"].get("content"))
        require(isinstance(dependencies, dict) and set(dependencies) == {
            "dependencies", "answer_parents", "answer_justification"}, "not a dependency proposal")
    except InvalidOutput as error:
        require(str(error) == result["reason"] == read_json(
            directory / "dependencies/validation.json")["reason"]
            and not (directory / "dependencies/output.json").exists(),
            "nonstructural dependency failure does not match recorded evidence")
        raise NoDependencyProposal(str(error)) from error
    try:
        assemble_graph(dependencies, normalized, item)
    except InvalidOutput as error:
        require(str(error) == result["reason"] == read_json(directory / "dependencies/validation.json")["reason"],
                "dependency failure no longer reproduces")
    else:
        raise InvalidOutput("source graph is not a structural failure")
    paths = [directory / name for name in (
        "result.json", "normalization.json", "normalize/output.json", "dependencies/input.json",
        "dependencies/validation.json")]
    paths += [response_path, response_path.parent / "request.json"]
    return {"previous_normalized": normalized, "previous_dependencies": dependencies,
            "previous_result": result}, paths


def _used_budget(root, config):
    proof = read_json(root / "calibri-normalization-manifest.json")
    requests = list(root.glob("items/*/*/attempt-*/request.json"))
    prior_calls = proof["prior_calls"] + len(requests)
    prior_reserved = proof["prior_reserved_tokens"]
    for path in requests:
        request = read_json(path)
        account = request["reserved_tokens"]
        if (path.parent / "response.json").exists():
            check = check_response(request["payload"], read_json(path.parent / "response.json")["body"],
                account, strict=config.strict_response_contract, content_gated=config.content_gated_response)
            require(not check["violations"], "source run has API contract violations")
            account = check["accounted_tokens"]
        prior_reserved += account
    return prior_calls, prior_reserved


def _dns_failed_transport_run(run, expected_parent):
    """Audit a paused, response-free DNS failure without refunding any attempt."""
    run = Path(run).resolve()
    config = Config.load(run / "run_config.json")
    require(config.prompt_version == CHECKED_PROTOCOL
            and read_json(run / "completion.json")["status"] == "paused",
            "only a paused checked first pass can be transport-recovered")
    manifest = read_json(run / "calibri-repair-manifest.json")
    require(manifest.get("repair_mode") == "checked_first_pass"
            and manifest["parent_run"] == str(Path(expected_parent).resolve())
            and "transport_recovery" not in manifest,
            "DNS recovery cannot chain or change its source")
    items = verify_repair(run, config)
    require(not list(run.glob("items/*/result.json"))
            and not list(run.glob("items/*/dag.json"))
            and not list(run.glob("items/*/*/attempt-*/response.json")),
            "DNS failure contains a semantic response or terminal result")
    requests = sorted(run.glob("items/*/*/attempt-*/request.json"))
    require(bool(requests), "no DNS-failed requests to recover")
    for path in requests:
        require(path.parts[-3] == "repair", "DNS recovery cannot replay later stages")
        stage = path.parent.parent
        input_record = read_json(stage / "input.json")
        expected = payload("repair", input_record["input"], config)
        request = read_json(path)
        error = read_json(path.parent / "error.json")
        require(input_record["payload_sha256"] == digest(expected)
                and request["payload"] == expected
                and error["category"] == "uncertain_remote_state"
                and error["transport_kind"] == "curl_exit_6"
                and error["http_status"] is None,
                "DNS recovery evidence is not a response-free name-resolution failure")
    return (_used_budget(run, config), [item["item_id"] for item in items],
            len(requests))


def _development_feedback(history, item):
    """Only bound public material; never send run paths or hidden test metadata."""
    directory = history / "items" / item["item_id"]
    result = read_json(directory / "result.json")
    require(result["status"] == "rejected" and result["stage"] == "review_dag",
            "checked pilot only revisits v1 semantic rejections")
    review = read_json(directory / "review_dag/output.json")
    validate_repair_audit(review)
    require(review["decision"] == "reject" and review["reason"] == result["reason"], "history rejection mismatch")
    paths = list((directory / "review_dag").glob("attempt-*/response.json"))
    require(len(paths) == 1, "ambiguous historical review")
    response = read_json(paths[0])["body"]
    choices = response.get("choices", [])
    require(len(choices) == 1 and choices[0].get("finish_reason") == "stop"
            and parse_object(choices[0]["message"].get("content")) == review, "historical review response changed")
    return {"protocol": PROTOCOL, "status": "prior_development_failure_not_an_independent_test",
            "normalization": read_json(directory / "normalization.json"),
            "repair_record": read_json(directory / "repair-record.json"), "review": review}


def prepare_repair(parent, root, *, max_calls=9, max_reserved_tokens=1200000,
                   prompt_version=PROTOCOL, history=None, first_pass=False,
                   transport_failed_run=None, campaign_token_limit=CAMPAIGN_TOKEN_LIMIT):
    """Freeze all eligible failures; v2 explicitly accounts for the prior pilot."""
    parent, root = Path(parent).resolve(), private_dir(root)
    require(parent != root and not root.is_relative_to(parent), "repair must use a new run directory")
    require(prompt_version in (PROTOCOL, CHECKED_PROTOCOL), "unknown repair version")
    require(type(first_pass) is bool and (not first_pass or
            (prompt_version == CHECKED_PROTOCOL and history is None)),
            "first pass requires checked protocol without revision history")
    checked_revision = prompt_version == CHECKED_PROTOCOL and not first_pass
    old_config = Config.load(parent / "run_config.json")
    require(old_config.prompt_version in ("calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3")
            and not (parent / "calibri-repair-manifest.json").exists(), "one repair pass only, from v2")
    require(read_json(parent / "completion.json")["status"] == "processed", "parent must have completed")
    if old_config.prompt_version == "calibri-lcb-normalize-v3":
        from .calibri_continuation import audit_completed
        require(audit_completed(parent)["mechanical_pass"], "continuation audit failed")
    original_items = verify_prepared(parent, old_config)
    old_proof = read_json(parent / "calibri-normalization-manifest.json")
    require(type(campaign_token_limit) is int
            and CAMPAIGN_TOKEN_LIMIT <= campaign_token_limit <= DNS_RECOVERY_TOKEN_LIMIT
            and (campaign_token_limit == CAMPAIGN_TOKEN_LIMIT or transport_failed_run is not None),
            "extra budget requires a bounded DNS transport recovery")
    recovery = None
    history_files, history_items, history_results = {}, {}, {}
    if checked_revision:
        require(history is not None, "checked revision requires explicit prior development history")
        history = Path(history).resolve()
        require(history != root and not root.is_relative_to(history), "history must be outside new run")
        history_config = Config.load(history / "run_config.json")
        require(history_config.prompt_version == PROTOCOL and read_json(history / "completion.json")["status"] == "processed",
                "only a completed v1 pilot is valid revision history")
        history_items = {i["item_id"]: i for i in verify_repair(history, history_config)}
        require(digest(read_json(history / "parent-evidence/items.json")) == digest(original_items),
                "revision history belongs to another source cohort")
        for item_id in history_items:
            history_results[item_id] = read_json(history / "items" / item_id / "result.json")
        for name in ("items.json", "selection.json", "run_config.json", "completion.json",
                     "calibri-normalization-manifest.json", "calibri-repair-manifest.json"):
            history_files[name] = (history / name).read_bytes()
        for folder in ("evidence", "parent-evidence", "repair-seeds", "items"):
            for path in (history / folder).rglob("*"):
                if path.is_file() and path.name != ".lock":
                    history_files[str(path.relative_to(history))] = path.read_bytes()
        prior_calls, prior_reserved = _used_budget(history, history_config)
    else:
        require(history is None, "first repair does not accept revision history")
        prior_calls, prior_reserved = _used_budget(parent, old_config)
    if transport_failed_run is not None:
        require(first_pass and prompt_version == CHECKED_PROTOCOL and history is None,
                "DNS transport recovery requires the same checked first pass")
        transport_failed_run = Path(transport_failed_run).resolve()
        require(transport_failed_run != root and not root.is_relative_to(transport_failed_run),
                "transport recovery requires a new directory")
        (prior_calls, prior_reserved), prior_selected_ids, failed_count = (
            _dns_failed_transport_run(transport_failed_run, parent))
        recovery = {"protocol": DNS_RECOVERY_PROTOCOL,
                    "failed_run": str(transport_failed_run),
                    "failed_requests": failed_count,
                    "prior_selected_ids": prior_selected_ids,
                    "prior_calls": prior_calls,
                    "prior_reserved_tokens": prior_reserved,
                    "campaign_token_limit": campaign_token_limit}
    require(type(max_calls) is int and 0 < max_calls <= CAMPAIGN_CALL_LIMIT - prior_calls,
            "campaign call allocation exceeded")
    require(type(max_reserved_tokens) is int and 0 < max_reserved_tokens <= campaign_token_limit - prior_reserved,
            "campaign token allocation exceeded")
    items, exclusions, seeds, files = [], [], {}, {}
    for item in original_items:
        result_path = parent / "items" / item["item_id"] / "result.json"
        result = read_json(result_path)
        if checked_revision and item["item_id"] not in history_items:
            exclusions.append({"item_id": item["item_id"], "status": result["status"], "stage": result["stage"],
                               "reason": "outside prior failed-cohort development revision"})
            files[str(result_path.relative_to(parent))] = result_path.read_bytes()
            continue
        if checked_revision and history_results[item["item_id"]]["status"] == "model_accepted":
            exclusions.append({"item_id": item["item_id"], "status": "model_accepted", "stage": "review_dag",
                               "reason": "accepted in previous repair; not resampled"})
            files[str(result_path.relative_to(parent))] = result_path.read_bytes()
            continue
        if result.get("status") == "needs_review" and result.get("stage") == "dependencies":
            try:
                seed, paths = _failed_seed(parent, item, old_config)
            except NoDependencyProposal as error:
                exclusions.append({"item_id": item["item_id"], "status": result["status"],
                                   "stage": result["stage"],
                                   "reason": "no parseable dependency graph; not repaired: " + str(error)})
                files[str(result_path.relative_to(parent))] = result_path.read_bytes()
                continue
            if checked_revision:
                require(history_items[item["item_id"]] == item, "revision source item changed")
                seed["development_feedback"] = _development_feedback(history, item)
            items.append(item)
            seeds[item["item_id"]] = seed
            for path in paths:
                files[str(path.relative_to(parent))] = path.read_bytes()
        else:
            exclusions.append({"item_id": item["item_id"], "status": result["status"], "stage": result["stage"],
                               "reason": "not a dependency-stage failure; not resampled"})
            files[str(result_path.relative_to(parent))] = result_path.read_bytes()
    require(bool(items), "no eligible dependency failures")
    if recovery is not None:
        require(recovery["prior_selected_ids"] == [item["item_id"] for item in items],
                "transport recovery changed the fixed repair cohort")
    selection = {"selected_ids": [i["item_id"] for i in items], "selected_count": len(items),
                 "source_count": len(original_items), "excluded": exclusions,
                 "sampling": ("all remaining v1 semantic failures, checked development revision; no PALS-based selection"
                              if checked_revision else
                              "all parseable structural dependency failures, one repair each; no PALS-based selection")}
    for name in ("items.json", "selection.json", "run_config.json", "completion.json", "calibri-normalization-manifest.json"):
        files[name] = (parent / name).read_bytes()
    if old_config.prompt_version == "calibri-lcb-normalize-v3":
        files["offline-continuation-audit.json"] = (parent / "offline-continuation-audit.json").read_bytes()
        files["calibri-continuation-manifest.json"] = (parent / "calibri-continuation-manifest.json").read_bytes()
    proof = {**old_proof, "prompt_version": prompt_version, "items_sha256": digest(items),
             "selection_sha256": digest(selection), "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens,
             "prior_calls": prior_calls, "prior_reserved_tokens": prior_reserved,
             "campaign_token_limit": campaign_token_limit}
    for path in (parent / "evidence").rglob("*"):
        if path.is_file():
            write_bytes_once(root / path.relative_to(parent), path.read_bytes())
    for name, content in files.items():
        write_bytes_once(root / "parent-evidence" / name, content)
    for name, content in history_files.items():
        write_bytes_once(root / "revision-history" / name, content)
    for item_id, seed in seeds.items():
        write_once(root / "repair-seeds" / (item_id + ".json"), seed)
    manifest = {"protocol": prompt_version, "max_repair_rounds": 1, "parent_run": str(parent),
                "parent_files": {name: sha256(content).hexdigest() for name, content in files.items()},
                "seed_sha256": {key: digest(value) for key, value in seeds.items()},
                "items_sha256": digest(items), "selection_sha256": digest(selection),
                "normalization_manifest_sha256": digest(proof)}
    if first_pass:
        manifest["repair_mode"] = "checked_first_pass"
    if recovery is not None:
        hashes = {}
        for path in sorted(transport_failed_run.rglob("*")):
            if path.is_file():
                require(not path.is_symlink(), "symlinked transport evidence")
                name = str(path.relative_to(transport_failed_run))
                if path.name == ".lock":
                    continue
                content = path.read_bytes()
                hashes[name] = sha256(content).hexdigest()
                write_bytes_once(root / "transport-history" / name, content)
        recovery["source_files"] = hashes
        manifest["transport_recovery"] = recovery
    if checked_revision:
        manifest["development_iteration"] = 2
        manifest["history_files"] = {name: sha256(content).hexdigest() for name, content in history_files.items()}
    for name, value in (("items.json", items), ("selection.json", selection),
                        ("calibri-normalization-manifest.json", proof), ("calibri-repair-manifest.json", manifest)):
        write_once(root / name, value)
    return selection


def verify_repair(root, config):
    manifest = read_json(root / "calibri-repair-manifest.json")
    recovery = manifest.get("transport_recovery")
    limit = recovery["campaign_token_limit"] if recovery is not None else CAMPAIGN_TOKEN_LIMIT
    items = verify_prepared(root, config, campaign_token_limit=limit)
    require(manifest["protocol"] == config.prompt_version and manifest["protocol"] in (PROTOCOL, CHECKED_PROTOCOL)
            and manifest["max_repair_rounds"] == 1
            and digest(items) == manifest["items_sha256"]
            and digest(read_json(root / "selection.json")) == manifest["selection_sha256"]
            and digest(read_json(root / "calibri-normalization-manifest.json")) == manifest["normalization_manifest_sha256"],
            "repair manifest changed")
    for name, expected in manifest["parent_files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts, "invalid provenance path")
        require(sha256((root / "parent-evidence" / name).read_bytes()).hexdigest() == expected,
                "parent repair evidence changed")
    parent = root / "parent-evidence"
    old_config = Config.load(parent / "run_config.json")
    require(old_config.prompt_version in ("calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3"),
            "cannot repair another repair")
    first_pass = manifest.get("repair_mode") == "checked_first_pass"
    require(manifest.get("repair_mode") in (None, "checked_first_pass"), "unknown repair mode")
    if first_pass:
        require(config.prompt_version == CHECKED_PROTOCOL
                and "development_iteration" not in manifest and "history_files" not in manifest
                and not (root / "revision-history").exists(), "mixed first-pass and revision evidence")
    if recovery is not None:
        require(first_pass and recovery["protocol"] == DNS_RECOVERY_PROTOCOL
                and recovery["campaign_token_limit"] == DNS_RECOVERY_TOKEN_LIMIT,
                "invalid DNS transport recovery protocol or limit")
        history = root / "transport-history"
        for name, expected in recovery["source_files"].items():
            require(not Path(name).is_absolute() and ".." not in Path(name).parts
                    and sha256((history / name).read_bytes()).hexdigest() == expected,
                    "DNS transport evidence changed")
        (calls, tokens), selected, failed = _dns_failed_transport_run(
            history, manifest["parent_run"])
        proof = read_json(root / "calibri-normalization-manifest.json")
        require((calls, tokens, selected, failed) == (
            recovery["prior_calls"], recovery["prior_reserved_tokens"],
            recovery["prior_selected_ids"], recovery["failed_requests"])
            and [item["item_id"] for item in items] == selected
            and (proof["prior_calls"], proof["prior_reserved_tokens"]) == (calls, tokens)
            and calls + config.max_calls <= CAMPAIGN_CALL_LIMIT
            and tokens + config.max_reserved_tokens <= recovery["campaign_token_limit"],
            "DNS recovery ledger or selected cohort changed")
    if config.prompt_version == CHECKED_PROTOCOL and not first_pass:
        require(manifest["development_iteration"] == 2, "undeclared development revision")
        for name, expected in manifest["history_files"].items():
            require(not Path(name).is_absolute() and ".." not in Path(name).parts, "invalid history path")
            require(sha256((root / "revision-history" / name).read_bytes()).hexdigest() == expected,
                    "development history changed")
        history_config = Config.load(root / "revision-history/run_config.json")
        require(history_config.prompt_version == PROTOCOL, "cannot chain checked revisions")
        verify_repair(root / "revision-history", history_config)
    for item in items:
        seed = read_json(root / "repair-seeds" / (item["item_id"] + ".json"))
        require(digest(seed) == manifest["seed_sha256"][item["item_id"]], "repair seed changed")
        expected_seed, _ = _failed_seed(parent, item, old_config)
        if config.prompt_version == CHECKED_PROTOCOL and not first_pass:
            expected_seed["development_feedback"] = _development_feedback(root / "revision-history", item)
        require(seed == expected_seed, "repair seed does not match its failed source")
    return items


class CALIBRIRepairPipeline(Pipeline):
    def run(self, limit=None, progress=None, through="review_dag"):
        require(self.config.prompt_version in (PROTOCOL, CHECKED_PROTOCOL) and through == "review_dag", "wrong repair protocol")
        verify_repair(self.root, self.config)
        return super().run(limit, progress, through)

    def process(self, item, through="review_dag"):
        directory = self.root / "items" / item["item_id"]
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        seed = read_json(self.root / "repair-seeds" / (item["item_id"] + ".json"))
        data, stage = public_input(item), "repair"
        repair_input = {**data, **seed}
        protocol = self.config.prompt_version
        try:
            proposal = self.request_stage(stage, item, repair_input, payload(stage, repair_input, self.config),
                lambda v: apply_versioned_repair(v, seed["previous_normalized"], item, protocol))
            normalized, record = apply_versioned_repair(proposal, seed["previous_normalized"], item, protocol)
            write_once(directory / "normalization.json", normalized)
            write_once(directory / "repair-record.json", record)
            stage = "dependencies"
            dependency_input = dependency_data(data, normalized, record)
            deps = self.request_stage(stage, item, dependency_input, payload(stage, dependency_input, self.config),
                lambda v: assemble_repaired_graph(v, normalized, item, record))
            graph = assemble_repaired_graph(deps, normalized, item, record)
            stage = "review_dag"
            audit_input = {**data, "normalized": normalized, "candidate": graph,
                           "previous_normalized": seed["previous_normalized"], "repair_record": record}
            audit = self.request_stage(stage, item, audit_input, payload(stage, audit_input, self.config),
                                       lambda v: validate_repair_audit(v, protocol))
            if audit["decision"] != "accept":
                return self._finish(item, "rejected" if audit["decision"] == "reject" else "needs_review",
                                    stage, audit["reason"])
            dag = {"schema_version": "reference_dag_v1", "construction_protocol": protocol,
                   "item_id": item["item_id"], "source": item, "nodes": graph["nodes"],
                   "normalization": normalized, "repair_record": record,
                   "recovery_provenance": {"parent_seed_sha256": digest(seed), "repair_round": 1},
                   "dag_review": audit, "execution_evidence": item["execution_evidence"],
                   "calculation_check": {"status": "reference_tests_passed"}, "formal_eligible": False,
                   "quality_status": "model_reviewed_pending_release_audit",
                   "limitation": "Source-bound repaired explanation; unchanged tested code; same-model review, not native CoT or official/human gold"}
            if protocol == CHECKED_PROTOCOL:
                manifest = read_json(self.root / "calibri-repair-manifest.json")
                if manifest.get("repair_mode") == "checked_first_pass":
                    dag["recovery_provenance"]["repair_mode"] = "checked_first_pass"
                else:
                    dag["recovery_provenance"]["development_iteration"] = 2
                if "transport_recovery" in manifest:
                    dag["recovery_provenance"]["transport_recovery_protocol"] = DNS_RECOVERY_PROTOCOL
            write_once(directory / "dag.json", dag)
            return self._finish(item, "model_accepted", stage, "pending release audit", digest(dag))
        except InvalidOutput as error:
            return self._finish(item, "needs_review", stage, str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused", "stage": stage, "reason": error.category}


def main():
    import argparse
    import json
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=9)
    parser.add_argument("--max-reserved-tokens", type=int, default=1200000)
    parser.add_argument("--prompt-version", choices=(PROTOCOL, CHECKED_PROTOCOL), default=PROTOCOL)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--first-pass", action="store_true",
                        help="Apply checked v2 directly once to new normalization failures, without a v1 repair")
    parser.add_argument("--transport-failed-run", type=Path)
    parser.add_argument("--campaign-token-limit", type=int, default=CAMPAIGN_TOKEN_LIMIT)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        print(json.dumps(prepare_repair(args.parent, args.root, max_calls=args.max_calls,
                                       max_reserved_tokens=args.max_reserved_tokens,
                                       prompt_version=args.prompt_version, history=args.history,
                                       first_pass=args.first_pass,
                                       transport_failed_run=args.transport_failed_run,
                                       campaign_token_limit=args.campaign_token_limit)))


if __name__ == "__main__":
    main()
