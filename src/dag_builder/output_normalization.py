"""Lossless protocol normalization; never fixes JSON syntax or scientific text."""

import json
from copy import deepcopy

from .repair_graph import validate_candidate
from .schemas import InvalidOutput, validate_nodes
from .validation import validate_justifications, validate_parents


def normalize(value):
    normalized, changes = deepcopy(value), []
    if not isinstance(normalized, dict):
        return normalized, changes
    explanations = normalized.get("changes")
    if isinstance(explanations, list):
        for index, entry in enumerate(explanations):
            if isinstance(entry, dict):
                explanations[index] = json.dumps(
                    entry, ensure_ascii=False, sort_keys=True
                )
                changes.append(
                    {
                        "path": f"changes/{index}",
                        "operation": "lossless_object_to_json_string",
                    }
                )
    candidate = normalized.get("candidate", normalized)
    if isinstance(candidate, dict) and isinstance(candidate.get("nodes"), list):
        for node in candidate["nodes"]:
            if not isinstance(node, dict):
                continue
            field = node.get("source_field")
            alias = {
                "question.question": "question",
                "solution.rationale": "solution",
            }.get(field)
            if alias:
                node["source_field"] = alias
                changes.append(
                    {"node_id": node.get("node_id"), "from": field, "to": alias}
                )
    return normalized, changes


def validate_bundle(bundle, reference):
    from .schemas import require

    require(
        isinstance(bundle, dict)
        and set(bundle) == {"nodes", "parents", "justifications"},
        "candidate requires nodes, parents and justifications only",
    )
    validate_candidate({k: bundle[k] for k in ("nodes", "parents")}, reference)
    validate_justifications(
        {"justifications": bundle["justifications"]}, bundle["nodes"]
    )


def diagnose(bundle, reference):
    """Collect independent shape/node/parent/justification faults, not just first."""
    if not isinstance(bundle, dict):
        return ["candidate is missing or not an object"]
    faults = []
    if set(bundle) != {"nodes", "parents", "justifications"}:
        faults.append("candidate requires nodes, parents and justifications only")
    checks = (
        lambda: validate_nodes(
            {"nodes": bundle.get("nodes")},
            reference["question"]["question"],
            reference["solution"]["rationale"],
            reference["reference_sources"],
        ),
        lambda: validate_parents(
            {"parents": bundle.get("parents")}, bundle.get("nodes", [])
        ),
        lambda: validate_justifications(
            {"justifications": bundle.get("justifications")}, bundle.get("nodes", [])
        ),
    )
    for check in checks:
        try:
            check()
        except (
            InvalidOutput,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as error:
            faults.append(
                str(error)
                if isinstance(error, InvalidOutput)
                else "malformed candidate prevents this structural check"
            )
    return list(dict.fromkeys(faults))
