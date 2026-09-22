"""Prepare the frozen full candidate cohort using the existing B1 source file.

No candidate execution. Run this data-processing step on a CPU allocation, not
the login node. Private test decoding uses the non-executing JSON/pickle parser.
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
from verify_livecodebench_reference import POLICY, file_hash, static_check


def prepare(candidates, manifest_path, source_file, output):
    items, manifest = read_json(candidates), read_json(manifest_path)
    require(manifest["protocol"] == "calibri-lcb-source-full-v1"
            and digest(items) == manifest["items_sha256"], "full candidate identity changed")
    require(0 < len(items) <= 175 and len({i["item_id"] for i in items}) == len(items), "candidate inventory invalid")
    require(file_sha256(source_file) == SOURCE_SHA256, "original v6 source changed")
    with Path(source_file).open(encoding="utf-8") as stream:
        original_rows = [json.loads(line) for line in stream if line.strip()]
    originals, bundles = normalize_livecodebench(original_rows, REVISION)
    require(len(originals) == 175 and digest(originals) == manifest["full_cohort_sha256"],
            "CALIBRI cohort differs from original v6")
    by_id = {i["item_id"]: i for i in originals}
    rows = []
    for item in items:
        original = by_id[item["item_id"]]
        require(all(item[k] == original[k] for k in (
            "question", "question_id", "platform", "io_type", "entry_point", "starter_code",
            "source_content_sha256", "tests_sha256")), "source identity changed")
        bundle = bundles[item["item_id"]]
        require(digest(bundle) == item["tests_sha256"], "tests changed")
        static_check(item["reference_code"])
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
            "entry_point": item["entry_point"], "code": item["reference_code"],
            "code_sha256": digest(item["reference_code"]),
            "source_content_sha256": item["source_content_sha256"], "tests_sha256": item["tests_sha256"],
            "tests": decode_tests(bundle, item["io_type"])})
    root = private_dir(output)
    harness = Path(__file__).with_name("verify_livecodebench_reference.py")
    write_once(root / "execution-input.json", rows)
    write_bytes_once(root / "execution-input.json.gz",
        gzip.compress((root / "execution-input.json").read_bytes(), mtime=0))
    write_bytes_once(root / harness.name, harness.read_bytes())
    write_once(root / "input-manifest.json", {
        "protocol": POLICY, "preparation_protocol": "calibri-full-execution-v1",
        "reference_origin": "calibri_model_output", "source_sha256": SOURCE_SHA256,
        "calibri_manifest_sha256": digest(manifest), "selected": len(originals), "planned": len(rows),
        "not_executed": [{"item_id": i["item_id"], "reason": "no_source_candidate"}
                         for i in originals if i["item_id"] not in {c["item_id"] for c in items}],
        "inputs_sha256": digest(rows), "harness_sha256": file_hash(harness),
        "prepare_script_sha256": file_hash(__file__), "implementation": implementation(),
        "comparison": "exact JSON equality / whitespace-normalized lines; no float tolerance",
        "claim": "pending isolated execution; not accepted DAGs or benchmark leaderboard scores"})
    return {"original_questions": len(originals), "candidates": len(items),
            "test_count": sum(len(r["tests"]) for r in rows),
            "input_manifest_sha256": file_hash(root / "input-manifest.json")}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("candidates", "manifest", "source-file", "output"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.candidates, args.manifest, args.source_file, args.output)), flush=True)
