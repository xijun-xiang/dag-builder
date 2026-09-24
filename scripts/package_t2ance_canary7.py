"""Freeze the exact seven-question CPU regression payload for B1.

This only copies data and code; it never executes candidate programs.
"""

import argparse
import hashlib
import io
import os
import tarfile
from pathlib import Path

from dag_builder.pipeline import implementation
from dag_builder.schemas import require
from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once
from dag_builder.t2ance_source import canary_seven
from verify_livecodebench_reference import file_hash


SBATCH_SCRIPTS = frozenset({"b1_verify_t2ance_canary7.sbatch",
                            "b1_verify_t2ance_canary7_clean.sbatch"})


def archive_package(package_root, archive_path):
    """Archive only manifest-listed files, never macOS metadata or stray files."""
    package_root = Path(package_root).resolve(strict=True)
    manifest = read_json(package_root / "package-manifest.json")
    require(manifest["protocol"] == "t2ance-canary7-package-v1", "unknown package")
    names = sorted((*manifest["files"], "package-manifest.json"))
    require(len(names) == len(set(names)) and all(
        name and not name.startswith("/") and all(part not in ("", ".", "..")
        for part in Path(name).parts) for name in names), "unsafe package filename")
    directories = sorted({str(parent) for name in names for parent in
                          list(Path(name).parents)[:-1]}, key=lambda value: (value.count("/"), value))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in directories:
            entry = tarfile.TarInfo(name + "/")
            entry.type, entry.mode, entry.mtime = tarfile.DIRTYPE, 0o700, 0
            archive.addfile(entry)
        for name in names:
            data = (package_root / name).read_bytes()
            if name in manifest["files"]:
                require(hashlib.sha256(data).hexdigest() == manifest["files"][name],
                        "package file changed before archive")
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
            archive.addfile(entry, io.BytesIO(data))
    payload = buffer.getvalue()
    write_bytes_once(archive_path, payload)
    return {"archive_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_bytes": len(payload), "archive_files": len(names)}


def package(source, output, *, sbatch_name="b1_verify_t2ance_canary7.sbatch"):
    require(sbatch_name in SBATCH_SCRIPTS, "unknown canary job script")
    source = Path(source).resolve(strict=True)
    output = private_dir(output)
    repo = Path(__file__).resolve().parent.parent
    items = read_json(source / "items.json")
    source_manifest = read_json(source / "t2ance-manifest.json")
    require(source_manifest["protocol"] == "t2ance-lcb-source-v1"
            and digest(items) == source_manifest["items_sha256"],
            "frozen t2ance source changed")
    selected = canary_seven(items)
    origin = implementation()
    paths = []

    def copy(destination, original):
        original = Path(original)
        require(original.is_file() and not original.is_symlink(),
                "source file missing or symlinked")
        write_bytes_once(output / destination, original.read_bytes())
        paths.append(destination)

    copy("source/items.json", source / "items.json")
    copy("source/t2ance-manifest.json", source / "t2ance-manifest.json")
    for item in selected:
        relative = f"source/source-rows/{item['item_id']}.json"
        raw = source / "source-rows" / (item["item_id"] + ".json")
        require(digest(read_json(raw)) == item["origin"]["selected_columns_sha256"],
                "selected raw source row changed")
        copy(relative, raw)
    for name in origin["source_files"]:
        copy("code/dag_builder/" + name, repo / "src/dag_builder" / name)
    write_once(output / "code/snapshot_origin.json", origin)
    paths.append("code/snapshot_origin.json")
    for name in ("prepare_t2ance_full_execution.py", "verify_livecodebench_reference.py",
                 sbatch_name):
        copy("scripts/" + name, repo / "scripts" / name)
    write_once(output / "package-manifest.json", {
        "protocol": "t2ance-canary7-package-v1", "source_candidates": len(items),
        "sample_ids": [item["item_id"] for item in selected],
        "held_without_cpu": len(items) - len(selected),
        "git_commit": origin["git_commit"],
        "sbatch": sbatch_name,
        "files": {path: file_hash(output / path) for path in sorted(paths)},
        "claim": "seven source candidates only; no CPU result or DAG acceptance yet"})
    return {"sample_ids": [item["item_id"] for item in selected],
            "files": len(paths), "manifest_sha256": file_hash(output / "package-manifest.json")}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sbatch-name", choices=sorted(SBATCH_SCRIPTS),
                        default="b1_verify_t2ance_canary7.sbatch")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    import json
    result = package(args.source, args.output, sbatch_name=args.sbatch_name)
    if args.archive:
        result.update(archive_package(args.output, args.archive))
    print(json.dumps(result, ensure_ascii=False))
