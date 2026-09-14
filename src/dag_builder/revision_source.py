"""Freeze explicitly selected prior records for a new, separately budgeted run."""

import fcntl
import hashlib
import os
import re
from pathlib import Path

from .output_normalization import normalize
from .schemas import InvalidOutput, parse_object, require
from .storage import digest, private_dir, read_json, write_once


def seed_candidate(location):
    candidate, operations = None, []
    if (location / "dag.json").exists():
        dag = read_json(location / "dag.json")
        keys = ("node_id", "kind", "statement", "source_field", "source_quote")
        candidate = {
            "nodes": [{k: n[k] for k in keys} for n in dag["nodes"]],
            "parents": [
                {"node_id": n["node_id"], "parents": n["parents"]} for n in dag["nodes"]
            ],
            "justifications": [
                {"node_id": n["node_id"], "text": n["justification"]}
                for n in dag["nodes"]
            ],
        }
    elif (location / "candidate.json").exists():
        candidate = read_json(location / "candidate.json")
    elif (location / "atomize/output.json").exists():
        # First-pass failures can still have useful fixed nodes and partial edges.
        candidate = {
            "nodes": read_json(location / "atomize/output.json")["nodes"],
            "parents": read_json(location / "dependencies/output.json")["parents"]
            if (location / "dependencies/output.json").exists()
            else [],
        }
    else:
        for path in sorted((location / "repair").glob("attempt-*/response.json")):
            choices = read_json(path)["body"].get("choices", [])
            if len(choices) == 1 and choices[0].get("finish_reason") == "stop":
                try:
                    proposal = parse_object(choices[0]["message"]["content"])
                except (InvalidOutput, KeyError):
                    break
                proposal, operations = normalize(proposal)
                candidate = proposal.get("candidate")
            break  # First returned response, never select the most favorable.
    if candidate is not None:
        candidate, aliases = normalize(candidate)
        operations.extend(aliases)
        if "justifications" not in candidate:
            justification = location / "justify/output.json"
            candidate["justifications"] = (
                read_json(justification)["justifications"]
                if justification.exists()
                else []
            )
    return candidate, operations


def prepare_revision(root, entries, selection_note):
    destination = Path(root).absolute()
    require(destination.resolve() == destination, "symlinked destination")
    require(
        bool(entries) and len({e["item_id"] for e in entries}) == len(entries),
        "empty/duplicate selection",
    )
    items, baselines, provenance = [], {}, {}
    for entry in entries:
        item_id = entry["item_id"]
        require(re.fullmatch(r"[0-9a-f]{20}", item_id) is not None, "invalid item ID")
        source = Path(entry["source_root"]).absolute()
        require(
            source.resolve() == source
            and not destination.is_relative_to(source)
            and not source.is_relative_to(destination),
            "source and destination must be separate unsymlinked roots",
        )
        fd = os.open(source / ".lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            source_items = read_json(source / "items.json")
            item = next(i for i in source_items if i["item_id"] == item_id)
            require(
                item.get("task_type") == "gpqa", "only GPQA references are supported"
            )
            manifest = read_json(source / "inputs_manifest.json")
            require(
                digest(source_items) == manifest["items_sha256"],
                "source cohort hash mismatch",
            )
            location = source / "items" / item_id
            candidate, normalization = seed_candidate(location)
            result = (
                read_json(location / "result.json")
                if (location / "result.json").exists()
                else {
                    "status": "paused",
                    "reason": "no terminal result; preserved transport evidence",
                }
            )
            role = entry.get("role", "case")
            require(role in ("case", "control"), "invalid pilot role")
            if role == "control":
                require(
                    result["status"] in ("model_accepted", "repaired_model_accepted"),
                    "control must be a previously model-accepted record",
                )
            hashes = {}
            for path in sorted(location.rglob("*.json")):
                require(path.resolve() == path, "symlinked source evidence")
                hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            requests = [read_json(p) for p in location.glob("*/attempt-*/request.json")]
            baseline = {
                "candidate": candidate,
                "normalization": normalization,
                "role": role,
                "prior_reason": result.get("reason", ""),
                "prior_status": result["status"],
                "source_dispute": result["status"] == "repair_unrepairable"
                or (
                    candidate is None
                    and result.get("stage") == "review_solution"
                    and result["status"] in ("rejected", "needs_review")
                ),
                "origin": str(location),
                "evidence_sha256": hashes,
                "historical_requests": len(requests),
                "historical_reserved_tokens": sum(
                    r["reserved_tokens"] for r in requests
                ),
                "historical_accounting_note": "Immediate parent item only; historical calls are not charged to the separately capped new pilot and are never erased.",
            }
            items.append(item)
            baselines[item_id] = baseline
            provenance[item_id] = dict(entry, prior_status=result["status"])
        finally:
            os.close(fd)
    private_dir(destination)
    for item_id, baseline in baselines.items():
        write_once(destination / "items" / item_id / "baseline.json", baseline)
    selection = {
        "protocol": "gpqa-revision-v1",
        "max_additional_rounds": 2,
        "selected_ids": [i["item_id"] for i in items],
        "selected_count": len(items),
        "selected_item_sha256": {i["item_id"]: digest(i) for i in items},
        "baseline_sha256": {k: digest(v) for k, v in baselines.items()},
        "selection_rule": selection_note,
        "provenance": provenance,
        "no_automatic_expansion": True,
        "human_approved_count": 0,
    }
    write_once(destination / "items.json", items)
    write_once(destination / "selection.json", selection)
    return selection
