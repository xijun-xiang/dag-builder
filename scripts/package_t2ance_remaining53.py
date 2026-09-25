"""Freeze all non-canary t2ance candidates for four isolated B1 CPU batches."""

import argparse
import json
import os
from pathlib import Path

from dag_builder.pipeline import implementation
from dag_builder.schemas import require
from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once
from dag_builder.t2ance_source import remaining_cpu_batch
from package_t2ance_canary7 import archive_package
from verify_livecodebench_reference import file_hash


def package(source, output):
    source = Path(source).resolve(strict=True)
    output = private_dir(output)
    repo = Path(__file__).resolve().parent.parent
    items = read_json(source / "items.json")
    manifest = read_json(source / "t2ance-manifest.json")
    require(manifest["protocol"] == "t2ance-lcb-source-v1"
            and digest(items) == manifest["items_sha256"], "frozen source changed")
    batches = [remaining_cpu_batch(items, index) for index in range(4)]
    selected = [item for batch in batches for item in batch]
    require(len(selected) == 53 and len({item["item_id"] for item in selected}) == 53,
            "remaining batch partition changed")
    origin = implementation()
    paths = []

    def copy(name, path):
        path = Path(path)
        require(path.is_file() and not path.is_symlink(), "missing or symlinked package source")
        write_bytes_once(output / name, path.read_bytes())
        paths.append(name)

    copy("source/items.json", source / "items.json")
    copy("source/t2ance-manifest.json", source / "t2ance-manifest.json")
    for item in selected:
        name = f"source/source-rows/{item['item_id']}.json"
        raw = source / "source-rows" / (item["item_id"] + ".json")
        require(digest(read_json(raw)) == item["origin"]["selected_columns_sha256"],
                "selected source row changed")
        copy(name, raw)
    for name in origin["source_files"]:
        copy("code/dag_builder/" + name, repo / "src/dag_builder" / name)
    write_once(output / "code/snapshot_origin.json", origin)
    paths.append("code/snapshot_origin.json")
    for name in ("prepare_t2ance_full_execution.py", "verify_livecodebench_reference.py",
                 "b1_verify_t2ance_remaining53.sbatch"):
        copy("scripts/" + name, repo / "scripts" / name)
    write_once(output / "package-manifest.json", {
        "protocol": "t2ance-remaining53-package-v1", "source_candidates": len(items),
        "batch_ids": [[item["item_id"] for item in batch] for batch in batches],
        "git_commit": origin["git_commit"],
        "sbatch": "b1_verify_t2ance_remaining53.sbatch",
        "files": {name: file_hash(output / name) for name in sorted(paths)},
        "claim": "53 previously untested candidates; no CPU or DAG result yet"})
    return {"batches": [len(batch) for batch in batches], "files": len(paths),
            "manifest_sha256": file_hash(output / "package-manifest.json")}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    result = package(args.source, args.output)
    result.update(archive_package(args.output, args.archive))
    print(json.dumps(result, ensure_ascii=False))
