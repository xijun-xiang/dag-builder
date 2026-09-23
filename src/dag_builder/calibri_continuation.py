"""Resume a stopped CALIBRI normalization without replaying settled judgments.

The new run owns a frozen snapshot of the old one. Returned stage responses are
replayed only when their request payload is identical; the v3 review prompt is
applied only to an unfinished review or one explicitly incomplete schema audit.
All old requests, including six uncertain ones, remain inside the shared budget.
"""

from hashlib import sha256
from pathlib import Path

from .calibri_normalize import REVIEW_CHECKS, assemble_graph, normalize, validate_audit
from .calibri_pipeline import (
    CAMPAIGN_CALL_LIMIT, CAMPAIGN_TOKEN_LIMIT, verify_prepared,
)
from .calibri_repair import _used_budget
from .config import Config
from .response_contract import check_response
from .schemas import parse_object, require
from .stages import payload
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

PROTOCOL = "calibri-lcb-continuation-v1"
SOURCE_VERSION = "calibri-lcb-normalize-v2"
TARGET_VERSION = "calibri-lcb-normalize-v3"
SOURCE_METADATA = (
    "items.json", "selection.json", "run_config.json", "calibri-normalization-manifest.json",
    "operator-stop-20260923-schema.json", "launch.json", "worker-start.json",
    "implementation.json", "code_origin.json", "inputs_manifest.json",
)


def _source_files(parent):
    for name in SOURCE_METADATA:
        path = parent / name
        require(path.is_file() and not path.is_symlink(), "missing stopped-run provenance")
        yield path
    for folder in ("evidence", "items"):
        for path in sorted((parent / folder).rglob("*")):
            if path.is_file():
                require(not path.is_symlink(), "symlinked stopped-run artifact")
                yield path


def _review_schema_retry(directory):
    result = read_json(directory / "result.json")
    if not (result["status"] == "needs_review" and result["stage"] == "review_dag"
            and result["reason"] == "missing normalization review check"):
        return False
    response_path = directory / "review_dag/attempt-00/response.json"
    require(response_path.exists(), "missing original schema review")
    choices = read_json(response_path)["body"].get("choices", [])
    require(len(choices) == 1 and choices[0]["finish_reason"] == "stop", "incomplete old review")
    response = parse_object(choices[0]["message"].get("content"))
    require(isinstance(response.get("checks"), dict)
            and set(REVIEW_CHECKS) - set(response["checks"]) == {"no_invariant_assumed"},
            "schema failure does not match the declared omission")
    return response.get("decision") == "accept"


def _replay_recorded_calls(root, config, *, old_uncertain=()):
    """Check recorded request inputs and formal content without sending a call."""
    items = {item["item_id"]: item for item in read_json(root / "items.json")}
    legacy_config = (Config.load(root / "parent-evidence/run_config.json")
                     if (root / "parent-evidence/run_config.json").exists() else config)
    uncertain = set(old_uncertain)
    seen_uncertain = set()
    for path in sorted(root.glob("items/*/*/attempt-*/request.json")):
        name = str(path.relative_to(root))
        stage = path.parts[-3]
        source_stage = "review_dag" if stage == "legacy_review_dag" else stage
        require(source_stage in ("normalize", "dependencies", "review_dag"),
                "unknown recorded stage")
        item_id = path.parts[-4]
        require(item_id in items, "request outside frozen cohort")
        input_record = read_json(path.parent.parent / "input.json")
        request = read_json(path)
        controls = legacy_config if stage == "legacy_review_dag" else config
        expected = payload(source_stage, input_record["input"], controls)
        require(input_record["payload_sha256"] == digest(expected)
                and request["payload"] == expected, "recorded request differs from frozen input")
        response_path = path.parent / "response.json"
        error_path = path.parent / "error.json"
        require(not (response_path.exists() and error_path.exists()), "ambiguous recorded attempt")
        if not response_path.exists() and not error_path.exists():
            require(name in uncertain, "unaccounted uncertain request")
            seen_uncertain.add(name)
            continue
        if not response_path.exists():
            continue
        choices = read_json(response_path)["body"].get("choices", [])
        contract = check_response(expected, read_json(response_path)["body"],
                                  request["reserved_tokens"],
                                  strict=controls.strict_response_contract,
                                  content_gated=controls.content_gated_response)
        require(not contract["violations"], "recorded response contract changed")
        output_path = path.parent.parent / "output.json"
        if not output_path.exists():
            continue
        require(len(choices) == 1 and choices[0]["finish_reason"] == "stop",
                "non-stop recorded stage output")
        output = parse_object(choices[0]["message"].get("content"))
        require(output == read_json(output_path), "recorded stage output changed")
        if source_stage == "normalize":
            normalize(output, items[item_id])
        elif source_stage == "dependencies":
            normalized = read_json(path.parent.parent.parent / "normalization.json")
            assemble_graph(output, normalized, items[item_id])
        else:
            validate_audit(output)
    require(seen_uncertain == uncertain, "stopped uncertain-call set changed")


