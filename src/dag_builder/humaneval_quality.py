"""HumanEval-only quality contracts and lossless source-field normalization.

No code execution, JSON repair, graph editing or text rewriting. A diagnostic
flag is not a theorem about correctness; source aliases never waive validators.
"""
from copy import deepcopy
import re

from .schemas import require

VERSION = "humaneval-reference-v4"
CODE_FACT_VERSION = "humaneval-reference-v5"
QUALITY_VERSIONS = (VERSION, CODE_FACT_VERSION)
ALIASES = {"solution.rationale": "solution", "rationale": "solution",
           "question.question": "question"}
REVIEW_CHECKS = ("root_premises_sound", "reference_behavior_faithful")
DAG_CHECKS = (*REVIEW_CHECKS, "self_contained_statements", "no_invariant_assumed")
NUMBERED_REFERENCE = re.compile(
    r"\b(?:steps?|nodes?|statements?)\s*(?:#\s*)?\d+\b|第\s*\d+\s*(?:步|个?节点)", re.I)


def normalize_sources(value):
    """Map a fixed alias allowlist only; preserve all scientific content exactly."""
    result, changes = deepcopy(value), []
    if not isinstance(result, dict) or not isinstance(result.get("nodes"), list):
        return result, changes
    for node in result["nodes"]:
        if not isinstance(node, dict):
            continue
        original = node.get("source_field")
        canonical = ALIASES.get(original) if isinstance(original, str) else None
        if canonical:
            node["source_field"] = canonical
            changes.append({"node_id": node.get("node_id"), "field": "source_field",
                            "from": original, "to": canonical})
    return result, changes


def text_findings(nodes):
    """Flag positional references that cease to be well-defined after reordering."""
    findings = []
    for node in nodes:
        if node.get("kind") == "answer":
            continue
        for match in NUMBERED_REFERENCE.finditer(node.get("statement", "")):
            findings.append({"node_id": node.get("node_id"),
                             "code": "positional_reference", "text": match.group(),
                             "span": [match.start(), match.end()]})
    return findings


def validate_code_facts(nodes, reference_code):
    """Check representational constraints, not semantic truth of prose claims.

    Grounding and absence of assumed invariants require the separate DAG review.
    In particular, matching a code quotation alone never proves the statement.
    """
    code_lines = {line.strip() for line in reference_code.splitlines() if line.strip()}
    for node in nodes:
        if node["kind"] == "answer":
            continue
        statement = node["statement"]
        require(not any(tag in statement for tag in ("```", "<step>", "</step>")),
                "reasoning statements must be prose without framing tags")
        require(statement.strip() != reference_code.strip() and statement.strip() not in code_lines,
                "reasoning statements must not copy code lines or the complete answer")
        if node["source_field"] == "reference_code":
            require(node["kind"] == "given", "only given observations may cite reference code")
            require("parents" not in node or node["parents"] == [], "code observations must be roots")


def validate_quality(stage, value, data, *, version=VERSION):
    require(version in QUALITY_VERSIONS, "unknown HumanEval quality protocol")
    if stage == "atomize":
        require(not text_findings(value["nodes"]),
                "positional step/node references require semantic revision, not renumbering")
        if version == CODE_FACT_VERSION:
            validate_code_facts(value["nodes"], data["reference_code"])
    elif stage in ("review_solution", "review_dag"):
        keys = DAG_CHECKS if stage == "review_dag" else REVIEW_CHECKS
        if version == CODE_FACT_VERSION and stage == "review_dag":
            keys = (*keys, "code_facts_grounded")
        checks = value["checks"]
        label = "HumanEval " + version.rsplit("-", 1)[-1]
        for key in keys:
            require(key in checks and (type(checks[key]) is bool or checks[key] is None),
                    "missing " + label + " quality check: " + key)
        if value["decision"] == "accept":
            require(all(checks[k] is True for k in keys), "unresolved " + label + " quality check")
        if stage == "review_dag":
            require(not text_findings(data["nodes"]), "unresolved positional references")
