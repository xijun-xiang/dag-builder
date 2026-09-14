"""Producer contract and complete before/after diffs for a bounded repair round."""

from collections import Counter

from .output_normalization import validate_bundle
from .review_issues import evidence_sources, validate_evidence
from .schemas import require, text
from .storage import digest


def validate_revision(value, reference, previous, issues):
    require(
        isinstance(value, dict)
        and set(value)
        == {"action", "reason", "changes", "issue_responses", "candidate"},
        "invalid revision fields",
    )
    require(
        value["action"] in ("revise", "dispute", "source_disputed")
        and text(value["reason"]),
        "invalid revision action",
    )
    require(
        isinstance(value["changes"], list) and all(text(s) for s in value["changes"]),
        "changes must be strings",
    )
    responses = value["issue_responses"]
    require(
        isinstance(responses, list) and all(isinstance(r, dict) for r in responses),
        "issue responses required",
    )
    expected = [i["issue_id"] for i in issues]
    require(
        len(responses) == len(expected)
        and sorted(r.get("issue_id", "") for r in responses) == sorted(expected),
        "each issue must be answered exactly once",
    )
    sources = evidence_sources(reference, previous)
    if value["action"] == "revise":
        validate_bundle(value["candidate"], reference)
        validate_revision_scope(previous, value["candidate"], issues)
        # Old and revised IDs can denote different assertions after renumbering.
        # Never try both versions under an ambiguous, unversioned address.
        for field, quote in evidence_sources(reference, value["candidate"]).items():
            if field.startswith(("node:", "justification:")):
                sources[f"revised_{field}"] = quote
    for row in responses:
        require(
            set(row) == {"issue_id", "action", "reason", "evidence"},
            "invalid issue response fields",
        )
        require(
            row["action"] in ("fixed", "disputed", "unresolved")
            and text(row["reason"]),
            "invalid issue response",
        )
        validate_evidence(row["evidence"], sources)
    if value["action"] == "revise":
        require(bool(value["changes"]), "revision must explain changes")
        require(
            all(r["action"] == "fixed" for r in responses),
            "unresolved issues require dispute route",
        )
    else:
        require(value["candidate"] is None, "dispute must not smuggle a candidate")


def candidate_diff(before, after):
    """Store entire component changes; renumbering cannot hide removed assertions."""
    return {
        "before_sha256": digest(before),
        "after_sha256": digest(after),
        "components": {
            key: {"before": (before or {}).get(key), "after": after.get(key)}
            for key in ("nodes", "parents", "justifications")
            if (before or {}).get(key) != after.get(key)
        },
    }


def validate_revision_scope(before, after, issues):
    """Prevent structural overrepair and literal conclusion-as-premise repairs.

    Paraphrased circularity still requires semantic review; this is not a prover.
    """
    diagnoses = [i.get("diagnosis", {}) for i in issues]
    require(
        not any(d.get("remedy") == "adjudicate" for d in diagnoses),
        "unresolved diagnosis requires dispute route, not candidate revision",
    )
    if before and diagnoses and all(d.get("kind") == "rule_gap" for d in diagnoses):
        require(
            all(before[k] == after[k] for k in ("nodes", "parents")),
            "rule-only feedback permits justification edits, not graph changes",
        )
    if (
        before
        and diagnoses
        and all(d.get("kind") in ("rule_gap", "structural_error") for d in diagnoses)
    ):

        def assertions(bundle):
            return Counter(
                (n["statement"], n["source_field"], n["source_quote"])
                for n in bundle["nodes"]
            )

        require(
            assertions(before) == assertions(after),
            "structure-only repair must preserve all assertions and source citations",
        )

    def duplicated_edges(bundle):
        if not bundle:
            return set()
        nodes = {n["node_id"]: n for n in bundle["nodes"]}
        duplicates = set()
        for row in bundle["parents"]:
            child = nodes[row["node_id"]]
            if child["kind"] != "derived":
                continue
            for parent_id in row["parents"]:
                parent = nodes[parent_id]
                statement = " ".join(parent["statement"].split())
                if parent["kind"] == "knowledge" and statement == " ".join(
                    child["statement"].split()
                ):
                    duplicates.add(statement)
        return duplicates

    require(
        not (duplicated_edges(after) - duplicated_edges(before)),
        "new knowledge premise duplicates its derived conclusion",
    )
