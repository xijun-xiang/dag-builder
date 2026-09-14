"""Pure repair contracts. Structural validity never certifies scientific truth."""

from copy import deepcopy
from heapq import heappop, heappush

from .schemas import require, text, validate_nodes, validate_review
from .validation import validate_parents

EXTRA_REVIEW_CHECKS = (
    "source_suitable",
    "essential_source_reasoning_preserved",
    "epistemic_strength_preserved",
)


def validate_candidate(candidate, reference):
    require(
        isinstance(candidate, dict) and set(candidate) == {"nodes", "parents"},
        "candidate must contain only nodes and parents",
    )
    validate_nodes(
        {"nodes": candidate["nodes"]},
        reference["question"]["question"],
        reference["solution"]["rationale"],
        reference["reference_sources"],
    )
    validate_parents({"parents": candidate["parents"]}, candidate["nodes"])


def topological_repair(nodes, parents, reference):
    """Reorder a closed acyclic graph without adding/removing any content or edge.

    Unknown IDs, cycles, disconnected graphs and source-quote errors fail. The
    caller may then request semantic adjudication instead of silently pruning.
    """
    validate_nodes(
        {"nodes": nodes},
        reference["question"]["question"],
        reference["solution"]["rationale"],
        reference["reference_sources"],
    )
    ids = [node["node_id"] for node in nodes]
    require(
        isinstance(parents, list) and len(parents) == len(ids),
        "dependency coverage mismatch",
    )
    require(
        all(
            isinstance(row, dict) and type(row.get("node_id")) is int for row in parents
        ),
        "invalid dependency rows",
    )
    require([row["node_id"] for row in parents] == ids, "dependency order/IDs mismatch")
    children = {node_id: [] for node_id in ids}
    degrees, mapping = {}, {}
    for row in parents:
        target, direct = row["node_id"], row.get("parents")
        require(
            isinstance(direct, list) and all(type(p) is int for p in direct),
            "invalid parents",
        )
        require(len(set(direct)) == len(direct), "duplicate parents")
        require(
            all(p in children and p != target for p in direct),
            "unknown/self dependency",
        )
        mapping[target] = direct
        degrees[target] = len(direct)
        for parent in direct:
            children[parent].append(target)
    ready, order = [], []
    for node_id in ids:
        if degrees[node_id] == 0:
            heappush(ready, node_id)
    while ready:
        current = heappop(ready)
        order.append(current)
        for child in children[current]:
            degrees[child] -= 1
            if degrees[child] == 0:
                heappush(ready, child)
    require(len(order) == len(ids), "dependency cycle")
    renumber = {old: new for new, old in enumerate(order, 1)}
    by_id = {node["node_id"]: node for node in nodes}
    candidate = {
        "nodes": [dict(deepcopy(by_id[old]), node_id=renumber[old]) for old in order],
        "parents": [
            {
                "node_id": renumber[old],
                "parents": sorted(renumber[p] for p in mapping[old]),
            }
            for old in order
        ],
    }
    validate_candidate(candidate, reference)
    return candidate, [{"old_id": old, "new_id": renumber[old]} for old in ids]


def validate_proposal(value, reference):
    require(isinstance(value, dict), "repair response must be an object")
    require(
        set(value)
        == {
            "decision",
            "category",
            "original_review",
            "reason",
            "changes",
            "candidate",
        },
        "unexpected repair fields",
    )
    require(
        value["decision"] in ("candidate", "needs_review", "unrepairable"),
        "invalid repair decision",
    )
    require(
        value["category"]
        in (
            "representation_error",
            "review_error",
            "source_gap",
            "source_error",
            "uncertain",
        ),
        "invalid repair category",
    )
    require(
        value["original_review"]
        in ("upheld", "overturned", "not_applicable", "uncertain"),
        "invalid original review verdict",
    )
    require(text(value["reason"]), "repair rationale required")
    changes = value["changes"]
    require(
        isinstance(changes, list) and all(text(change) for change in changes),
        "changes must be explanatory strings",
    )
    if value["decision"] == "candidate":
        require(
            value["category"] in ("representation_error", "review_error"),
            "unresolved source issue cannot produce an accepted candidate",
        )
        require(bool(changes), "candidate requires explicit change rationale")
        validate_candidate(value["candidate"], reference)
    else:
        require(
            value["candidate"] is None,
            "unresolved response must not include a candidate",
        )


def validate_repair_review(value):
    validate_review(value, "review_dag")
    for key in EXTRA_REVIEW_CHECKS:
        require(
            key in value["checks"]
            and (type(value["checks"][key]) is bool or value["checks"][key] is None),
            "missing repair audit check",
        )
    if value["decision"] == "accept":
        require(
            all(value["checks"][key] is True for key in EXTRA_REVIEW_CHECKS),
            "repair acceptance requires every additional check true",
        )
