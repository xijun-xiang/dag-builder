"""Independently rebuild t2ance CPU inputs from frozen source and original v6."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from dag_builder.livecodebench_dag import sha256, verify_execution
from dag_builder.livecodebench_source import REVISION, SOURCE_SHA256, normalize_livecodebench
from dag_builder.livecodebench_tests import decode_tests
from dag_builder.schemas import parse_object, require
from dag_builder.storage import digest, read_json, write_once
from dag_builder.t2ance_source import canary_seven, remaining_cpu_batch


def expected_rows(items, originals, bundles):
    by_id = {i["item_id"]: i for i in originals}
    rows = []
    for item in items:
        original = by_id[item["item_id"]]
        require(all(item[k] == original[k] for k in (
            "question", "question_id", "io_type", "entry_point", "starter_code",
            "source_content_sha256", "tests_sha256")), "candidate differs from original v6")
        tests = bundles[item["item_id"]]
        require(digest(tests) == item["tests_sha256"], "original tests changed")
        rows.append({"item_id": item["item_id"], "io_type": item["io_type"],
                     "entry_point": item["entry_point"], "code": item["reference_code"],
                     "code_sha256": digest(item["reference_code"]),
                     "source_content_sha256": item["source_content_sha256"],
                     "tests_sha256": item["tests_sha256"],
                     "tests": decode_tests(tests, item["io_type"])})
    return rows


def audit(execution, source, raw_source, frozen_package):
    require(sha256(raw_source) == SOURCE_SHA256, "raw v6 source changed")
    with Path(raw_source).open(encoding="utf-8") as stream:
        originals, bundles = normalize_livecodebench(
            [parse_object(line) for line in stream if line.strip()], REVISION)
    items = read_json(source / "items.json")
    manifest = read_json(source / "t2ance-manifest.json")
    require(manifest["protocol"] == "t2ance-lcb-source-v1"
            and len(originals) == 175 and digest(originals) == manifest["full_cohort_sha256"]
            and digest(items) == manifest["items_sha256"], "frozen source changed")
    require(read_json(frozen_package / "source/items.json") == items
            and read_json(frozen_package / "source/t2ance-manifest.json") == manifest,
            "deployed package changed")
    returned = read_json(execution / "input-manifest.json")
    protocol = returned["preparation_protocol"]
    require(protocol in ("t2ance-full-execution-v1", "t2ance-canary7-execution-v1",
                         "t2ance-remaining53-batch-v1"),
            "unknown CPU preparation protocol")
    selected = (remaining_cpu_batch(items, returned["remaining_batch"])
                if protocol == "t2ance-remaining53-batch-v1" else
                canary_seven(items) if protocol == "t2ance-canary7-execution-v1" else items)
    require(returned.get("remaining_batch") == (
        returned["remaining_batch"] if protocol == "t2ance-remaining53-batch-v1" else None),
        "unexpected CPU batch metadata")
    for item in selected:
        filename = item["item_id"] + ".json"
        deployed = read_json(frozen_package / "source/source-rows" / filename)
        require(deployed == read_json(source / "source-rows" / filename)
                and digest(deployed) == item["origin"]["selected_columns_sha256"],
                "deployed raw source row changed")
    rows = expected_rows(selected, originals, bundles)
    origin = read_json(frozen_package / "code/snapshot_origin.json")
    require(returned["implementation"]["source_files"] == origin["source_files"]
            and returned["implementation"]["git_commit"] == origin["git_commit"]
            and returned["harness_sha256"] == sha256(frozen_package / "scripts/verify_livecodebench_reference.py")
            and returned["prepare_script_sha256"] == sha256(
                frozen_package / "scripts/prepare_t2ance_full_execution.py"),
            "deployed implementation changed")
    require(returned["source_sha256"] == SOURCE_SHA256
            and returned["t2ance_manifest_sha256"] == digest(manifest)
            and returned["inputs_sha256"] == digest(rows)
            and read_json(execution / "execution-input.json") == rows
            and returned["selected"] == 175 and returned["source_candidates"] == len(items)
            and returned["planned"] == len(selected)
            and returned["sample_ids"] == [item["item_id"] for item in selected]
            and returned["held_without_cpu"] == [item["item_id"] for item in items
                                                  if item not in selected],
            "CPU input differs from independent reconstruction")
    results, completion = verify_execution(execution, execution / "input-manifest.json")
    by_id = {i["item_id"]: i for i in selected}
    failures = []
    for item_id, (_, result) in results.items():
        if result["status"] != "passed":
            failures.append({"item_id": item_id, "question_id": by_id[item_id]["question_id"],
                             "counts": result["counts"]})
    return {"protocol": "t2ance-execution-offline-audit-v1", "mechanical_pass": True,
            "job_id": completion["job_id"], "hostname": completion["hostname"],
            "original_questions": len(originals), "source_candidates": len(items),
            "cpu_sampled": len(selected), "held_without_cpu": len(items) - len(selected),
            "reference_passed": completion["passed"], "reference_not_passed": len(failures),
            "tests": sum(len(r["tests"]) for r in rows),
            "test_statuses": dict(Counter(t["status"] for _, r in results.values()
                                          for t in r["tests"])),
            "failures": failures, "input_manifest_sha256": sha256(execution / "input-manifest.json"),
            "source_sha256": SOURCE_SHA256, "dag_semantic_acceptance": False}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("execution", "source", "raw-source", "frozen-package"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.execution, args.source, args.raw_source, args.frozen_package)
    write_once(args.execution / "offline-audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
