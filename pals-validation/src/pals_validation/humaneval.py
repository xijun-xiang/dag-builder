"""HumanEval export adapter. Self-contained: no import from dag_builder."""
import re
from .data import validated_steps
from .io import digest


def normalize_humaneval(record):
    if (record.get("schema_version") != "humaneval_validation_export_v1"
            or record.get("model_accepted") is not True or record.get("status") != "model_accepted"):
        raise ValueError("Reviewed HumanEval export required; raw official data is not a DAG")
    source, dag = record["source"], record["dag"]
    if source.get("task_type") != "humaneval" or source.get("subset") != "humaneval":
        raise ValueError("HumanEval source identity required")
    if (dag["source"] != source or dag["item_id"] != record["item_id"]
            or source["item_id"] != record["item_id"] or digest(dag) != record["dag_sha256"]):
        raise ValueError("DAG source, identity or hash mismatch")
    if dag.get("construction_protocol") != "humaneval-reference-v1":
        raise ValueError("Unrecognized HumanEval construction protocol")
    if (source.get("dataset") != "openai/human-eval"
            or not re.fullmatch(r"[0-9a-f]{40}", source.get("revision", ""))
            or not re.fullmatch(r"HumanEval/\d+", source.get("task_id", ""))):
        raise ValueError("Invalid source provenance")
    if (not isinstance(source.get("question"), str) or not source["question"].strip()
            or not isinstance(source.get("entry_point"), str) or not source["entry_point"].isidentifier()
            or not isinstance(source.get("canonical_solution"), str) or not source["canonical_solution"].strip()):
        raise ValueError("Missing prompt, entry point or separated reference")
    review_keys = {"solution_review": ("answer_correct", "intermediate_correct", "premises_complete", "trace_sufficient"),
                   "dag_review": ("statements_correct", "faithful_to_solution", "dependencies_sufficient",
                                  "dependencies_minimal", "justifications_complete", "no_new_facts")}
    for name, keys in review_keys.items():
        review = dag[name]
        if (review.get("decision") != "accept" or review.get("issues") != []
                or not all(review.get("checks", {}).get(k) is True for k in keys)):
            raise ValueError("Unaccepted semantic review")
    nodes = dag["nodes"]
    # One-step questions are valid sources but ineligible for local pairs; retain them.
    steps = validated_steps(nodes, minimum=1)
    if nodes[-1]["kind"] != "answer" or nodes[-1]["statement"] != source["canonical_solution"]:
        raise ValueError("Reference code must be the separated terminal answer")
    for n in steps:
        if (n.get("source_field") not in ("question", "solution") or "```" in n["statement"]
                or "<step>" in n["statement"] or "</step>" in n["statement"]):
            raise ValueError("Reasoning nodes must be prose without framing tags or code-answer sources")
    # Drop answer, tests, justification and quotes: only these fields can reach inference.
    return {"item_id": record["item_id"], "task_id": source["task_id"], "task_type": "humaneval",
            "source_row": source["row"], "domain": "code", "question": source["question"],
            "entry_point": source["entry_point"],
            "steps": [{k: n[k] for k in ("node_id", "kind", "statement", "parents")} for n in steps],
            "adapter": "humaneval_reference_code_explanation_v1", "source_dag_sha256": record["dag_sha256"],
            "human_approved": record.get("human_approved", False)}
