"""Auditable, score-blind selection of an existing E1 forest-breaking edge.

This optional protocol is intentionally narrow: one frozen MMLU psychology
cohort, one source-record identity per question, and no DAG or legal-order edit.
"""

import json
from pathlib import Path

from .graph import violations
from .io import sha256


PSYCH_SUBSETS = frozenset({
    "high_school_psychology", "professional_psychology", "human_sexuality", "sociology",
})
SCHEMA = "pals_mmlu_psych_e1_break_overrides_v1"


def _record_identity(value):
    if not isinstance(value, dict) or set(value) != {
        "item_id", "source_id", "source_record_sha256"
    }:
        raise ValueError("Invalid E1 override cohort identity")
    item_id, source_id, record_sha = (
        value["item_id"], value["source_id"], value["source_record_sha256"]
    )
    source_parts = source_id.split(":") if isinstance(source_id, str) else []
    if (not isinstance(item_id, str) or not item_id
            or len(source_parts) != 4 or source_parts[0] != "cais/mmlu"
            or source_parts[1] not in PSYCH_SUBSETS
            or source_parts[2] != "test" or not source_parts[3].isdigit()
            or not isinstance(record_sha, str) or len(record_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in record_sha)):
        raise ValueError("Invalid E1 override source identity")
    return value


def _no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key in E1 overrides: {key}")
        result[key] = value
    return result


class BreakOverrides:
    """Validated manifest plus exact cohort-identity and direct-edge checks."""

    def __init__(self, path, benchmark, seed):
        if benchmark != "mmlu":
            raise ValueError("E1 break overrides are limited to MMLU psychology")
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("E1 override manifest must be a regular file")
        with self.path.open(encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_no_duplicate_pairs)
        if (not isinstance(value, dict) or set(value) != {
                "schema_version", "benchmark", "selection_seed", "cohort", "overrides"
        } or value["schema_version"] != SCHEMA or value["benchmark"] != "mmlu"
                or type(value["selection_seed"]) is not int or value["selection_seed"] != seed
                or not isinstance(value["cohort"], list) or not value["cohort"]
                or not isinstance(value["overrides"], list) or not value["overrides"]):
            raise ValueError("Invalid E1 override manifest or selection seed")
        self.cohort = {}
        self.source_ids = set()
        for identity in value["cohort"]:
            identity = _record_identity(identity)
            item_id, source_id = identity["item_id"], identity["source_id"]
            if item_id in self.cohort or source_id in self.source_ids:
                raise ValueError("Duplicate E1 override cohort identity")
            self.cohort[item_id] = identity
            self.source_ids.add(source_id)
        self.edges = {}
        for override in value["overrides"]:
            if (not isinstance(override, dict)
                    or set(override) != {"item_id", "parent_id", "target_id"}
                    or override["item_id"] not in self.cohort
                    or override["item_id"] in self.edges
                    or type(override["parent_id"]) is not int
                    or type(override["target_id"]) is not int
                    or override["parent_id"] == override["target_id"]):
                raise ValueError("Invalid or duplicate E1 break override")
            self.edges[override["item_id"]] = (
                override["parent_id"], override["target_id"]
            )
        self.file_sha256 = sha256(self.path)
        self.seen = set()

    def check_record(self, record):
        item_id = record.get("item_id")
        if item_id not in self.cohort or item_id in self.seen:
            raise ValueError("E1 override cohort has unexpected or duplicate question")
        source = record.get("provenance", {})
        identity = self.cohort[item_id]
        if (source.get("source_id") != identity["source_id"]
                or source.get("source_record_sha256") != identity["source_record_sha256"]):
            raise ValueError("E1 override source-record identity mismatch")
        self.seen.add(item_id)

    def check_complete(self):
        if self.seen != set(self.cohort):
            raise ValueError("E1 override cohort is incomplete")

    def select(self, item_id, steps, baseline, original_break):
        """Return unchanged break or one alternative existing adjacent direct edge."""
        edge = self.edges.get(item_id)
        if edge is None:
            return original_break
        if original_break is None:
            raise ValueError("E1 override requires an existing forest breaking candidate")
        parent, target = edge
        by_id = {node["node_id"]: node for node in steps}
        if (parent not in by_id or target not in by_id
                or parent not in by_id[target]["parents"]
                or by_id[parent].get("source_field") != "solution"
                or by_id[parent].get("kind") not in {"knowledge", "derived"}):
            raise ValueError("E1 override is not an existing solution dependency")
        position = baseline.index(parent)
        if position < 1 or position + 1 >= len(baseline) or baseline[position + 1] != target:
            raise ValueError("E1 override is not adjacent after fixed first node")
        order = list(baseline)
        order[position], order[position + 1] = order[position + 1], order[position]
        if violations(steps, order) != [edge]:
            raise ValueError("E1 override must invert exactly its stated direct edge")
        return {"order": order, "swapped": [parent, target], "violated_edges": [edge]}

    def provenance(self, code_files):
        return {
            "schema_version": SCHEMA,
            "manifest_sha256": self.file_sha256,
            "cohort_questions": len(self.cohort),
            "changed_forest_breaks": len(self.edges),
            "selection_policy": "same_forest_baseline_one_existing_adjacent_solution_edge",
            "code_sha256": {Path(path).name: sha256(path) for path in code_files},
        }
