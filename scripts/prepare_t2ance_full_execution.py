"""Prepare t2ance candidates for the existing isolated B1 CPU reference gate.

Run on an allocated CPU node, not a login node: private test decoding is data
processing only, but it can be memory-intensive. No candidate code runs here.
"""

import argparse
import gzip
import json
import os
from pathlib import Path

from dag_builder.calibri_source import file_sha256
from dag_builder.livecodebench_source import REVISION, SOURCE_SHA256, normalize_livecodebench
from dag_builder.livecodebench_tests import decode_tests
from dag_builder.pipeline import implementation
from dag_builder.schemas import require
from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once
from dag_builder.t2ance_source import canary_seven, inspect_candidate
from verify_livecodebench_reference import POLICY, file_hash, static_check


def prepare(source, source_file, output, *, canary=False):
    source = Path(source)
    items, manifest = read_json(source / "items.json"), read_json(source / "t2ance-manifest.json")
    require(manifest["protocol"] == "t2ance-lcb-source-v1"
            and digest(items) == manifest["items_sha256"], "source selection changed")
    require(0 < len(items) <= 82 and len({i["item_id"] for i in items}) == len(items),
            "invalid source cohort")
    require(file_sha256(source_file) == SOURCE_SHA256
            and manifest["original_v6_sha256"] == SOURCE_SHA256,
            "original v6 source changed")
    with Path(source_file).open(encoding="utf-8") as stream:
        originals, bundles = normalize_livecodebench(
            [json.loads(line) for line in stream if line.strip()], REVISION)
    require(len(originals) == 175 and digest(originals) == manifest["full_cohort_sha256"],
            "original cohort changed")
    by_id = {i["item_id"]: i for i in originals}
    selected = canary_seven(items) if canary else items
    rows = []
    for item in selected:
        original = by_id[item["item_id"]]
        require(all(item[k] == original[k] for k in (
            "question", "question_id", "platform", "io_type", "entry_point", "starter_code",
            "source_content_sha256", "tests_sha256")), "selected item differs from pinned v6")
        origin = item["origin"]
        raw = read_json(source / "source-rows" / (item["item_id"] + ".json"))
        require(digest(raw) == origin["selected_columns_sha256"]
                and raw["solution_code"] == item["reference_code"]
                and raw["full_response"] == item["raw_output"]
                and inspect_candidate(raw, original, static_check)["candidate"],
                "selected t2ance response changed or became ineligible")
        bundle = bundles[item["item_id"]]
        require(digest(bundle) == item["tests_sha256"], "official tests changed")
        static_check(item["reference_code"])
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
                     "entry_point": item["entry_point"], "code": item["reference_code"],
                     "code_sha256": digest(item["reference_code"]),
                     "source_content_sha256": item["source_content_sha256"],
                     "tests_sha256": item["tests_sha256"],
                     "tests": decode_tests(bundle, item["io_type"])})
    root = private_dir(output)
    harness = Path(__file__).with_name("verify_livecodebench_reference.py")
    write_once(root / "execution-input.json", rows)
    write_bytes_once(root / "execution-input.json.gz",
        gzip.compress((root / "execution-input.json").read_bytes(), mtime=0))
    write_bytes_once(root / harness.name, harness.read_bytes())
    write_once(root / "input-manifest.json", {
        "protocol": POLICY, "preparation_protocol": (
            "t2ance-canary7-execution-v1" if canary else "t2ance-full-execution-v1"),
        "reference_origin": "t2ance_model_output", "source_sha256": SOURCE_SHA256,
        "t2ance_manifest_sha256": digest(manifest), "selected": len(originals),
        "source_candidates": len(items), "planned": len(rows),
        "sample_ids": [item["item_id"] for item in selected],
        "held_without_cpu": [item["item_id"] for item in items if item not in selected],
        "sample_policy": ("frozen SHA-256 seed within fixed I/O x difficulty quotas"
                          if canary else "all source candidates"),
        "inputs_sha256": digest(rows),
        "harness_sha256": file_hash(harness), "prepare_script_sha256": file_hash(__file__),
        "implementation": implementation(),
        "comparison": "exact JSON equality / whitespace-normalized lines; no float tolerance",
        "claim": "pending isolated execution; untested candidates are not CPU-verified DAGs or scores"})
    return {"original_questions": len(originals), "candidates": len(items),
            "planned": len(rows), "test_count": sum(len(r["tests"]) for r in rows),
            "input_manifest_sha256": file_hash(root / "input-manifest.json")}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "source-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--canary-seven", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.source_file, args.output,
                             canary=args.canary_seven)), flush=True)
