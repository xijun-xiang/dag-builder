"""Standalone reader for pals_dag_unified_v1; no dag-builder dependency."""

from .io import digest


def normalize_unified(record, benchmark):
    from .data import validated_steps

    expected = {"gpqa": "gpqa_diamond", "humaneval": "humaneval"}.get(benchmark)
    if expected is None or record.get("schema_version") != "pals_dag_unified_v1":
        raise ValueError("Unknown unified benchmark or schema version")
    if set(record) != {"schema_version", "item_id", "benchmark", "problem", "answer",
                       "dag", "review", "provenance"} or record["benchmark"] != expected:
        raise ValueError("Unified record fields or benchmark mismatch")
    problem, answer, graph = record["problem"], record["answer"], record["dag"]
    review, source = record["review"], record["provenance"]
    if (review.get("model_accepted") is not True or type(review.get("human_approved")) is not bool
            or not isinstance(review.get("source_status"), str)
            or source.get("subset") != expected
            or not isinstance(source.get("source_row"), int)
            or type(source["source_row"]) is bool
            or source["source_row"] < 0):
        raise ValueError("Unaccepted or malformed unified provenance")
    if (graph.get("schema_version") != "pals_step_dag_v1"
            or digest(graph["nodes"]) != graph.get("nodes_sha256")):
        raise ValueError("Unified DAG hash or version mismatch")
    nodes = graph["nodes"]
    if any(set(node) != {"node_id", "kind", "statement", "parents",
                             "source_field", "source_quote", "justification"} for node in nodes):
        raise ValueError("Unified node fields mismatch")
    steps = validated_steps(nodes, minimum=1 if benchmark == "humaneval" else 2)
    if not isinstance(problem.get("question"), str) or not problem["question"].strip():
        raise ValueError("Missing question")
    if benchmark == "gpqa":
        if (answer.get("kind") != "choice" or answer.get("value") not in ("A", "B", "C", "D")
                or not isinstance(problem.get("choices"), list) or len(problem["choices"]) != 4
                or any(not isinstance(c, str) or not c.strip() for c in problem["choices"])
                or problem.get("entry_point") is not None):
            raise ValueError("Invalid unified GPQA problem")
        # The historical diagnostic fallback attached parents but did not
        # include justifications in the prepared case. Keep that case contract
        # identical even though the unified archival row preserves them.
        scoring_steps = ([{key: value for key, value in node.items() if key != "justification"}
                          for node in steps] if review["source_status"] == "model_accepted_diagnostic"
                         else steps)
        return {"item_id": record["item_id"], "source_row": source["source_row"],
                "domain": problem["domain"], "question": problem["question"],
                "choices": problem["choices"], "steps": scoring_steps,
                "adapter": "pals_dag_unified_v1", "human_approved": review["human_approved"]}
    if (answer.get("kind") != "code" or not isinstance(answer.get("value"), str)
            or nodes[-1]["statement"] != answer["value"]
            or problem.get("choices") is not None
            or not isinstance(problem.get("entry_point"), str)
            or not problem["entry_point"].isidentifier()
            or not isinstance(source.get("source_id"), str)
            or not source["source_id"].startswith("HumanEval/")):
        raise ValueError("Invalid unified HumanEval problem")
    return {"item_id": record["item_id"], "task_id": source["source_id"],
            "task_type": "humaneval", "source_row": source["source_row"],
            "domain": problem["domain"], "question": problem["question"],
            "entry_point": problem["entry_point"],
            "steps": [{key: node[key] for key in ("node_id", "kind", "statement", "parents")}
                      for node in steps],
            "adapter": "pals_dag_unified_v1", "source_dag_sha256": source["source_dag_sha256"],
            "human_approved": review["human_approved"]}
