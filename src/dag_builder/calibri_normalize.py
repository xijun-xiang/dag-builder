"""Source-bound step normalization contracts, separate from PALS and generation.

Programs, source offsets, quotes and numeric step IDs are assigned by Python.
The model proposes only statements and explicit source-unit references. A valid
structure is not a semantic acceptance, and never an automatic formal release.
"""

from .humaneval_quality import text_findings, validate_code_facts
from .schemas import require, text, validate_nodes, validate_review
from .storage import digest
from .validation import validate_justifications, validate_parents

PROTOCOL = "calibri-lcb-normalize-v1"
REVIEW_CHECKS = (
    "source_meaning_preserved", "algorithm_matches_tested_code", "no_silent_error_repair",
    "omissions_safe", "supplements_disclosed_and_valid", "root_premises_sound",
    "self_contained_statements", "no_invariant_assumed", "code_facts_grounded",
)


def output_source_field(item):
    """Keep historical CALIBRI provenance while naming newer source outputs."""
    origin = item.get("reference_origin", "calibri_model_output")
    require(origin in ("calibri_model_output", "t2ance_model_output"),
            "unknown reference output origin")
    return "calibri_output" if origin == "calibri_model_output" else "t2ance_output"


def source_units(item):
    """Line units are lossless anchors, NOT the intended reasoning granularity."""
    sources = {"question": item["question"], output_source_field(item): item["raw_output"],
               "reference_code": item["reference_code"]}
    result = []
    for prefix, (field, value) in zip(("Q", "O", "C"), sources.items()):
        require(text(value), "empty source text")
        offset = 0
        for number, line in enumerate(value.splitlines(keepends=True), 1):
            if line.strip():
                result.append({"unit_id": f"{prefix}{number:04d}", "source_field": field,
                               "start": offset, "end": offset + len(line), "text": line,
                               "source_sha256": digest(value)})
            offset += len(line)
    return result


def public_input(item):
    """Allowlist only; never copy test bundles, filesystem paths or API keys."""
    from .schemas import public_question
    # Offsets/hashes stay in the local evidence; the model needs only lossless
    # text and stable IDs. Repeating a source hash on every line wastes context.
    units = [{k: u[k] for k in ("unit_id", "source_field", "text")} for u in source_units(item)]
    return {"question": public_question(item), "source_units": units,
            "reference_code": item["reference_code"],
            "reference_execution": item["reference_execution"],
            "purpose": "derived reference explanation, not a preserved native generation trajectory"}


def normalize(value, item):
    require(isinstance(value, dict) and set(value) == {"steps", "omissions"},
            "normalization needs exactly steps and omissions")
    steps, omissions = value["steps"], value["omissions"]
    require(isinstance(steps, list) and 1 <= len(steps) <= 64, "invalid bounded step list")
    units = {u["unit_id"]: u for u in source_units(item)}

    def anchors(refs):
        require(isinstance(refs, list) and bool(refs) and all(isinstance(r, str) for r in refs),
                "source references must be a nonempty string list")
        require(len(refs) == len(set(refs)) and all(r in units for r in refs), "unknown or duplicate source unit")
        return [units[r] for r in refs]

    nodes = []
    for number, step in enumerate(steps, 1):
        require(isinstance(step, dict) and set(step) == {
            "kind", "statement", "source_refs", "support_type", "normalization_note"},
            "model must not assign IDs, quotes or code attachments")
        require(step["kind"] in ("given", "knowledge", "derived") and text(step["statement"]),
                "invalid reasoning step")
        require("```" not in step["statement"], "reasoning statement must not be fenced code")
        require(step["support_type"] in ("source_supported", "supplementary"), "support disclosure required")
        require(text(step["normalization_note"]), "normalization note required")
        if step["support_type"] == "supplementary":
            require(step["kind"] != "given", "new inference cannot be a given fact")
        evidence = anchors(step["source_refs"])
        # A proof claim cannot be grounded solely in observing code operations.
        primary = next((a for a in evidence if a["source_field"] != "reference_code"), evidence[0])
        if primary["source_field"] == "reference_code":
            require(step["kind"] == "given" and step["support_type"] == "source_supported",
                    "code-only evidence supports operational observations, not correctness claims")
        nodes.append({"node_id": number, **step, "source_field": primary["source_field"],
                      "source_quote": primary["text"], "source_spans": evidence})
    require(not text_findings(nodes), "positional step references are not reorder-stable")
    validate_code_facts(nodes, item["reference_code"])
    require(isinstance(omissions, list), "omissions must be explicit")
    for omission in omissions:
        require(isinstance(omission, dict) and set(omission) == {"source_refs", "reason"}
                and text(omission["reason"]), "invalid omission record")
        anchors(omission["source_refs"])
    protocol = ("t2ance-lcb-normalize-v1" if output_source_field(item) == "t2ance_output"
                else PROTOCOL)
    return {"protocol": protocol, "nodes": nodes, "omissions": omissions,
            "original_output_sha256": digest(item["raw_output"]),
            "code_sha256": digest(item["reference_code"]),
            "claim": "mechanically valid normalization proposal, pending semantic review"}


def assemble_graph(value, normalized, item):
    require(isinstance(value, dict) and set(value) == {"dependencies", "answer_parents", "answer_justification"},
            "invalid dependency response")
    nodes = normalized["nodes"]
    dependencies = value["dependencies"]
    require(isinstance(dependencies, list) and len(dependencies) == len(nodes), "dependency coverage mismatch")
    graph = []
    for node, dep in zip(nodes, dependencies):
        require(isinstance(dep, dict) and set(dep) == {"node_id", "parents", "justification"}
                and type(dep["node_id"]) is int and dep["node_id"] == node["node_id"],
                "dependency IDs/order changed")
        graph.append({**node, "parents": dep["parents"], "justification": dep["justification"]})
    graph.append({"node_id": len(nodes) + 1, "kind": "answer", "statement": item["reference_code"],
                  "source_field": "reference_code", "source_quote": item["reference_code"],
                  "support_type": "source_supported", "parents": value["answer_parents"],
                  "justification": value["answer_justification"], "excluded_from_pals": True})
    validate_nodes({"nodes": graph}, item["question"], "", extra_sources={
        output_source_field(item): item["raw_output"], "reference_code": item["reference_code"]},
        allow_reference_code_facts=True)
    validate_parents({"parents": [{"node_id": n["node_id"], "parents": n["parents"]} for n in graph]}, graph)
    validate_justifications({"justifications": [{"node_id": n["node_id"], "text": n["justification"]}
                                               for n in graph]}, graph)
    return {"nodes": graph}


def validate_audit(value):
    validate_review(value, "review_dag")
    require(all(k in value["checks"] and (type(value["checks"][k]) is bool or value["checks"][k] is None)
                for k in REVIEW_CHECKS), "missing normalization review check")
    if value["decision"] == "accept":
        require(all(value["checks"][k] is True for k in REVIEW_CHECKS), "unresolved normalization review")
