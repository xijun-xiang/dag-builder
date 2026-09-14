"""Evidence-addressed audit contracts. Valid quotations do not prove a verdict."""

from .repair_graph import EXTRA_REVIEW_CHECKS, validate_repair_review
from .schemas import REVIEW_CHECKS, require, text
from .storage import digest

AUDIT_CONTRACT = "actionable-diagnosis-20260914-v2"

CODES = {
    "missing_premise",
    "invalid_dependency",
    "redundant_dependency",
    "source_fidelity",
    "missing_reasoning",
    "justification_error",
    "source_error",
    "source_gap",
    "uncertain",
}


def evidence_sources(reference, candidate):
    sources = {
        "question": reference["question"]["question"],
        "solution": reference["solution"]["rationale"],
        **reference["reference_sources"],
    }
    if candidate:
        for node in candidate.get("nodes", []):
            sources[f"node:{node['node_id']}"] = node["statement"]
        for row in candidate.get("justifications", []):
            sources[f"justification:{row['node_id']}"] = row["text"]
    return sources


def validate_evidence(rows, sources):
    require(isinstance(rows, list) and bool(rows), "explicit evidence required")
    for row in rows:
        require(
            isinstance(row, dict) and set(row) == {"field", "quote"},
            "invalid evidence record",
        )
        require(
            isinstance(row["field"], str) and row["field"] in sources,
            "unknown evidence field",
        )
        require(
            text(row["quote"]) and row["quote"] in sources[row["field"]],
            "evidence quote does not match addressed field",
        )


def validate_audit(value, reference, candidate):
    require(
        isinstance(value, dict)
        and set(value) == {"decision", "checks", "issues", "reason"},
        "invalid audit fields",
    )
    require(isinstance(value["issues"], list), "issues must be a list")
    require(
        isinstance(value["checks"], dict)
        and set(value["checks"])
        == set(REVIEW_CHECKS["review_dag"]) | set(EXTRA_REVIEW_CHECKS),
        "audit requires exactly nine checks",
    )
    if value["decision"] != "accept":
        require(bool(value["issues"]), "non-accept audit requires evidenced issues")
    sources = evidence_sources(reference, candidate)
    ids = {n["node_id"] for n in candidate["nodes"]}
    for issue in value["issues"]:
        require(
            isinstance(issue, dict)
            and set(issue)
            == {"code", "node_ids", "certainty", "reason", "evidence", "diagnosis"},
            "invalid structured issue",
        )
        require(
            isinstance(issue["code"], str) and issue["code"] in CODES,
            "unknown issue code",
        )
        require(
            issue["certainty"] in ("definite", "uncertain") and text(issue["reason"]),
            "issue certainty and reason required",
        )
        require(
            isinstance(issue["node_ids"], list)
            and all(type(i) is int and i in ids for i in issue["node_ids"]),
            "issue references unknown node",
        )
        validate_evidence(issue["evidence"], sources)
        validate_diagnosis(issue)
        for evidence in issue["evidence"]:
            if evidence["field"].startswith(("node:", "justification:")):
                require(
                    int(evidence["field"].split(":")[1]) in issue["node_ids"],
                    "evidence node is not among the cited nodes",
                )
    validate_repair_review(dict(value, issues=[i["reason"] for i in value["issues"]]))
    if value["decision"] == "reject":
        require(
            any(i["certainty"] == "definite" for i in value["issues"]),
            "uncertainty alone cannot justify rejection",
        )


def issue_packet(audit):
    return [dict(issue, issue_id=digest(issue)[:16]) for issue in audit["issues"]]


def validate_diagnosis(issue):
    """Check the actionable contract, not the scientific truth of a critique."""
    row = issue["diagnosis"]
    require(
        isinstance(row, dict)
        and set(row)
        == {"kind", "missing_content", "necessity", "source_support", "remedy"},
        "explicit issue diagnosis required",
    )
    remedies = {
        "factual_gap": {"add_source_premise", "adjudicate"},
        "rule_gap": {"explain_rule"},
        "structural_error": {"repair_structure"},
        "granularity_dispute": {"adjudicate"},
        "other": {"other"},
    }
    require(
        isinstance(row["kind"], str) and row["kind"] in remedies,
        "unknown diagnosis kind",
    )
    require(
        isinstance(row["remedy"], str) and row["remedy"] in remedies[row["kind"]],
        "remedy does not match diagnosis",
    )
    require(
        text(row["missing_content"]) and text(row["necessity"]),
        "specify the defect and why it matters",
    )
    require(
        row["source_support"] in ("available", "absent", "uncertain", "not_applicable"),
        "invalid source support",
    )
    if row["kind"] == "granularity_dispute":
        require(
            issue["certainty"] == "uncertain",
            "granularity dispute is not a definite defect",
        )
    if row["remedy"] == "add_source_premise":
        require(
            row["source_support"] == "available"
            and any(
                e["field"]
                in (
                    "question",
                    "solution",
                    "choice_A",
                    "choice_B",
                    "choice_C",
                    "choice_D",
                )
                for e in issue["evidence"]
            ),
            "new premise needs unchanged source evidence, not answer or candidate authority",
        )
    if issue["code"] in ("missing_premise", "invalid_dependency"):
        require(
            row["kind"] != "other",
            "dependency criticism must distinguish fact, rule, structure, or granularity",
        )
    if row["kind"] == "structural_error":
        require(
            issue["code"] in ("invalid_dependency", "redundant_dependency"),
            "structural diagnosis requires a dependency issue",
        )
        require(
            bool(issue["node_ids"]), "structural diagnosis must identify affected nodes"
        )


def validate_adjudication(value, reference, candidate):
    require(
        isinstance(value, dict)
        and set(value) == {"decision", "reason", "evidence", "choice_analysis"},
        "invalid adjudication fields",
    )
    require(
        value["decision"] in ("revisable", "source_disputed", "uncertain")
        and text(value["reason"]),
        "invalid adjudication verdict",
    )
    validate_evidence(value["evidence"], evidence_sources(reference, candidate))
    validate_choice_analysis(value["choice_analysis"], reference)


def validate_choice_analysis(rows, reference):
    """Ensure actual choices are inspected; this does not prove their status."""
    sources = evidence_sources(reference, None)
    sources.pop("correct_answer", None)
    expected = {f"choice_{letter}" for letter in "ABCD"}
    require(
        isinstance(rows, list) and len(rows) == 4,
        "source reassessment must inspect all four actual choices",
    )
    seen = set()
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row) == {"choice", "status", "reason", "evidence"},
            "invalid choice analysis fields",
        )
        require(
            isinstance(row["choice"], str)
            and row["choice"] in expected
            and row["choice"] not in seen,
            "duplicate or unknown choice",
        )
        seen.add(row["choice"])
        require(
            row["status"] in ("compatible", "excluded", "undetermined")
            and text(row["reason"]),
            "choice status and reason required",
        )
        validate_evidence(row["evidence"], sources)
        require(
            any(
                e["field"] == row["choice"] and e["quote"] == sources[row["choice"]]
                for e in row["evidence"]
            ),
            "each choice needs its complete actual text",
        )
        require(
            any(e["field"] in ("question", "solution") for e in row["evidence"]),
            "choice comparison needs question or explanation evidence",
        )
