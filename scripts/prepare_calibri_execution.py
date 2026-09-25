"""Bind CALIBRI candidates to original LCB tests; prepare only, never execute."""

import argparse
import gzip
import json
import os
from pathlib import Path

from dag_builder.calibri_source import inspect_row
from dag_builder.livecodebench_tests import decode_tests
from dag_builder.pipeline import implementation
from dag_builder.schemas import require
from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once
from verify_livecodebench_reference import POLICY, file_hash, static_check


def prepare(calibri, source, output):
    calibri, source, output = Path(calibri), Path(source), private_dir(output)
    manifest = read_json(calibri / "calibri-manifest.json")
    items = read_json(calibri / "items.json")
    selection = read_json(calibri / "selection.json")
    original_manifest = read_json(source / "prepared-manifest.json")
    original_items = read_json(source / "items.json")
    require(manifest["protocol"] == "calibri-lcb-source-v2", "wrong source protocol")
    require(digest(items) == manifest["items_sha256"]
            and digest(selection) == manifest["selection_sha256"],
            "CALIBRI selection changed")
    require(digest(original_manifest) == manifest["source_prepared_manifest_sha256"]
            and digest(original_items) == original_manifest["items_sha256"], "original cohort changed")
    by_id = {i["item_id"]: i for i in original_items}
    selected_ids = [i["item_id"] for i in items]
    require(len(original_items) == len(by_id) == 5, "original five-question canary required")
    require(len(selected_ids) == len(set(selected_ids))
            and selected_ids == selection["selected_ids"]
            and set(selected_ids) <= set(by_id), "candidate inventory mismatch")
    rows = []
    for item in items:
        origin = item["origin"]
        original = by_id[item["item_id"]]
        require(all(item[k] == original[k] for k in (
            "question", "question_id", "platform", "io_type", "entry_point", "starter_code",
            "source_content_sha256", "tests_sha256")), "CALIBRI source identity changed")
        require(origin["config"] in ("livecodebench_qwen3", "livecodebench_gpt-oss"), "unknown model config")
        raw = read_json(calibri / "source-rows" / origin["config"] / (item["item_id"] + ".json"))
        require(digest(raw) == origin["selected_columns_sha256"], "upstream row snapshot changed")
        n = origin["sample_index"]
        require(type(n) is int and 0 <= n < 10, "invalid source sample index")
        require(inspect_row(raw, original)[n]["candidate"] and raw["program"][n] == item["reference_code"]
                and raw["output"][n] == item["raw_output"], "selected sample differs from raw source")
        bundle = read_json(source / "tests" / (item["item_id"] + ".json"))
        require(digest(bundle) == item["tests_sha256"], "test bundle changed")
        static_check(item["reference_code"])
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
                     "entry_point": item["entry_point"], "code": item["reference_code"],
                     "code_sha256": digest(item["reference_code"]),
                     "source_content_sha256": item["source_content_sha256"],
                     "tests_sha256": item["tests_sha256"],
                     "tests": decode_tests(bundle, item["io_type"])})
    require(bool(rows) and len(rows) <= 5, "bounded nonempty original canary required")
    write_once(output / "execution-input.json", rows)
    harness = Path(__file__).with_name("verify_livecodebench_reference.py")
    write_bytes_once(output / harness.name, harness.read_bytes())
    write_bytes_once(output / "execution-input.json.gz",
                     gzip.compress((output / "execution-input.json").read_bytes(), mtime=0))
    write_once(output / "input-manifest.json", {
        "protocol": POLICY, "reference_origin": "calibri_model_output",
        "calibri_manifest_sha256": digest(manifest),
        "source_prepared_manifest_sha256": digest(original_manifest),
        "selected": 5, "planned": len(rows),
        "not_executed": read_json(calibri / "selection.json")["excluded"],
        "inputs_sha256": digest(rows), "harness_sha256": file_hash(harness),
        "prepare_script_sha256": file_hash(__file__), "implementation": implementation(),
        "comparison": "exact JSON equality / whitespace-normalized lines; no float tolerance",
        "claim": "pending isolated reference execution; not a PALS experiment or accepted DAG"})
    return {"original_canary": 5, "execution_candidates": len(rows),
            "tests": sum(len(row["tests"]) for row in rows)}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibri", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.calibri, args.source, args.output)))
