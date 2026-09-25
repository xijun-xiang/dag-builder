"""Continue a budget-paused t2ance DAG batch without resampling any stage.

The parent is immutable. A new private run replays its exact recorded requests,
responses and terminal decisions, then spends a separately bounded allocation
only on stages that never received a response. Transport errors and unknown
remote outcomes are deliberately not eligible for this path.
"""

import argparse
import hashlib
import os
from dataclasses import replace
from pathlib import Path

from .config import Config
from .schemas import require
from .storage import private_dir, read_json, write_bytes_once, write_once
from .t2ance_audit import (_replay_v3_recorded_calls,
                           _replay_v3_terminal_item, audit_completed)
from .t2ance_pipeline import PROTOCOL_V4, prepare, verify_prepared


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit_paused_parent(parent):
    """Accept only a complete, response-known, budget-paused parent."""
    parent = Path(parent)
    config = Config.load(parent / "run_config.json")
    require(config.prompt_version == PROTOCOL_V4
            and Config.load(parent / "config.json") == config,
            "parent configuration is not frozen v4")
    items = verify_prepared(parent, config)
    completion = read_json(parent / "completion.json")
    summary = completion["summary"]
    require(completion["status"] == "paused" and completion["global_stop"]
            and summary["selected"] == len(items)
            and summary["counts"].get("paused", 0) > 0,
            "parent is not a complete budget-paused batch")
    listed = {row["item_id"]: row for row in summary["items"]}
    require(set(listed) == {item["item_id"] for item in items},
            "parent summary omits an item")
    requests = sorted(parent.glob("items/*/*/attempt-*/request.json"))
    require(len(requests) == summary["api_attempts"] and requests,
            "parent request inventory changed")
    for request in requests:
        attempt = request.parent
        require((attempt / "response.json").is_file()
                and not (attempt / "error.json").exists(),
                "unknown or failed remote request requires a separate decision")
    _replay_v3_recorded_calls(parent, config, items)
    for item in items:
        result = parent / "items" / item["item_id"] / "result.json"
        if listed[item["item_id"]]["status"] == "paused":
            require(not result.exists()
                    and listed[item["item_id"]]["reason"] in
                    ("budget_exhausted", "paused"),
                    "parent has a non-budget pause")
        else:
            require(result.is_file()
                    and read_json(result)["status"] == listed[item["item_id"]]["status"],
                    "parent terminal result differs from summary")
            _replay_v3_terminal_item(parent, item, config)
    origin = read_json(parent / "code_origin.json")
    require(origin == read_json(parent / "controller/code/snapshot_origin.json"),
            "parent code origin changed")
    for name, expected in origin["source_files"].items():
        path = parent / "controller/code/dag_builder" / name
        require(path.is_file() and sha256(path) == expected,
                "parent code snapshot changed")
    return items, config, completion


def prepare_continuation(parent, source, execution, expected_manifest, root, *,
                         max_calls, max_reserved_tokens, prior_calls,
                         prior_reserved_tokens):
    parent = Path(parent).absolute()
    root = private_dir(root)
    require(root != parent and root not in parent.parents and parent not in root.parents,
            "continuation must have a separate private directory")
    items, old_config, completion = audit_paused_parent(parent)
    require(max_calls > completion["summary"]["api_attempts"],
            "new call cap must exceed carried requests")
    new_config = replace(old_config, max_calls=max_calls,
                         max_reserved_tokens=max_reserved_tokens)
    development_run = read_json(parent / "selection.json").get("development_run")
    prepare(source, execution, expected_manifest, root,
            development_run=Path(development_run) if development_run else None,
            max_calls=max_calls, max_reserved_tokens=max_reserved_tokens,
            prior_calls=prior_calls, prior_reserved_tokens=prior_reserved_tokens,
            prompt_version=old_config.prompt_version)
    require(read_json(root / "items.json") == items
            and new_config.prompt_version == old_config.prompt_version,
            "continuation input or prompt differs from parent")
    carried = {}
    for path in sorted((parent / "items").rglob("*")):
        if path.is_dir():
            continue
        require(path.is_file() and not path.is_symlink(),
                "parent contains an unsafe item artifact")
        relative = path.relative_to(parent)
        write_bytes_once(root / relative, path.read_bytes())
        carried[str(relative)] = sha256(path)
    evidence_names = ("config.json", "run_config.json", "completion.json",
                      "t2ance-normalization-manifest.json", "selection.json",
                      "items.json", "code_origin.json")
    parent_evidence = {}
    for name in evidence_names:
        path = parent / name
        write_bytes_once(root / "parent-evidence" / name, path.read_bytes())
        parent_evidence[name] = sha256(path)
    write_once(root / "config.json", new_config.to_dict())
    write_once(root / "budget-continuation-manifest.json", {
        "protocol": "t2ance-v4-budget-continuation-v1",
        "parent": str(parent), "carried_files": carried,
        "parent_evidence": parent_evidence,
        "parent_attempts": completion["summary"]["api_attempts"],
        "new_max_calls": max_calls,
        "new_max_reserved_tokens": max_reserved_tokens,
        "prior_calls": prior_calls,
        "prior_reserved_tokens": prior_reserved_tokens,
        "no_semantic_resampling": True,
    })
    verify_prepared(root, new_config)
    return {"selected": len(items), "carried_attempts":
            completion["summary"]["api_attempts"],
            "carried_files": len(carried), "root": str(root)}


def audit_continuation(root):
    root = Path(root)
    manifest = read_json(root / "budget-continuation-manifest.json")
    require(manifest["protocol"] == "t2ance-v4-budget-continuation-v1"
            and manifest["no_semantic_resampling"],
            "wrong continuation protocol")
    for name, expected in manifest["parent_evidence"].items():
        require(sha256(root / "parent-evidence" / name) == expected,
                "parent evidence changed")
    for name, expected in manifest["carried_files"].items():
        require(sha256(root / name) == expected,
                "carried request, response or result changed")
    require(Config.load(root / "config.json").max_calls == manifest["new_max_calls"],
            "continuation budget changed")
    report = audit_completed(root)
    return {**report, "continuation_protocol": manifest["protocol"],
            "carried_attempts": manifest["parent_attempts"]}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    for name in ("parent", "source", "execution", "expected-manifest"):
        parser.add_argument("--" + name, type=Path)
    for name in ("max-calls", "max-reserved-tokens", "prior-calls",
                 "prior-reserved-tokens"):
        parser.add_argument("--" + name, type=int)
    args = parser.parse_args()
    if args.audit:
        write_once(args.root / "offline-audit.json", audit_continuation(args.root))
        print(read_json(args.root / "offline-audit.json"))
        return
    require(all(getattr(args, name) is not None for name in (
        "parent", "source", "execution", "expected_manifest", "max_calls",
        "max_reserved_tokens", "prior_calls", "prior_reserved_tokens")),
        "all preparation arguments are required")
    print(prepare_continuation(args.parent, args.source, args.execution,
                               args.expected_manifest, args.root,
                               max_calls=args.max_calls,
                               max_reserved_tokens=args.max_reserved_tokens,
                               prior_calls=args.prior_calls,
                               prior_reserved_tokens=args.prior_reserved_tokens))


if __name__ == "__main__":
    main()
