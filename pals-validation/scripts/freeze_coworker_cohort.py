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
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pals_validation.data import normalize  # noqa: E402
from pals_validation.graph import breaking, forest  # noqa: E402


PACKAGES = ("gsm8k", "mmlu_math", "mmlu_psych_social")
EXPERIMENTS = ("e1", "e2")
PRIMARY_E1_PARENT_KINDS = frozenset(("derived", "knowledge"))


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


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key in decisions: {key}")
        result[key] = value
    return result


def read_decisions(data: bytes) -> tuple[str, dict[str, dict[str, str]]]:
    """V1 applies one explicit exclusion to both arms; V2 names each arm."""
    decisions = json.loads(data, object_pairs_hook=_unique_json_pairs)
    if not isinstance(decisions, dict):
        raise ValueError("Invalid decision object")
    protocol = decisions.get("protocol")
    if protocol == "score_blind_semantic_exclusions_v1":
        if set(decisions) != {"protocol", "excluded_source_ids"}:
            raise ValueError("Unknown V1 decision fields")
        exclusions = {experiment: decisions["excluded_source_ids"]
                      for experiment in EXPERIMENTS}
    elif protocol == "score_blind_experiment_exclusions_v2":
        if set(decisions) != {"protocol", "excluded_source_ids"}:
            raise ValueError("Unknown V2 decision fields")
        exclusions = decisions["excluded_source_ids"]
        if not isinstance(exclusions, dict) or set(exclusions) != set(EXPERIMENTS):
            raise ValueError("V2 exclusions must name E1 and E2 separately")
    else:
        raise ValueError("Unknown decision format")
    for experiment in EXPERIMENTS:
        arm = exclusions[experiment]
        if not isinstance(arm, dict) or any(
            not isinstance(source_id, str) or not source_id.strip()
            or not isinstance(reason, str) or not reason.strip()
            for source_id, reason in arm.items()
        ):
            raise ValueError(f"Invalid {experiment.upper()} exclusion reasons")
    return protocol, exclusions


