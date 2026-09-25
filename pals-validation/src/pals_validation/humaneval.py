"""HumanEval export adapter. Self-contained: no import from dag_builder."""
import re
from .data import ancestors, validated_steps
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
    protocol = dag.get("construction_protocol")
    if protocol not in ("humaneval-reference-v1", "humaneval-reference-v2", "humaneval-reference-v3", "humaneval-reference-v4", "humaneval-reference-v5"):
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
        if protocol in ("humaneval-reference-v4", "humaneval-reference-v5"):
            keys = (*keys, "root_premises_sound", "reference_behavior_faithful")
            if name == "dag_review":
                keys = (*keys, "self_contained_statements", "no_invariant_assumed")
                if protocol == "humaneval-reference-v5":
                    keys = (*keys, "code_facts_grounded")
        review = dag[name]
        if (review.get("decision") != "accept" or review.get("issues") != []
                or not all(review.get("checks", {}).get(k) is True for k in keys)):
            raise ValueError("Unaccepted semantic review")
    nodes = dag["nodes"]
    # One-step questions are valid sources but ineligible for local pairs; retain them.
    steps = validated_steps(nodes, minimum=1)
    if nodes[-1]["kind"] != "answer" or nodes[-1]["statement"] != source["canonical_solution"]:
        raise ValueError("Reference code must be the separated terminal answer")
    if protocol == "humaneval-reference-v5":
        # This adapter is deliberately standalone. Recheck provenance rather than
        # trusting a model-accepted label, and never pass code quotations to PALS.
        sources = {"question": source["question"],
                   "solution": dag.get("reference_solution", {}).get("rationale", ""),
                   "reference_code": source["canonical_solution"]}
        if not all(isinstance(text, str) and text.strip() for text in sources.values()):
            raise ValueError("HumanEval v5 requires complete source texts")
        if ancestors(nodes, nodes[-1]["node_id"]) != {n["node_id"] for n in nodes[:-1]}:
            raise ValueError("HumanEval v5 nodes must contribute to the answer")
        for node in nodes:
            if bool(node["parents"]) != (node["kind"] in ("derived", "answer")):
                raise ValueError("HumanEval v5 premise classification mismatch")
            field, quote = node.get("source_field"), node.get("source_quote")
            if (field not in sources or not isinstance(quote, str) or not quote.strip()
                    or quote not in sources[field]):
                raise ValueError("HumanEval v5 source quotation mismatch")
        if nodes[-1]["source_field"] != "reference_code":
            raise ValueError("Reference answer source required")
    code_lines = {line.strip() for line in source["canonical_solution"].splitlines() if line.strip()}
    for n in steps:
        if protocol in ("humaneval-reference-v4", "humaneval-reference-v5") and re.search(
                r"\b(?:steps?|nodes?|statements?)\s*(?:#\s*)?\d+\b|第\s*\d+\s*(?:步|个?节点)",
                n["statement"], re.I):
            raise ValueError("HumanEval statements must not contain positional references")
        code_fact = (protocol == "humaneval-reference-v5" and n.get("source_field") == "reference_code"
                     and n["kind"] == "given" and n["parents"] == [])
        if ((n.get("source_field") not in ("question", "solution") and not code_fact) or "```" in n["statement"]
                or "<step>" in n["statement"] or "</step>" in n["statement"]):
            raise ValueError("Reasoning nodes must be prose without framing tags or code-answer sources")
        if protocol == "humaneval-reference-v5" and (n["statement"].strip() in code_lines
                or n["statement"].strip() == source["canonical_solution"].strip()):
            raise ValueError("Reasoning nodes must not copy code lines or the complete answer")
    # Drop answer, tests, justification and quotes: only these fields can reach inference.
    return {"item_id": record["item_id"], "task_id": source["task_id"], "task_type": "humaneval",
            "source_row": source["row"], "domain": "code", "question": source["question"],
            "entry_point": source["entry_point"],
            "steps": [{k: n[k] for k in ("node_id", "kind", "statement", "parents")} for n in steps],
            "adapter": "humaneval_reference_code_explanation_v1", "source_dag_sha256": record["dag_sha256"],
            "human_approved": record.get("human_approved", False)}
