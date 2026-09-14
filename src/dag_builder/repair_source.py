"""Freeze terminal semantic failures into a separate one-round repair cohort."""

import fcntl
import hashlib
import os
import re
from pathlib import Path

from .schemas import InvalidOutput, parse_object, require
from .stages import REFERENCE_STAGES
from .storage import digest, private_dir, read_json, write_bytes_once, write_once


def baseline_for(directory):
    """Expose recorded content, never invent a successful stage after failure."""
    outputs, failed = {}, {}
    for stage in REFERENCE_STAGES:
        output = directory / stage / "output.json"
        if output.exists():
            outputs[stage] = read_json(output)
            continue
        for response in sorted((directory / stage).glob("attempt-*/response.json")):
            choices = read_json(response)["body"].get("choices", [])
            if len(choices) != 1:
                failed[stage] = {"error": "ambiguous completion"}
                break
            content = choices[0].get("message", {}).get("content")
            failed[stage] = {
                "finish_reason": choices[0].get("finish_reason"),
                "content": content,
            }
            if choices[0].get("finish_reason") == "stop":
                try:
                    failed[stage]["parsed"] = parse_object(content)
                except InvalidOutput:
                    pass
            # The first returned content is authoritative, not the best attempt.
            break
    return {
        "result": read_json(directory / "result.json"),
        "stage_outputs": outputs,
        "failed_completions": failed,
    }


def prepare_repair(root, source_root):
    source = Path(source_root).absolute()
    destination = Path(root).absolute()
    require(
        source.resolve() == source and destination.resolve() == destination,
        "symlinked repair path",
    )
    require(
        not destination.is_relative_to(source)
        and not source.is_relative_to(destination),
        "repair and source roots must be separate",
    )
    require(
        (source / "completion.json").is_file(),
        "source batch must finish before repair preparation",
    )
    # Shared read lock: never mutate source artifacts or race its active writer.
    fd = os.open(source / ".lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        config = read_json(source / "config.json")
        require(
            config["task_type"] == "gpqa"
            and config["prompt_version"] == "gpqa-reference-v1",
            "only first-pass GPQA runs can enter one-round repair",
        )
        items = read_json(source / "items.json")
        require(
            items
            and all(re.fullmatch(r"[0-9a-f]{20}", item["item_id"]) for item in items),
            "invalid source IDs",
        )
        require(
            len({item["item_id"] for item in items}) == len(items),
            "duplicate source IDs",
        )
        require(
            [item["item_id"] for item in items]
            == read_json(source / "selection.json")["selected_ids"],
            "source selection mismatch",
        )
        require(
            digest(items) == read_json(source / "inputs_manifest.json")["items_sha256"],
            "source cohort hash mismatch",
        )
        chosen, snapshots, excluded = [], {}, []
        for item in items:
            directory = source / "items" / item["item_id"]
            result = (
                read_json(directory / "result.json")
                if (directory / "result.json").exists()
                else {"status": "unfinished_transport_or_other"}
            )
            if result["status"] in ("needs_review", "rejected"):
                chosen.append(item)
                snapshots[item["item_id"]] = baseline_for(directory)
            else:
                excluded.append(
                    {"item_id": item["item_id"], "status": result["status"]}
                )
        require(bool(chosen), "no terminal semantic failures to repair")
        root = private_dir(destination)
        hashes = {}
        for item in chosen:
            directory = source / "items" / item["item_id"]
            for path in sorted(directory.rglob("*.json")):
                require(path.resolve() == path, "symlink in source artifacts")
                relative = Path("items") / item["item_id"] / path.relative_to(directory)
                data = path.read_bytes()
                hashes[str(relative)] = hashlib.sha256(data).hexdigest()
                write_bytes_once(root / "originals" / relative, data)
            write_once(
                root / "items" / item["item_id"] / "baseline.json",
                snapshots[item["item_id"]],
            )
        for name in (
            "items.json",
            "selection.json",
            "config.json",
            "completion.json",
            "inputs_manifest.json",
        ):
            data = (source / name).read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            write_bytes_once(root / "originals" / name, data)
        selection = {
            "protocol": "gpqa-repair-v1",
            "semantic_repair_round_limit": 1,
            "source_root": str(source),
            "source_selected_count": len(items),
            "selected_ids": [item["item_id"] for item in chosen],
            "selected_count": len(chosen),
            "selected_item_sha256": {item["item_id"]: digest(item) for item in chosen},
            "excluded": excluded,
            "baseline_sha256": {key: digest(value) for key, value in snapshots.items()},
            "original_file_sha256": hashes,
            "selection_rule": "all terminal needs_review/rejected records; exclude accepted and transport-incomplete records; no quality-based replenishment",
            "scope": "same-model one-round adjudication, not independent gold verification",
        }
        write_once(root / "items.json", chosen)
        write_once(root / "selection.json", selection)
        return selection
    finally:
        os.close(fd)