def e1_primary_parent(record: dict, seed: int) -> tuple[dict, bool, str]:
    """Replay the *existing* chosen E1 operator; never choose a new edge."""
    benchmark = "gsm8k" if record["benchmark"] == "gsm8k" else "mmlu"
    case = normalize(record, benchmark)
    steps = case["steps"]
    serial = forest(steps, seed, case["item_id"])
    selected = breaking(steps, serial["baseline"], seed,
                        case["item_id"], "forest_break")
    if serial["legal"] is None or selected is None:
        raise ValueError(f"Audited E1 candidate lost its selected fair pair: {case['item_id']}")
    if len(selected["violated_edges"]) != 1:
        raise ValueError(f"E1 selected operator does not invert one edge: {case['item_id']}")
    parent_id, target_id = selected["violated_edges"][0]
    parent = next(node for node in steps if node["node_id"] == parent_id)
    provenance = {"node_id": parent_id, "target_id": target_id,
                  "kind": parent["kind"], "source_field": parent["source_field"]}
    eligible = (parent["kind"] in PRIMARY_E1_PARENT_KINDS
                and parent["source_field"] == "solution")
    reason = ("selected_parent_solution_derived_or_knowledge" if eligible
              else "selected_parent_not_solution_derived_or_knowledge")
    return provenance, eligible, reason


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
    decision_protocol, exclusions = read_decisions(raw_decisions)
    source_to_item = {row["source_id"]: row["item_id"] for row in flow}
    if (len(source_to_item) != len(flow)
            or any(set(exclusions[arm]) - set(source_to_item) for arm in EXPERIMENTS)):
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
            if row.get("provenance", {}).get("source_id") != flow_by_id[item]["source_id"]:
                raise ValueError(f"Candidate source ID differs from audited flow: {item}")
            if flow_by_id[item]["record_sha256"] != sha(
                (json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2,
                            allow_nan=False) + "\n").encode("utf-8")
            ):
                raise ValueError(f"Candidate content differs from audited row: {item}")
        certified = {item for item, entry in flow_by_id.items() if entry[expected_flag]}
        if {row["item_id"] for row in rows} != certified:
            raise ValueError(f"Candidate set differs from certified flow: {name}")
        candidates[experiment] = rows

    candidate_by_id = {arm: {row["item_id"]: row for row in candidates[arm]}
                       for arm in EXPERIMENTS}
    e1_parent = {row["item_id"]: e1_primary_parent(row, audit["selection_seed"])
                 for row in candidates["e1"]}
    included_secondary = {row["item_id"] for row in candidates["e1"]
                          if flow_by_id[row["item_id"]]["source_id"] not in exclusions["e1"]}
    included = {
        "e1": {item for item in included_secondary if e1_parent[item][1]},
        "e2": {row["item_id"] for row in candidates["e2"]
               if flow_by_id[row["item_id"]]["source_id"] not in exclusions["e2"]},
    }
    outputs: dict[str, bytes] = {}
    for package in PACKAGES:
        for name, records in (("e1", candidates["e1"]),
                              ("e1-mechanical-secondary", candidates["e1"]),
                              ("e2", candidates["e2"])):
            selected_ids = included_secondary if name == "e1-mechanical-secondary" else included[name]
            rows = [row for row in records if row["item_id"] in selected_ids
                    and flow_by_id[row["item_id"]]["package"] == package]
            outputs[f"{name}-{package}.jsonl"] = jsonl(rows)

    flow_out = []
    for row in flow:
        source_id, item = row["source_id"], row["item_id"]
        parent, primary_eligible, primary_reason = e1_parent.get(
            item, (None, False, "not_e1_mechanical_candidate"))
        flow_out.append({
            **row,
            "e1_selected_break_parent": parent,
            "e1_primary_eligibility": primary_eligible,
            "e1_primary_eligibility_reason": primary_reason,
            "e1_score_blind_exclusion": exclusions["e1"].get(source_id),
            "e1_in_mechanical_secondary": item in included_secondary,
            "e1_in_frozen_cohort": item in included["e1"],
            "e2_score_blind_exclusion": exclusions["e2"].get(source_id),
            "e2_in_frozen_cohort": item in included["e2"],
            "e1_decision": ("not_mechanically_eligible" if item not in candidate_by_id["e1"]
                            else "semantic_exclusion" if source_id in exclusions["e1"]
                            else "primary" if item in included["e1"]
                            else "mechanical_secondary_only"),
            "e2_decision": ("not_mechanically_eligible" if item not in candidate_by_id["e2"]
                            else "semantic_exclusion" if source_id in exclusions["e2"]
                            else "included"),
        })
    outputs["flow_1539.jsonl"] = jsonl(flow_out)
    files = {name: {"records": len(read_jsonl(data)) if data else 0,
                    "sha256": sha(data)} for name, data in outputs.items()}
    result = {
        "protocol": "coworker-pals-frozen-synthetic-cohort-v2",
        "scope": "model-accepted synthetic DAGs; no semantic graph rewrite or human-gold claim",
        "source_zip_sha256": audit["source_zip_sha256"],
        "audit_manifest_sha256": sha(audit_raw),
        "decision_sha256": sha(raw_decisions),
        "decision_protocol": decision_protocol,
        "source_delivered": len(flow),
        "source_total_denominator": audit.get("source_total_denominator"),
        "files": files,
        "exclusions": exclusions,
        "e1_primary_rule": "selected forest_break parent: kind derived/knowledge and source_field solution",
        "e1_mechanical_secondary": "same selected operator; semantic exclusions applied; not primary claim",
        "selection_seed": audit["selection_seed"],
    }
    if not output_dir.parent.is_dir():
        raise ValueError("Create the private output parent directory before freezing")
    output_dir.mkdir(mode=0o700, exist_ok=False)
    for name, data in outputs.items():
        write_private(output_dir / name, data)
    write_private(output_dir / "manifest.json", json.dumps(
        result, ensure_ascii=False, sort_keys=True, indent=2,
        allow_nan=False).encode("utf-8") + b"\n")
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
