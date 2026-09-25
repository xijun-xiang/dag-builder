#!/usr/bin/env python3
"""Score-blind E2 quality gate for the immutable coworker reasoning DAGs.

The historical hash-selected anchor is replayed unchanged.  This gate selects
whole records only; it never edits statements, edges, prompts or the anchor.
The output is private evidence and a per-arm exclusion map consumable by the
cohort freezer.  Lexical checks catch only obvious repeats, not semantic ones.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pals_validation.data import normalize  # noqa: E402
from pals_validation.graph import e2_anchor  # noqa: E402


PACKAGES = ("gsm8k", "mmlu_math", "mmlu_psych_social")
MATH_SUBSETS = frozenset((
    "abstract_algebra", "college_mathematics", "elementary_mathematics",
    "high_school_mathematics", "high_school_statistics",
))
OPTION_CONCLUSION = re.compile(
    r"\b(?:correct|right|best)\s+(?:answer|option|choice)\b"
    r"|\b(?:answer|option|choice)\s*(?:is|:)?\s*[ABCD]\b"
    r"|\bcorresponds\s+to\s+(?:option|choice)\s*[ABCD]\b",
    re.IGNORECASE,
)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_jsonl(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8")
    if not text.endswith("\n") or not text.strip():
        raise ValueError("Expected nonempty newline-terminated JSONL")
    rows = [json.loads(line) for line in text.splitlines()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid JSONL row")
    return rows


def verified_rows(audit_dir: Path, audit: dict, filename: str) -> list[dict]:
    raw = (audit_dir / filename).read_bytes()
    if sha(raw) != audit["outputs_sha256"][filename]:
        raise ValueError(f"Audit hash mismatch: {filename}")
    return read_jsonl(raw)


def norm_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(text.split()).rstrip(" .?!。！？")


def prompt_fingerprint(row: dict) -> tuple[str, tuple[str, ...]]:
    problem = row["problem"]
    return norm_text(problem["question"]), tuple(
        norm_text(choice) for choice in (problem.get("choices") or [])
    )


def long_choice_overlap(target: str, choices: list[str] | None) -> bool:
    """Conservative lexical screen; overlapping options may also contradict."""
    target_tokens = Counter(re.findall(r"[a-z0-9]+", norm_text(target)))
    for choice in choices or []:
        choice_tokens = Counter(re.findall(r"[a-z0-9]+", norm_text(choice)))
        length = sum(choice_tokens.values())
        if length >= 6 and sum((target_tokens & choice_tokens).values()) / length >= 0.85:
            return True
    return False


def anchor_reasons(case: dict, anchor: dict) -> tuple[dict, list[str]]:
    """Classify a frozen anchor without choosing a different target."""
    by_id = {node["node_id"]: node for node in case["steps"]}
    deleted = by_id[anchor["deleted_id"]]
    target = by_id[anchor["target_id"]]
    reasons = []
    # The question and all MMLU choices remain in both scoring contexts.
    # A quote copied from them has not actually been removed from the prompt.
    if deleted["kind"] not in ("derived", "knowledge") or deleted["source_field"] != "solution":
        reasons.append("deleted_parent_not_solution_reasoning_or_knowledge")
    if target["kind"] != "derived" or target["source_field"] != "solution":
        reasons.append("target_not_solution_derived")
    # E2 is advertised as a process-level next-step probe, not terminal answer
    # prediction.  Require the reference target to feed another scored step.
    if not any(target["node_id"] in node["parents"] for node in case["steps"]):
        reasons.append("reference_target_has_no_nonanswer_child")
    if OPTION_CONCLUSION.search(target["statement"]):
        reasons.append("target_explicit_option_conclusion")
    if long_choice_overlap(target["statement"], case.get("choices")):
        reasons.append("target_overlaps_long_choice")
    if norm_text(target["statement"]) == norm_text(deleted["statement"]):
        reasons.append("target_literal_parent_repeat")
    if norm_text(target["statement"]) == norm_text(case["question"]):
        reasons.append("target_literal_question_repeat")
    return {
        "target_id": anchor["target_id"],
        "deleted_id": anchor["deleted_id"],
        "prefix_ids": anchor["prefix_ids"],
        "deleted_kind": deleted["kind"],
        "deleted_source_field": deleted["source_field"],
        "target_kind": target["kind"],
        "target_source_field": target["source_field"],
        "target_has_nonanswer_child": not (
            "reference_target_has_no_nonanswer_child" in reasons
        ),
    }, reasons


def _e2_reasons(record: dict, seed: int) -> tuple[dict, list[str]]:
    benchmark = "gsm8k" if record["benchmark"] == "gsm8k" else "mmlu"
    case = normalize(record, benchmark)
    anchor = e2_anchor(case["steps"], case["item_id"], seed)
    if anchor is None:
        raise ValueError("Certified E2 candidate has no anchor")
    return anchor_reasons(case, anchor)


def _package(record: dict) -> str:
    if record["benchmark"] == "gsm8k":
        return "gsm8k"
    return ("mmlu_math" if record["provenance"]["subset"] in MATH_SUBSETS
            else "mmlu_psych_social")


def apply_score_blind_review(assessments: dict[str, dict], by_item: dict[str, dict],
                             review: dict) -> Counter:
    """Require an explicit decision on every mechanically strict psych case."""
    if (review.get("protocol") != "assistant_score_blind_psych_e2_review_v1"
            or not isinstance(review.get("items"), dict)):
        raise ValueError("Unexpected score-blind review format")
    expected = {by_item[item]["source_id"] for item, value in assessments.items()
                if by_item[item]["package"] == "mmlu_psych_social" and not value["reasons"]}
    if set(review["items"]) != expected:
        raise ValueError("Score-blind review does not cover exactly the strict psych candidates")
    counts = Counter()
    by_source = {by_item[item]["source_id"]: item for item in assessments}
    for source_id, verdict in review["items"].items():
        if (not isinstance(verdict, dict) or set(verdict) != {"decision", "reason"}
                or verdict["decision"] not in ("retain", "uncertain", "hard_exclude")
                or not isinstance(verdict["reason"], str) or not verdict["reason"].strip()):
            raise ValueError("Invalid per-question review decision")
        item = by_source[source_id]
        assessments[item]["assistant_blind_review"] = verdict
        counts[verdict["decision"]] += 1
        if verdict["decision"] != "retain":
            assessments[item]["reasons"].append(
                "assistant_blind_review_" + verdict["decision"] + ":" + verdict["reason"]
            )
    return counts


def qualify(audit_dir: Path, review_path: Path | None = None) -> tuple[list[dict], dict, dict]:
    audit_raw = (audit_dir / "manifest.json").read_bytes()
    audit = json.loads(audit_raw)
    if (audit.get("protocol") != "coworker-dag-conservative-audit-v1"
            or audit.get("delivered_total") != 1539):
        raise ValueError("Unexpected audit version or delivered denominator")
    flow = verified_rows(audit_dir, audit, "flow_1539.jsonl")
    candidates = verified_rows(audit_dir, audit, "e2_mechanical_candidates.jsonl")
    if len(flow) != 1539 or len({r["item_id"] for r in flow}) != 1539:
        raise ValueError("Incomplete or duplicate 1,539-row flow")
    by_item = {row["item_id"]: row for row in flow}
    by_candidate = {row["item_id"]: row for row in candidates}
    certified = {row["item_id"] for row in flow if row["e2_anchor_mechanical"]}
    if len(by_candidate) != len(candidates) or set(by_candidate) != certified:
        raise ValueError("E2 candidate set differs from audited flow")

    assessments: dict[str, dict] = {}
    for item, record in by_candidate.items():
        source_id = record["provenance"]["source_id"]
        if by_item[item]["source_id"] != source_id:
            raise ValueError("Candidate/source identity mismatch")
        if _package(record) != by_item[item]["package"]:
            raise ValueError("Package identity mismatch")
        anchor, reasons = _e2_reasons(record, audit["selection_seed"])
        assessments[item] = {"anchor": anchor, "reasons": reasons,
                             "prompt_fingerprint": prompt_fingerprint(record)}

    # Report duplicate prompts across all E2 candidates.  Only candidates
    # passing the protocol checks compete for inclusion, so an unusable earlier
    # row cannot cause the only usable member of a duplicate cluster to vanish.
    all_groups: dict[tuple[str, tuple[str, tuple[str, ...]]], list[str]] = defaultdict(list)
    for item, assessment in assessments.items():
        key = (by_item[item]["package"], assessment["prompt_fingerprint"])
        all_groups[key].append(item)
    duplicate_sources = {
        item: sorted(by_item[member]["source_id"] for member in items)
        for items in all_groups.values() if len(items) > 1 for item in items
    }

    # Deduplicate only among candidates passing the protocol checks.  A
    # punctuation-only duplicate must not count as another independent item.
    groups: dict[tuple[str, tuple[str, tuple[str, ...]]], list[str]] = defaultdict(list)
    for item, assessment in assessments.items():
        if not assessment["reasons"]:
            key = (by_item[item]["package"], assessment["prompt_fingerprint"])
            groups[key].append(item)
    for items in groups.values():
        if len(items) > 1:
            canonical = min(items, key=lambda item: by_item[item]["source_id"])
            for item in items:
                if item != canonical:
                    assessments[item]["reasons"].append(
                        "duplicate_prompt_of:" + by_item[canonical]["source_id"]
                    )

    review_counts = None
    review_sha256 = None
    if review_path is not None:
        review_raw = review_path.read_bytes()
        review_counts = apply_score_blind_review(
            assessments, by_item, json.loads(review_raw)
        )
        review_sha256 = sha(review_raw)

    full_flow = []
    exclusion_map = {}
    counts = {package: Counter() for package in PACKAGES}
    reason_counts = {package: Counter() for package in PACKAGES}
    for row in flow:
        item, package = row["item_id"], row["package"]
        counts[package]["delivered"] += 1
        if item not in assessments:
            decision, anchor, reasons = "not_e2_mechanical", None, []
        else:
            counts[package]["e2_mechanical"] += 1
            assessment = assessments[item]
            anchor, reasons = assessment["anchor"], assessment["reasons"]
            decision = "excluded" if reasons else "qualified"
            if reasons:
                exclusion_map[row["source_id"]] = ";".join(reasons)
                for reason in reasons:
                    reason_counts[package][reason.split(":", 1)[0]] += 1
            else:
                counts[package]["qualified"] += 1
        full_flow.append({
            "item_id": item, "source_id": row["source_id"], "package": package,
            "source_record_sha256": row["record_sha256"],
            "e2_decision": decision, "e2_reasons": reasons,
            "e2_anchor": anchor,
            "duplicate_prompt_source_ids": duplicate_sources.get(item, []),
            "assistant_blind_review": (assessments[item].get("assistant_blind_review")
                                       if item in assessments else None),
        })
    result = {
        "protocol": "coworker-e2-score-blind-internal-step-v1",
        "scope": "Unchanged hash-selected E2 anchor; unchanged source DAGs; model-accepted, not human gold",
        "audit_manifest_sha256": sha(audit_raw),
        "source_zip_sha256": audit["source_zip_sha256"],
        "selection_seed": audit["selection_seed"],
        "counts": {package: dict(counts[package]) for package in PACKAGES},
        "reason_counts": {package: dict(reason_counts[package]) for package in PACKAGES},
        "rule": [
            "deleted parent is derived/knowledge from solution, not question/choices",
            "reference target is derived from solution and feeds a non-answer child",
            "reject explicit option conclusion, long-choice overlap or literal target repeat",
            "deduplicate equivalent question+choices within each package",
        ],
        "limitations": "Semantic paraphrases and graph truth require separate blind review; reference target is not forced during generation",
        "delivered_total": len(full_flow),
        "qualified_total": sum(counts[p]["qualified"] for p in PACKAGES),
        "duplicate_prompt_groups": len([items for items in all_groups.values() if len(items) > 1]),
        "assistant_blind_review_sha256": review_sha256,
        "assistant_blind_review_counts": dict(review_counts) if review_counts is not None else None,
    }
    decisions = {"protocol": "score_blind_experiment_exclusions_v2",
                 "excluded_source_ids": {"e1": {}, "e2": exclusion_map}}
    return full_flow, result, decisions


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def encode_json(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def encode_jsonl(rows: list[dict]) -> bytes:
    return b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False) + "\n").encode("utf-8")
        for row in rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--psych-review", type=Path,
                        help="Score-blind per-ID assistant review; only for strict psych candidates")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    flow, report, decisions = qualify(args.audit_dir, args.psych_review)
    if not args.output.parent.is_dir():
        raise ValueError("Create the private output parent before qualifying")
    args.output.mkdir(mode=0o700, exist_ok=False)
    raw_flow = encode_jsonl(flow)
    raw_decisions = encode_json(decisions)
    report["files_sha256"] = {"flow_1539.jsonl": sha(raw_flow),
                              "e2_decisions.json": sha(raw_decisions)}
    write_private(args.output / "flow_1539.jsonl", raw_flow)
    write_private(args.output / "e2_decisions.json", raw_decisions)
    write_private(args.output / "manifest.json", encode_json(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
