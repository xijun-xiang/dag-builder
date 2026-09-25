"""New-prompt canary with the unchanged, replayable v3 graph transform.

The stronger reviewer contract targets known overstatements and missing
boundary arguments. It remains same-model review, not semantic certification.
"""

from .schemas import require
from .t2ance_v3 import (PROTOCOL as V3, assemble_graph_v3, normalize_v3,
                         validate_audit_v3)

PROTOCOL = "t2ance-lcb-normalize-v4"
EXTRA_REVIEW_CHECKS = (
    "retained_vs_feasible_set_distinguished",
    "boundary_and_monotonicity_arguments_explicit",
    "original_and_renumbered_ids_not_confused",
)


def normalize_v4(value, item):
    result = normalize_v3(value, item)
    return {**result, "protocol": PROTOCOL}


def assemble_graph_v4(value, normalized, item):
    require(normalized.get("protocol") == PROTOCOL, "wrong v4 normalization")
    return assemble_graph_v3(value, {**normalized, "protocol": V3}, item)


def validate_audit_v4(value):
    validate_audit_v3(value)
    require(all(key in value["checks"] and
                (type(value["checks"][key]) is bool or value["checks"][key] is None)
                for key in EXTRA_REVIEW_CHECKS), "missing v4 semantic review check")
    if value["decision"] == "accept":
        require(all(value["checks"][key] is True for key in EXTRA_REVIEW_CHECKS),
                "v4 semantic review is unresolved")
