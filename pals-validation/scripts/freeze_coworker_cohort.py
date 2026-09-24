#!/usr/bin/env python3
"""Freeze score-blind E1/E2 cohorts from the immutable coworker DAG audit.

This script selects whole, unchanged unified records.  It does not infer edges,
rewrite steps, or promote model review to human approval.  Explicit exclusions
must be documented before any PALS score is observed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PACKAGES = ("gsm8k", "mmlu_math", "mmlu_psych_social")
EXPERIMENTS = ("e1", "e2")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonl(rows: list[dict]) -> bytes:
    return b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode("utf-8") + b"\n"
        for row in rows
    )


def read_jsonl(data: bytes) -> list[dict]:
    lines = data.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError("Empty or blank JSONL row")
    return [json.loads(line) for line in lines]


def read_verified(audit_dir: Path, manifest: dict, name: str) -> list[dict]:
    data = (audit_dir / name).read_bytes()
    if sha(data) != manifest["outputs_sha256"][name]:
        raise ValueError(f"Audit output hash mismatch: {name}")
    return read_jsonl(data)


def freeze(audit_dir: Path, decisions_path: Path, output_dir: Path) -> dict:
    audit_raw = (audit_dir / "manifest.json").read_bytes()
    audit = json.loads(audit_raw)
    if (audit.get("protocol") != "coworker-dag-conservative-audit-v1"
            or audit.get("delivered_total") != 1539):
        raise ValueError("Unexpected audit protocol or denominator")
    flow = read_verified(audit_dir, audit, "flow_1539.jsonl")
    if len(flow) != 1539 or len({row["item_id"] for row in flow}) != len(flow):
        raise ValueError("Incomplete or duplicate flow")
    flow_by_id = {row["item_id"]: row for row in flow}
    raw_decisions = decisions_path.read_bytes()
    decisions = json.loads(raw_decisions)
    if set(decisions) != {"protocol", "excluded_source_ids"} or decisions["protocol"] != "score_blind_semantic_exclusions_v1":
        raise ValueError("Unknown decision format")
    exclusions = decisions["excluded_source_ids"]
    if not isinstance(exclusions, dict) or any(
        not isinstance(source_id, str) or not isinstance(reason, str) or not reason.strip()
        for source_id, reason in exclusions.items()
    ):
        raise ValueError("Invalid exclusion reasons")
    source_to_item = {row["source_id"]: row["item_id"] for row in flow}
    if len(source_to_item) != len(flow) or set(exclusions) - set(source_to_item):
        raise ValueError("Unknown or duplicate source ID in decisions")

    candidates = {}
    for experiment in EXPERIMENTS:
        name = f"{experiment}_mechanical_candidates.jsonl"
        rows = read_verified(audit_dir, audit, name)
        if len({row["item_id"] for row in rows}) != len(rows):
            raise ValueError(f"Duplicate candidate in {name}")
        expected_flag = ("e1_fair_pair_mechanical" if experiment == "e1"
                         else "e2_anchor_mechanical")
        for row in rows:
            item = row["item_id"]
            if item not in flow_by_id or not flow_by_id[item][expected_flag]:
                raise ValueError(f"Candidate not certified by flow: {item}")
            if flow_by_id[item]["record_sha256"] != sha(
                (json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2,
                            allow_nan=False) + "\n").encode("utf-8")
            ):
                raise ValueError(f"Candidate content differs from audited row: {item}")
        candidates[experiment] = rows

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    files: dict[str, dict] = {}
    included: dict[str, set[str]] = {}
    for experiment in EXPERIMENTS:
        included[experiment] = set()
        for package in PACKAGES:
            rows = [row for row in candidates[experiment]
                    if flow_by_id[row["item_id"]]["package"] == package
                    and flow_by_id[row["item_id"]]["source_id"] not in exclusions]
            if not rows:
                raise ValueError(f"Empty cohort: {experiment}/{package}")
            data = jsonl(rows)
            relative = f"{experiment}-{package}.jsonl"
            with (output_dir / relative).open("xb") as stream:
                stream.write(data)
            files[relative] = {"records": len(rows), "sha256": sha(data)}
            included[experiment].update(row["item_id"] for row in rows)

    flow_out = []
    for row in flow:
        source_id, item = row["source_id"], row["item_id"]
        flow_out.append({
            **row,
            "e1_in_frozen_cohort": item in included["e1"],
            "e2_in_frozen_cohort": item in included["e2"],
            "score_blind_exclusion": exclusions.get(source_id),
        })
    data = jsonl(flow_out)
    with (output_dir / "flow_1539.jsonl").open("xb") as stream:
        stream.write(data)
    files["flow_1539.jsonl"] = {"records": len(flow_out), "sha256": sha(data)}
    result = {
        "protocol": "coworker-pals-frozen-synthetic-cohort-v1",
        "scope": "model-accepted synthetic DAGs; no semantic graph rewrite or human-gold claim",
        "source_zip_sha256": audit["source_zip_sha256"],
        "audit_manifest_sha256": sha(audit_raw),
        "decision_sha256": sha(raw_decisions),
        "source_delivered": len(flow),
        "source_total_denominator": audit.get("source_total_denominator"),
        "files": files,
        "exclusions": exclusions,
        "selection_seed": audit["selection_seed"],
    }
    with (output_dir / "manifest.json").open("xb") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                indent=2, allow_nan=False).encode("utf-8") + b"\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.audit_dir, args.decisions, args.output),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
