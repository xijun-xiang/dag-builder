"""Freeze official-editorial code with the original LCB tests; never execute code."""
import argparse
import os
from pathlib import Path

from dag_builder.livecodebench_editorial import PROTOCOL, validate_editorial
from dag_builder.livecodebench_tests import decode_tests
from dag_builder.pipeline import implementation
from dag_builder.schemas import require
from dag_builder.storage import digest, private_dir, read_json, write_once
from verify_livecodebench_reference import POLICY, file_hash, static_check


def prepare(editorial, original, output):
    items = read_json(editorial / "items.json")
    manifest = read_json(editorial / "editorial-manifest.json")
    source = read_json(original / "prepared-manifest.json")
    source_items = read_json(original / "items.json")
    require(manifest["protocol"] == PROTOCOL and digest(items) == manifest["items_sha256"],
            "editorial snapshot changed")
    require(digest(source_items) == source["items_sha256"], "original source changed")
    originals = {item["item_id"]: item for item in source_items}
    rows = []
    for item in items:
        validate_editorial(item)
        previous = originals[item["item_id"]]
        require(all(item[k] == previous[k] for k in
                    ("question_id", "question", "platform", "io_type", "entry_point",
                     "source_content_sha256", "tests_sha256")), "editorial/source mismatch")
        bundle = read_json(original / "tests" / (item["item_id"] + ".json"))
        require(digest(bundle) == item["tests_sha256"], "test bundle changed")
        static_check(item["reference_code"])
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
                     "entry_point": item["entry_point"], "code": item["reference_code"],
                     "code_sha256": digest(item["reference_code"]),
                     "source_content_sha256": item["source_content_sha256"],
                     "tests_sha256": item["tests_sha256"],
                     "tests": decode_tests(bundle, item["io_type"])})
    require(bool(rows), "no official references")
    root = private_dir(output)
    write_once(root / "execution-input.json", rows)
    write_once(root / "input-manifest.json", {
        "protocol": POLICY, "reference_origin": "official_editorial",
        "editorial_manifest_sha256": digest(manifest),
        "source_prepared_manifest_sha256": digest(source),
        "selected": len(items), "planned": len(rows), "not_executed": [],
        "inputs_sha256": digest(rows),
        "harness_sha256": file_hash(Path(__file__).with_name("verify_livecodebench_reference.py")),
        "code_git_commit": implementation()["git_commit"],
        "comparison": "exact JSON equality / whitespace-normalized lines; no float tolerance",
        "claim": "official reference execution check, not official gold DAG certification"})
    return {"items": len(rows), "tests": sum(len(row["tests"]) for row in rows)}


if __name__ == "__main__":
    import json
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editorial", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.editorial, args.original, args.output)))
