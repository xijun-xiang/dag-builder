"""Continue unfinished repair items in a new run with an inherited budget.

The source must be stopped: its exclusive run lock is acquired before reading.
Terminal semantic results and exhausted retry windows are never selected again.
Unknown requests are copied and still consume an attempt and reserved budget.
"""

import hashlib
from dataclasses import replace
from pathlib import Path

from .config import Config
from .run_status import paused_items
from .storage import (
    digest,
    private_dir,
    read_json,
    run_lock,
    write_bytes_once,
    write_once,
)


def prepare_continuation(source, destination, workers=6):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError("continuation requires a separate sibling run")
    if destination.exists():
        raise FileExistsError("continuation destination already exists")
    with run_lock(source):
        original = Config.load(source / "config.json")
        if original.prompt_version != "gpqa-repair-v1":
            raise ValueError("only the one-round GPQA repair protocol is supported")
        pauses = paused_items(source)
        items = [
            item
            for item in read_json(source / "items.json")
            if not (source / "items" / item["item_id"] / "result.json").exists()
            and item["item_id"] not in pauses
        ]
        if not items:
            raise ValueError("no unfinished items; do not restart completed work")
        ids = {item["item_id"] for item in items}
        requests = list(source.glob("items/*/*/attempt-*/request.json"))
        excluded = [p for p in requests if p.relative_to(source).parts[1] not in ids]
        prior_calls = len(excluded)
        prior_reserved = sum(read_json(p)["reserved_tokens"] for p in excluded)
        config = replace(
            original,
            workers=workers,
            max_calls=original.max_calls - prior_calls,
            max_reserved_tokens=original.max_reserved_tokens - prior_reserved,
        )
        selection = read_json(source / "selection.json")
        selection = dict(
            selection,
            selected_ids=[i["item_id"] for i in items],
            selected_count=len(items),
        )
        for field in ("baseline_sha256", "selected_item_sha256"):
            selection[field] = {
                key: value for key, value in selection[field].items() if key in ids
            }
        for item in items:
            if digest(item) != selection["selected_item_sha256"][item["item_id"]]:
                raise ValueError("selected item integrity mismatch")
            if (
                digest(read_json(source / "items" / item["item_id"] / "baseline.json"))
                != selection["baseline_sha256"][item["item_id"]]
            ):
                raise ValueError("baseline integrity mismatch")
        private_dir(destination)
        copied = {}
        for item in items:
            for path in (source / "items" / item["item_id"]).rglob("*.json"):
                if path.is_symlink():
                    raise ValueError("source artifact symlinks are not allowed")
                relative = path.relative_to(source)
                content = path.read_bytes()
                write_bytes_once(destination / relative, content)
                # Storage uses canonical JSON hashes; preserve the raw bytes too.
                copied[str(relative)] = hashlib.sha256(content).hexdigest()
        write_once(destination / "items.json", items)
        write_once(destination / "selection.json", selection)
        write_once(destination / "config.json", config.to_dict())
        write_once(
            destination / "continuation.json",
            {
                "source_root": str(source),
                "selected_ids": [i["item_id"] for i in items],
                "source_config": original.to_dict(),
                "source_selection_sha256": digest(read_json(source / "selection.json")),
                "source_file_sha256": copied,
                "prior_calls_outside_continuation": prior_calls,
                "prior_reserved_tokens_outside_continuation": prior_reserved,
                "imported_attempts": len(requests) - prior_calls,
                "scope": "Unfinished items only; no extra semantic round or retry window. Imported requests count toward the remaining budget.",
            },
        )
        return {
            "selected": len(items),
            "workers": config.workers,
            "config": config.to_dict(),
        }
