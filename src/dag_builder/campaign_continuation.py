"""Concurrency-only continuation while full campaign construction is active.

All construction item artifacts are inherited, including completed results,
failed attempts and unknown requests. Therefore the original phase caps remain
unchanged and Pipeline._restore_budget counts every imported request exactly once.
"""

import hashlib
from dataclasses import replace
from pathlib import Path

from .config import Config
from .storage import read_json, run_lock, write_bytes_once, write_once


def continue_construction(source, destination, workers=32):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.resolve() != source or destination.resolve() != destination:
        raise ValueError("symlinked campaign path")
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("use separate sibling campaigns")
    if destination.exists():
        raise FileExistsError("continuation destination already exists")
    with run_lock(source), run_lock(source / "construction"):
        if (source / "completion.json").exists() or (source / "revision").exists():
            raise ValueError("only construction-phase handoff is supported")
        config = Config.load(source / "construction/config.json")
        if config.prompt_version != "gpqa-reference-v1":
            raise ValueError("unexpected construction protocol")
        new_config = replace(config, workers=workers)
        revision = replace(
            Config.load(source / "revision-config.json"), workers=workers
        )
        copied = {}

        def copy(path):
            if path.is_symlink():
                raise ValueError("source artifact symlink")
            relative = path.relative_to(source)
            content = path.read_bytes()
            write_bytes_once(destination / relative, content)
            copied[str(relative)] = hashlib.sha256(content).hexdigest()

        for name in (
            "campaign.json",
            "items.json",
            "pilot_history.json",
            "construction/items.json",
            "construction/selection.json",
        ):
            copy(source / name)
        for directory in ("source", "construction/items", "construction/status_events"):
            for path in sorted((source / directory).rglob("*")):
                if path.is_file() and (directory == "source" or path.suffix == ".json"):
                    copy(path)
        requests = list(
            (source / "construction").glob("items/*/*/attempt-*/request.json")
        )
        unknown = [
            str(p.relative_to(source))
            for p in requests
            if not (p.parent / "response.json").exists()
            and not (p.parent / "error.json").exists()
        ]
        write_once(destination / "construction/config.json", new_config.to_dict())
        write_once(destination / "revision-config.json", revision.to_dict())
        manifest = {
            "source_root": str(source),
            "workers_before": config.workers,
            "workers_after": workers,
            "copied_sha256": copied,
            "imported_attempts": len(requests),
            "imported_reserved_tokens": sum(
                read_json(p)["reserved_tokens"] for p in requests
            ),
            "unknown_requests": unknown,
            "phase_call_caps_unchanged": True,
            "note": "Imported calls count inside unchanged phase caps. Unknown requests consume retry slots; no semantic outcomes or retry windows reset. Do not sum duplicate requests across source and continuation.",
        }
        write_once(destination / "continuation.json", manifest)
        return manifest
