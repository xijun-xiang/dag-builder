"""Standalone reader for pals_dag_unified_v1; no dag-builder dependency."""

from .io import digest


# Kept local so this reader remains installable without dag-builder. An
# integration test checks equality with dag_builder.mmlu_catalog.MMLU_SUBJECTS.
MMLU_SUBSETS = frozenset({
    "abstract_algebra", "anatomy", "astronomy", "business_ethics",
    "clinical_knowledge", "college_biology", "college_chemistry",
    "college_computer_science", "college_mathematics", "college_medicine",
    "college_physics", "computer_security", "conceptual_physics",
    "econometrics", "electrical_engineering", "elementary_mathematics",
    "formal_logic", "global_facts", "high_school_biology",
    "high_school_chemistry", "high_school_computer_science",
    "high_school_european_history", "high_school_geography",
    "high_school_government_and_politics", "high_school_macroeconomics",
    "high_school_mathematics", "high_school_microeconomics",
    "high_school_physics", "high_school_psychology",
    "high_school_statistics", "high_school_us_history",
    "high_school_world_history", "human_aging", "human_sexuality",
    "international_law", "jurisprudence", "logical_fallacies",
    "machine_learning", "management", "marketing", "medical_genetics",
    "miscellaneous", "moral_disputes", "moral_scenarios", "nutrition",
    "philosophy", "prehistory", "professional_accounting",
    "professional_law", "professional_medicine", "professional_psychology",
    "public_relations", "security_studies", "sociology",
    "us_foreign_policy", "virology", "world_religions",
})


def _normalize_gsm8k_mmlu(record, benchmark, steps):
    """Validate the two released reasoning-task formats without repairing their DAGs."""
    problem, answer, source = record["problem"], record["answer"], record["provenance"]
    review = record["review"]
    subset, row = source["subset"], source["source_row"]
    if (not isinstance(record.get("item_id"), str) or not record["item_id"].strip()
            or review.get("source_status") != "model_accepted"
            or source.get("split") != "test" or problem.get("entry_point") is not None
            or record["dag"]["nodes"][-1]["kind"] != "answer"):
        raise ValueError("Invalid unified reasoning-task identity or answer boundary")
    if benchmark == "gsm8k":
        if (source.get("dataset") != "openai/gsm8k" or subset != "main"
                or source.get("source_id") != f"openai/gsm8k:main:test:{row}"
                or problem.get("domain") != "grade_school_math"
                or problem.get("choices") is not None
                or answer.get("kind") != "text"
                or not isinstance(answer.get("value"), str) or not answer["value"].strip()):
            raise ValueError("Invalid unified GSM8K problem or provenance")
        choices = None
    else:
        choices = problem.get("choices")
        if (source.get("dataset") != "cais/mmlu" or subset not in MMLU_SUBSETS
                or source.get("source_id") != f"cais/mmlu:{subset}:test:{row}"
                or problem.get("domain") != subset
                or not isinstance(choices, list) or len(choices) != 4
                or any(not isinstance(choice, str) or not choice.strip() for choice in choices)
                or answer.get("kind") != "choice" or answer.get("value") not in ("A", "B", "C", "D")):
            raise ValueError("Invalid unified MMLU problem or provenance")
    return {"item_id": record["item_id"], "source_row": row, "source_id": source["source_id"],
            "source_dag_sha256": source.get("source_dag_sha256"), "domain": problem["domain"],
            "question": problem["question"], "choices": choices, "steps": steps,
            "task_type": benchmark, "subset": subset, "adapter": "pals_dag_unified_v1",
            "human_approved": review["human_approved"]}


def normalize_unified(record, benchmark):
    from .data import validated_steps

    expected = {"gpqa": "gpqa_diamond", "humaneval": "humaneval",
                "livecodebench": "livecodebench_v6", "gsm8k": "gsm8k",
                "mmlu": "mmlu"}.get(benchmark)
    if expected is None or record.get("schema_version") != "pals_dag_unified_v1":
        raise ValueError("Unknown unified benchmark or schema version")
    if set(record) != {"schema_version", "item_id", "benchmark", "problem", "answer",
                       "dag", "review", "provenance"} or record["benchmark"] != expected:
        raise ValueError("Unified record fields or benchmark mismatch")
    problem, answer, graph = record["problem"], record["answer"], record["dag"]
    review, source = record["review"], record["provenance"]
    expected_subset = {"livecodebench": "v6", "gsm8k": "main"}.get(benchmark, expected)
    if (review.get("model_accepted") is not True or type(review.get("human_approved")) is not bool
            or not isinstance(review.get("source_status"), str)
            or (benchmark != "mmlu" and source.get("subset") != expected_subset)
            or (benchmark == "mmlu" and source.get("subset") not in MMLU_SUBSETS)
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
    steps = validated_steps(nodes, minimum=1 if benchmark in ("humaneval", "livecodebench") else 2)
    if not isinstance(problem.get("question"), str) or not problem["question"].strip():
        raise ValueError("Missing question")
    if benchmark in ("gsm8k", "mmlu"):
        return _normalize_gsm8k_mmlu(record, benchmark, steps)
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
            or problem.get("choices") is not None):
        raise ValueError("Invalid unified code problem")
    if benchmark == "livecodebench":
        entry = problem.get("entry_point")
        if (not isinstance(source.get("source_id"), str) or not source["source_id"].strip()
                or not isinstance(source.get("source_dag_sha256"), str)
                or len(source["source_dag_sha256"]) != 64
                or (entry is not None and (not isinstance(entry, str) or not entry.isidentifier()))):
            raise ValueError("Invalid unified LiveCodeBench problem")
        return {"item_id": record["item_id"], "task_id": source["source_id"],
                "task_type": "livecodebench", "io_type": "stdin" if entry is None else "functional",
                "source_row": source["source_row"], "domain": problem["domain"],
                "question": problem["question"], "entry_point": entry,
                "steps": [{key: node[key] for key in ("node_id", "kind", "statement", "parents")}
                          for node in steps], "adapter": "pals_dag_unified_v1",
                "source_dag_sha256": source["source_dag_sha256"],
                "human_approved": review["human_approved"]}
    if (not isinstance(problem.get("entry_point"), str)
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
