"""Versioned, auditable answer-backward selection for t2ance DAG proposals.

This is a structural transform of a model proposal, not a semantic repair.
The raw response, discarded claims and deleted redundant edges remain visible
to the later reviewer and to the offline replay audit.
"""

from .calibri_normalize import normalize, output_source_field, validate_audit
from .schemas import require, text, validate_nodes
from .storage import digest
from .validation import validate_justifications, validate_parents


PROTOCOL = "t2ance-lcb-normalize-v3"
EXTRA_REVIEW_CHECKS = (
    "answer_backward_selection_sound",
    "excluded_claims_unnecessary",
    "removed_direct_edges_redundant",
)


def normalize_v3(value, item):
    """Reuse the frozen source-anchor contract, with a distinct v3 identity."""
    result = normalize(value, item, prompt_version="t2ance-lcb-normalize-v2")
    require(output_source_field(item) == "t2ance_output", "v3 requires t2ance source")
    return {**result, "protocol": PROTOCOL}


def _checked_rows(value, normalized):
    require(isinstance(value, dict) and set(value) == {
        "dependencies", "answer_parents", "answer_justification"},
        "invalid v3 dependency response")
    nodes = normalized["nodes"]
    rows = value["dependencies"]
    require(isinstance(rows, list) and len(rows) == len(nodes),
            "v3 dependency coverage mismatch")
    mapping = {}
    for node, row in zip(nodes, rows):
        node_id = node["node_id"]
        require(isinstance(row, dict) and set(row) == {
            "node_id", "parents", "justification"} and type(row["node_id"]) is int
            and row["node_id"] == node_id, "v3 dependency order/IDs changed")
        parents = row["parents"]
        require(isinstance(parents, list) and all(type(p) is int for p in parents)
                and len(parents) == len(set(parents))
                and all(0 < p < node_id for p in parents),
                "v3 unknown, duplicate or future parent")
        require(text(row["justification"]), "v3 empty dependency justification")
        require(node["kind"] not in ("given", "knowledge") or not parents,
                "v3 given/knowledge node cannot depend on another claim")
        mapping[node_id] = parents
    answer_parents = value["answer_parents"]
    require(isinstance(answer_parents, list) and bool(answer_parents)
            and all(type(p) is int and 0 < p <= len(nodes) for p in answer_parents)
            and len(answer_parents) == len(set(answer_parents)),
            "v3 answer has no valid declared premise")
    require(text(value["answer_justification"]), "v3 empty answer justification")
    mapping[len(nodes) + 1] = answer_parents
    return rows, mapping


def _ancestors(mapping, node_id, memo):
    if node_id not in memo:
        result = set()
        for parent in mapping[node_id]:
            result.add(parent)
            result.update(_ancestors(mapping, parent, memo))
        memo[node_id] = result
    return memo[node_id]


def assemble_graph_v3(value, normalized, item):
    """Prune only non-ancestors and reduce only provably transitive edges.

    Both operations preserve *declared* answer reachability. They do not prove
    that the model declared all necessary premises; that remains an explicit
    semantic-review gate. No node statement or source anchor is rewritten.
    """
    require(normalized.get("protocol") == PROTOCOL, "wrong v3 normalization")
    rows, mapping = _checked_rows(value, normalized)
    original_nodes = normalized["nodes"]
    terminal = len(original_nodes) + 1
    memo = {}
    selected = sorted(_ancestors(mapping, terminal, memo))
    selected_set = set(selected)
    require(bool(selected) and all(mapping[node_id] or
            original_nodes[node_id - 1]["kind"] in ("given", "knowledge")
            for node_id in selected), "selected derivation has a missing premise")
    question_roots = [node_id for node_id in selected
                      if original_nodes[node_id - 1]["kind"] == "given"
                      and any(span["source_field"] == "question"
                              for span in original_nodes[node_id - 1]["source_spans"])]
    require(bool(question_roots), "answer chain lacks an explicit question-root premise")

    reduced = {}
    removed_edges = []
    for child in (*selected, terminal):
        parents = mapping[child]
        direct = []
        for parent in parents:
            through = next((other for other in parents if other != parent
                            and parent in _ancestors(mapping, other, memo)), None)
            if through is None:
                direct.append(parent)
            else:
                removed_edges.append({"child_original_id": child,
                                      "parent_original_id": parent,
                                      "via_original_id": through})
        reduced[child] = direct

    renumber = {old: new for new, old in enumerate(selected, 1)}
    renumber[terminal] = len(selected) + 1
    nodes = []
    for old in selected:
        node = original_nodes[old - 1]
        nodes.append({**node, "node_id": renumber[old], "original_node_id": old,
                      "parents": [renumber[p] for p in reduced[old]],
                      "justification": rows[old - 1]["justification"]})
    nodes.append({"node_id": renumber[terminal], "original_node_id": terminal,
                  "kind": "answer", "statement": item["reference_code"],
                  "source_field": "reference_code",
                  "source_quote": item["reference_code"],
                  "support_type": "source_supported",
                  "parents": [renumber[p] for p in reduced[terminal]],
                  "justification": value["answer_justification"],
                  "excluded_from_pals": True})
    validate_nodes({"nodes": nodes}, item["question"], "", extra_sources={
        output_source_field(item): item["raw_output"],
        "reference_code": item["reference_code"]}, allow_reference_code_facts=True)
    validate_parents({"parents": [{"node_id": n["node_id"], "parents": n["parents"]}
                                 for n in nodes]}, nodes, require_minimal=True)
    validate_justifications({"justifications": [{"node_id": n["node_id"],
                                                 "text": n["justification"]}
                                                for n in nodes]}, nodes)
    discarded = [{"original_node_id": old,
                  "statement": original_nodes[old - 1]["statement"],
                  "source_refs": original_nodes[old - 1]["source_refs"],
                  "model_justification": rows[old - 1]["justification"],
                  "reason": "outside model-declared answer ancestry; semantic necessity unverified"}
                 for old in range(1, terminal) if old not in selected_set]
    transformation = {
        "protocol": "t2ance-answer-backward-canonicalization-v1",
        "raw_dependencies_sha256": digest(value),
        "original_node_count": len(original_nodes),
        "selected_original_ids": selected,
        "question_root_original_ids": question_roots,
        "discarded_nodes": discarded,
        "removed_transitive_edges": removed_edges,
        "claim": "structural reachability only; fresh semantic review required",
    }
    return {"nodes": nodes, "transformation": transformation}


def validate_audit_v3(value):
    validate_audit(value)
    require(all(k in value["checks"] and
                (type(value["checks"][k]) is bool or value["checks"][k] is None)
                for k in EXTRA_REVIEW_CHECKS), "missing v3 transformation review check")
    if value["decision"] == "accept":
        require(all(value["checks"][k] is True for k in EXTRA_REVIEW_CHECKS),
                "v3 transformation is not semantically accepted")
