#!/usr/bin/env python3
"""Materialize a deterministic 100-record GSM8K DAG candidate cohort.

The fixed comparison sample is preferred.  When an original row has no
model-accepted DAG from any declared source, this script deterministically adds
an already accepted, non-overlapping row from the full run.  It never rewrites a
source question, gold answer, DAG, or failure result.
"""

import argparse
import hashlib
import html
import json
from pathlib import Path

from dag_builder.storage import digest, private_dir, read_json, write_bytes_once, write_once


def accepted_dag(root, item_id):
    directory = root / "items" / item_id
    result_path, dag_path = directory / "result.json", directory / "dag.json"
    if not result_path.exists() or not dag_path.exists():
        return None
    result = read_json(result_path)
    if result.get("status") != "model_accepted":
        return None
    dag = read_json(dag_path)
    if dag.get("item_id") != item_id:
        raise ValueError("DAG item identifier mismatch")
    return {"dag": dag, "dag_sha256": result.get("dag_sha256")}


def choose_primary(item_id, candidates):
    for source_name, root in candidates:
        found = accepted_dag(root, item_id)
        if found is not None:
            return {"source_name": source_name, "source_root": str(root), **found}
    return None


def rank(seed, item_id):
    return hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-items", required=True, type=Path)
    parser.add_argument("--full", required=True, type=Path)
    parser.add_argument("--conditioned", required=True, type=Path)
    parser.add_argument("--official", required=True, type=Path)
    parser.add_argument("--canonical-initial", required=True, type=Path)
    parser.add_argument("--canonical-recovery", required=True, type=Path)
    parser.add_argument("--diagnostic-initial", required=True, type=Path)
    parser.add_argument("--diagnostic-individual-base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-count", type=int, default=100)
    parser.add_argument("--seed", default="gsm8k-dag-cohort-20260910")
    args = parser.parse_args()
    if args.target_count <= 0:
        raise ValueError("target count must be positive")

    fixed_items = read_json(args.fixed_items)
    fixed_ids = {item["item_id"] for item in fixed_items}
    full = args.full.resolve()
    shared_candidates = [
        ("independent_generation", full),
        ("answer_conditioned_generation", args.conditioned.resolve()),
        ("official_rationale", args.official.resolve()),
        ("canonical_official_initial", args.canonical_initial.resolve()),
        ("canonical_official_recovery", args.canonical_recovery.resolve()),
    ]
    records, unresolved = [], []
    for item in fixed_items:
        item_id = item["item_id"]
        per_item_candidates = list(shared_candidates)
        per_item_candidates.extend(
            [
                ("diagnostic_repair_initial", args.diagnostic_initial.resolve()),
                (
                    "diagnostic_repair_isolated",
                    (args.diagnostic_individual_base / item_id).resolve(),
                ),
            ]
        )
        selected = choose_primary(item_id, per_item_candidates)
        if selected is None:
            unresolved.append(item_id)
            continue
        records.append(
            {
                "cohort_role": "fixed_sample",
                "item_id": item_id,
                "source_name": selected["source_name"],
                "source_root": selected["source_root"],
                "source_dag_sha256": selected["dag_sha256"],
                "dag": selected["dag"],
            }
        )
    if len(records) > args.target_count:
        raise ValueError("target count is smaller than accepted fixed cohort")

    needed = args.target_count - len(records)
    full_items = read_json(full / "items.json")
    replacements = []
    for item in full_items:
        item_id = item["item_id"]
        if item_id in fixed_ids:
            continue
        selected = accepted_dag(full, item_id)
        if selected is not None:
            replacements.append((rank(args.seed, item_id), item_id, selected))
    replacements.sort()
    if len(replacements) < needed:
        raise ValueError("insufficient accepted full-run replacements")
    for _, item_id, selected in replacements[:needed]:
        records.append(
            {
                "cohort_role": "deterministic_replacement",
                "item_id": item_id,
                "source_name": "independent_generation_full_run",
                "source_root": str(full),
                "source_dag_sha256": selected["dag_sha256"],
                "replaces_unresolved_fixed_item_id": unresolved.pop(0),
                "selection_rank": rank(args.seed, item_id),
                "dag": selected["dag"],
            }
        )
    if unresolved:
        raise ValueError("replacement accounting mismatch")
    if len({record["item_id"] for record in records}) != len(records):
        raise ValueError("duplicate cohort item")

    root = private_dir(args.output)
    write_bytes_once(
        root / "cohort.jsonl",
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records).encode("utf-8"),
    )
    manifest = {
        "schema_version": "gsm8k_model_reviewed_dag_cohort_v1",
        "target_count": args.target_count,
        "actual_count": len(records),
        "fixed_item_count": len(fixed_items),
        "accepted_fixed_item_count": len(records) - needed,
        "replacement_count": needed,
        "selection_seed": args.seed,
        "replacement_policy": "lowest SHA256(seed,item_id) among full-run model_accepted rows not in fixed sample",
        "fixed_unresolved_item_ids": [
            record["replaces_unresolved_fixed_item_id"]
            for record in records
            if record["cohort_role"] == "deterministic_replacement"
        ],
        "records_sha256": digest(records),
        "quality_status": "model_reviewed_pending_human_review",
        "boundary": "This is not a human-approved or independently mathematically verified release.",
    }
    write_once(root / "manifest.json", manifest)
    rows = "".join(
        "<tr><td>" + str(index) + "</td><td>" + html.escape(record["item_id"])
        + "</td><td>" + html.escape(record["cohort_role"])
        + "</td><td>" + html.escape(record["source_name"])
        + "</td><td>" + html.escape(record.get("replaces_unresolved_fixed_item_id", "")) + "</td></tr>"
        for index, record in enumerate(records, 1)
    )
    page = """<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">
<title>GSM8K 100 条 DAG 候选队列</title><style>body{max-width:1300px;margin:30px auto;padding:0 18px;font:15px/1.5 sans-serif}table{border-collapse:collapse;width:100%}th,td{border:1px solid #c9cdd2;padding:7px;text-align:left}th{background:#f1f3f4}</style>
<h1>GSM8K 100 条 DAG 候选队列</h1><p>100 条均已有 model_accepted DAG。93 条来自固定样本，7 条是按固定哈希规则从全量运行中补入的非重叠样本。所有记录仍待人工审核。</p>
<table><tr><th>#</th><th>item_id</th><th>角色</th><th>来源</th><th>替代的固定样本</th></tr>""" + rows + "</table></html>"
    write_bytes_once(root / "review.html", page.encode("utf-8"))
    print(json.dumps({"output": str(root), **manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