def _classify(parent, items, stopped):
    actions = {}
    for item in items:
        item_id = item["item_id"]
        directory = parent / "items" / item_id
        if (directory / "result.json").exists():
            if _review_schema_retry(directory):
                actions[item_id] = "one_fixed_graph_schema_review"
            else:
                actions[item_id] = "carry_terminal_result"
            continue
        pending = [name for name in stopped if name.startswith("items/" + item_id + "/")]
        require(len(pending) <= 1, "multiple uncertain requests on one item")
        if pending:
            stage = Path(pending[0]).parts[2]
            require(stage in ("normalize", "review_dag"), "unplanned uncertain stage")
            actions[item_id] = "retry_uncertain_" + stage
        else:
            require(not directory.exists(), "unclassified partial item")
            actions[item_id] = "fresh"
    require(set(actions.values()) <= {
        "one_fixed_graph_schema_review", "carry_terminal_result",
        "retry_uncertain_review_dag", "retry_uncertain_normalize", "fresh",
    } and bool(actions), "unexpected stopped-run status categories")
    return actions


def prepare_continuation(parent, root, *, max_calls=561, max_reserved_tokens=22265390):
    parent, root = Path(parent).resolve(), private_dir(root)
    require(parent != root and not root.is_relative_to(parent), "continuation requires a new directory")
    require(not any(path.name != ".lock" for path in root.iterdir()),
            "continuation directory must be empty")
    old_config = Config.load(parent / "run_config.json")
    require(old_config.prompt_version == SOURCE_VERSION, "unexpected stopped protocol")
    items = verify_prepared(parent, old_config)
    require(not (parent / "completion.json").exists(), "source already completed")
    stopped = read_json(parent / "operator-stop-20260923-schema.json")
    require(stopped["action"].startswith("SIGTERM exact verified worker process group")
            and type(stopped["requests_recorded"]) is int
            and stopped["requests_recorded"] >= 0, "unexpected stop record")
    pending = set(stopped["pending_at_stop"])
    require(all(name.startswith("items/") and name.endswith("/request.json")
            for name in pending), "unexpected uncertain requests")
    actual_pending = {str(path.relative_to(parent)) for path in parent.glob("items/*/*/attempt-*/request.json")
                      if not (path.parent / "response.json").exists()
                      and not (path.parent / "error.json").exists()}
    require(actual_pending == pending, "uncertain request evidence changed")
    _replay_recorded_calls(parent, old_config, old_uncertain=pending)
    prior_calls, prior_reserved = _used_budget(parent, old_config)
    old_proof = read_json(parent / "calibri-normalization-manifest.json")
    require(prior_calls == old_proof["prior_calls"] + stopped["requests_recorded"]
            and stopped["reserved_tokens"] == sum(read_json(path)["reserved_tokens"]
            for path in parent.glob("items/*/*/attempt-*/request.json")),
            "stopped-run request ledger changed")
    require(type(max_calls) is int and stopped["requests_recorded"] < max_calls
            <= CAMPAIGN_CALL_LIMIT - old_proof["prior_calls"], "call budget allocation exceeded")
    require(type(max_reserved_tokens) is int and prior_reserved - old_proof["prior_reserved_tokens"]
            < max_reserved_tokens <= CAMPAIGN_TOKEN_LIMIT - old_proof["prior_reserved_tokens"],
            "token budget allocation exceeded")
    actions = _classify(parent, items, pending)
    source_hashes, active_map = {}, {}
    for path in _source_files(parent):
        name = str(path.relative_to(parent))
        content = path.read_bytes()
        source_hashes[name] = sha256(content).hexdigest()
        write_bytes_once(root / "parent-evidence" / name, content)
        if name.startswith("evidence/"):
            active = name
        elif name.startswith("items/"):
            parts = Path(name).parts
            action = actions[parts[1]]
            if action == "one_fixed_graph_schema_review" and len(parts) == 3 and parts[2] == "result.json":
                continue
            if (action in ("one_fixed_graph_schema_review", "retry_uncertain_review_dag")
                    and len(parts) >= 3 and parts[2] == "review_dag"):
                active = str(Path(parts[0], parts[1], "legacy_review_dag", *parts[3:]))
            else:
                active = name
        else:
            continue
        active_map[name] = active
        write_bytes_once(root / active, content)
    selection = read_json(parent / "selection.json")
    config = Config(**{**old_config.to_dict(), "prompt_version": TARGET_VERSION,
                       "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens})
    proof = {**old_proof, "prompt_version": TARGET_VERSION,
             "max_calls": max_calls, "max_reserved_tokens": max_reserved_tokens}
    for name, value in (("items.json", items), ("selection.json", selection),
                        ("config.json", config.to_dict()),
                        ("calibri-normalization-manifest.json", proof)):
        write_once(root / name, value)
    manifest = {"protocol": PROTOCOL, "parent_run": str(parent),
                "parent_evidence_sha256": digest(source_hashes),
                "source_files": source_hashes, "active_files": active_map,
                "actions": actions, "prior_calls": prior_calls,
                "prior_reserved_tokens": prior_reserved,
                "config_sha256": digest(config.to_dict()),
                "proof_sha256": digest(proof),
                "uncertain_requests": sorted(pending),
                "policy": "one new fixed-graph review only for raw accept missing no_invariant_assumed; all old calls retained"}
    write_once(root / "calibri-continuation-manifest.json", manifest)
    return {"selected_count": len(items), "actions": actions,
            "historical_requests": prior_calls, "historical_accounted_tokens": prior_reserved}


def verify_continuation(root, config):
    root = Path(root)
    items = verify_prepared(root, config)
    manifest = read_json(root / "calibri-continuation-manifest.json")
    require(manifest["protocol"] == PROTOCOL and config.prompt_version == TARGET_VERSION
            and digest(config.to_dict()) == manifest["config_sha256"]
            and digest(read_json(root / "calibri-normalization-manifest.json")) == manifest["proof_sha256"],
            "continuation manifest/config changed")
    require(set(manifest["actions"]) == {item["item_id"] for item in items}
            and digest(manifest["source_files"]) == manifest["parent_evidence_sha256"],
            "continuation cohort changed")
    for name, expected in manifest["source_files"].items():
        require(not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe source path")
        source_path = root / "parent-evidence" / name
        require(source_path.is_file() and sha256(source_path.read_bytes()).hexdigest() == expected,
                "stopped-run evidence changed")
        if name in manifest["active_files"]:
            active = manifest["active_files"][name]
            require(not Path(active).is_absolute() and ".." not in Path(active).parts,
                    "unsafe active path")
            active_path = root / active
            require(active_path.is_file() and sha256(active_path.read_bytes()).hexdigest() == expected,
                    "imported stage artifact changed")
    source = root / "parent-evidence"
    old_config = Config.load(source / "run_config.json")
    require(read_json(source / "items.json") == items
            and read_json(source / "selection.json") == read_json(root / "selection.json")
            and read_json(source / "calibri-normalization-manifest.json")["prompt_version"] == SOURCE_VERSION,
            "source cohort or protocol changed")
    verify_prepared(source, old_config)
    old_calls, old_tokens = _used_budget(source, old_config)
    require((old_calls, old_tokens) == (manifest["prior_calls"], manifest["prior_reserved_tokens"]),
            "source budget changed")
    require(set(manifest["uncertain_requests"]) == set(read_json(
        source / "operator-stop-20260923-schema.json")["pending_at_stop"]),
        "uncertain call ledger changed")
    _replay_recorded_calls(source, old_config, old_uncertain=manifest["uncertain_requests"])
    for item in items:
        action = manifest["actions"][item["item_id"]]
        directory = source / "items" / item["item_id"]
        if action == "one_fixed_graph_schema_review":
            require(_review_schema_retry(directory), "ineligible schema re-review")
            require(not (root / "items" / item["item_id"] / "legacy_review_dag/output.json").exists(),
                    "old incomplete review was promoted")
        elif action == "carry_terminal_result":
            require((root / "items" / item["item_id"] / "result.json").exists(),
                    "terminal result was dropped")
    return items


def audit_completed(root):
    """Verify the completed continuation, all imported evidence and each result."""
    from collections import Counter
    from .calibri_repair import _used_budget

    root = Path(root).resolve()
    config = Config.load(root / "run_config.json")
    items = verify_continuation(root, config)
    require(read_json(root / "completion.json")["status"] == "processed",
            "continuation has not completed")
    code = read_json(root / "code_origin.json")
    require(read_json(root / "implementation.json")["code_sha256"] == code["code_sha256"],
            "continuation implementation changed")
    for name, expected in code["source_files"].items():
        require(sha256((root / "controller/code/dag_builder" / name).read_bytes()).hexdigest() == expected,
                "frozen continuation code changed")
    manifest = read_json(root / "calibri-continuation-manifest.json")
    old_uncertain = {
        manifest["active_files"][name] for name in manifest["uncertain_requests"]
    }
    _replay_recorded_calls(root, config, old_uncertain=old_uncertain)
    statuses = Counter()
    for item in items:
        directory = root / "items" / item["item_id"]
        result = read_json(directory / "result.json")
        require(result["item_id"] == item["item_id"], "result item changed")
        statuses[result["status"]] += 1
        if result["status"] == "model_accepted":
            dag = read_json(directory / "dag.json")
            normalized = normalize(read_json(directory / "normalize/output.json"), item)
            graph = assemble_graph(read_json(directory / "dependencies/output.json"), normalized, item)
            review = read_json(directory / "review_dag/output.json")
            validate_audit(review)
            require(review["decision"] == "accept" and dag["source"] == item
                    and dag["nodes"] == graph["nodes"] and dag["normalization"] == normalized
                    and dag["dag_review"] == review and dag["formal_eligible"] is False
                    and result["dag_sha256"] == digest(dag), "accepted DAG not reproducible")
        else:
            require(not (directory / "dag.json").exists(), "unaccepted DAG published")
    calls, reserved = _used_budget(root, config)
    require(calls <= CAMPAIGN_CALL_LIMIT and reserved <= CAMPAIGN_TOKEN_LIMIT,
            "campaign budget exceeded")
    require(sum(statuses.values()) == len(items), "partial continuation results")
    report = {"protocol": PROTOCOL, "mechanical_pass": True,
              "semantic_certification": False, "source_count": len(items),
              "statuses": dict(statuses), "campaign_requests": calls,
              "campaign_accounted_tokens": reserved,
              "new_requests": calls - manifest["prior_calls"],
              "new_accounted_tokens": reserved - manifest["prior_reserved_tokens"],
              "formal_eligible": False}
    write_once(root / "offline-continuation-audit.json", report)
    return report


def main():
    import argparse
    import json
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=561)
    parser.add_argument("--max-reserved-tokens", type=int, default=22265390)
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        if args.audit:
            print(json.dumps(audit_completed(args.root)))
        else:
            print(json.dumps(prepare_continuation(args.parent, args.root,
                max_calls=args.max_calls, max_reserved_tokens=args.max_reserved_tokens)))


if __name__ == "__main__":
    main()
