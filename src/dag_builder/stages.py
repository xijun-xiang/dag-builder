"""Pure stage input construction, prompts and output validation."""

import json
from importlib.resources import files

from .schemas import (
    public_question,
    require,
    validate_conditioned_math_rationale,
    validate_math_solution,
    validate_nodes,
    validate_review,
    validate_solution,
    validate_code_explanation,
)
from .validation import validate_justifications, validate_parents

STAGES = (
    "solve",
    "review_solution",
    "atomize",
    "dependencies",
    "justify",
    "review_dag",
)
THINKING_STAGES = ("solve", "structure_solution", *STAGES[1:])
REFERENCE_STAGES = STAGES[1:]
REPAIR_STAGES = ("repair", "justify", "review_repair")
REVISION_STAGES = ("revise", "audit", "adjudicate")


def stages_for(config):
    if config.prompt_version == "gpqa-revision-v1":
        return REVISION_STAGES
    if config.prompt_version == "gpqa-repair-v1":
        return REPAIR_STAGES
    if config.task_type == "gpqa":
        return REFERENCE_STAGES
    return THINKING_STAGES if config.prompt_version == "mmlu-thinking-v1" else STAGES


def prompt(
    stage, version="v1", task_type="mmlu", solution_source="independent_generation"
):
    allowed = (
        REVISION_STAGES
        if version == "gpqa-revision-v1"
        else REPAIR_STAGES
        if version == "gpqa-repair-v1"
        else REFERENCE_STAGES
        if task_type == "gpqa"
        else THINKING_STAGES
        if version == "mmlu-thinking-v1"
        else STAGES
    )
    if stage not in allowed:
        raise ValueError("unknown stage or prompt version")
    if task_type == "mmlu" and version not in ("v1", "mmlu-thinking-v1"):
        raise ValueError("unsupported mmlu prompt version")
    if task_type == "gsm8k" and version != "gsm8k-v1":
        raise ValueError("gsm8k requires prompt version gsm8k-v1")
    if task_type == "gpqa" and version not in (
        "gpqa-reference-v1",
        "gpqa-repair-v1",
        "gpqa-revision-v1",
    ):
        raise ValueError("gpqa requires the official-reference protocol")
    if task_type == "humaneval" and version != "humaneval-reference-v1":
        raise ValueError("humaneval requires the reference-code protocol")
    if task_type not in ("mmlu", "gsm8k", "gpqa", "humaneval"):
        raise ValueError("unknown task type")
    filename = (
        "solve-diagnostic-repair.md"
        if stage == "solve" and solution_source == "diagnostic_repair_generation"
        else "solve-answer-conditioned.md"
        if stage == "solve" and solution_source == "answer_conditioned_generation"
        else stage + ".md"
    )
    location = files("dag_builder").joinpath("prompts", version, filename)
    if version == "gpqa-repair-v1" and stage == "justify":
        location = files("dag_builder").joinpath(
            "prompts", "gpqa-reference-v1", "justify.md"
        )
    if version == "mmlu-thinking-v1" and not location.is_file():
        location = files("dag_builder").joinpath("prompts", "v1", stage + ".md")
    return location.read_text(encoding="utf-8")


def reference_solution(item, results):
    """Official references are source data, never a fabricated solve completion."""
    if item.get("task_type") == "humaneval":
        return {"answer": item["canonical_solution"], "rationale": results["solve"]["rationale"],
                "origin": "model_explanation_of_official_code", "reference_execution": "not_executed"}
    if item.get("task_type") == "gpqa":
        return {
            "answer": item["gold_answer"],
            "rationale": item["official_explanation"],
            "origin": "official_expert_explanation",
            "source_field": item["source_fields"]["Explanation"],
        }
    if "structure_solution" in results:
        return results["structure_solution"]
    return results["solve"]


def stage_input(stage, item, results, solution_source="independent_generation"):
    data = {"question": public_question(item)}
    if item.get("task_type") == "humaneval":
        require(stage in STAGES, "unknown HumanEval stage")
        # Tests are intentionally absent; allowlist fields instead of copying source.
        data["reference_code"] = item["canonical_solution"]
        data["reference_execution"] = "not_executed"
        if stage != "solve":
            data["solution"] = reference_solution(item, results)
        if stage == "atomize":
            data["reference_sources"] = {"reference_code": item["canonical_solution"]}
        if stage in ("dependencies", "justify", "review_dag"):
            data["nodes"] = results["atomize"]["nodes"]
        if stage in ("justify", "review_dag"):
            data["parents"] = results["dependencies"]["parents"]
        if stage == "review_dag":
            data["justifications"] = results["justify"]["justifications"]
        return data
    if item.get("task_type") == "gpqa":
        require(stage in REFERENCE_STAGES, "official-reference protocol has no solve")
        data["reference_sources"] = {
            "correct_answer": item["choices"]["ABCD".index(item["gold_answer"])],
            **{
                f"choice_{label}": value
                for label, value in zip("ABCD", item["choices"])
            },
        }
    if stage == "structure_solution":
        data["native_solution"] = results["solve"]
        return data
    if stage == "solve" and solution_source in (
        "answer_conditioned_generation",
        "diagnostic_repair_generation",
    ):
        data["reference_answer"] = item["gold_answer"]
    if stage == "solve" and solution_source == "diagnostic_repair_generation":
        data["draft_rationale"] = item["repair_draft_rationale"]
        data["repair_diagnosis"] = item["repair_diagnosis"]
    if stage != "solve":
        data["solution"] = reference_solution(item, results)
    if stage in ("review_solution", "review_dag") and "structure_solution" in results:
        data["native_solution"] = results["solve"]
    if stage in ("review_solution", "review_dag"):
        data["reference_answer"] = item["gold_answer"]
    if stage in ("dependencies", "justify", "review_dag"):
        data["nodes"] = results["atomize"]["nodes"]
    if stage in ("justify", "review_dag"):
        data["parents"] = results["dependencies"]["parents"]
    if stage == "review_dag":
        data["justifications"] = results["justify"]["justifications"]
    return data


def payload(stage, data, config):
    native = config.prompt_version == "mmlu-thinking-v1"
    if native or config.task_type == "gpqa":
        # Explicit labels in every model request; preserve the original item bytes.
        data = dict(data, question=dict(data["question"]))
        data["question"]["choices"] = dict(zip("ABCD", data["question"]["choices"]))
    request = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "messages": [
            {
                "role": "system",
                "content": prompt(
                    stage,
                    config.prompt_version,
                    config.task_type,
                    config.solution_source,
                ),
            },
            {
                "role": "user",
                "content": json.dumps(data, ensure_ascii=False, sort_keys=True),
            },
        ],
    }
    if config.thinking is not None:
        request["thinking"] = {"type": config.thinking}
    if config.thinking == "enabled":
        request.pop(
            "temperature"
        )  # Official thinking mode ignores sampling temperature.
    if config.reasoning_effort is not None:
        request["reasoning_effort"] = config.reasoning_effort
    if config.response_format is not None and not (native and stage == "solve"):
        request["response_format"] = {"type": config.response_format}
    return request


def validate(stage, value, data, solution_source="independent_generation"):
    if stage == "structure_solution":
        validate_solution(value)
        require(
            value["answer"] == data["native_solution"]["answer"],
            "structured answer changed; no repair allowed",
        )
    elif stage == "solve":
        if data["question"].get("task_type") == "humaneval":
            validate_code_explanation(value)
        elif solution_source in (
            "answer_conditioned_generation",
            "diagnostic_repair_generation",
        ):
            validate_conditioned_math_rationale(value)
        elif data["question"].get("task_type") == "gsm8k":
            validate_math_solution(value)
        else:
            validate_solution(value)
    elif stage in ("review_solution", "review_dag"):
        validate_review(value, stage)
    elif stage == "atomize":
        validate_nodes(
            value,
            data["question"]["question"],
            data["solution"]["rationale"],
            extra_sources=data.get("reference_sources"),
        )
        if data["question"].get("task_type") == "humaneval":
            require(value["nodes"][-1]["statement"] == data["reference_code"],
                    "terminal answer must preserve reference completion verbatim")
            require(value["nodes"][-1]["source_field"] == "reference_code", "code answer source required")
            require(all("```" not in n["statement"] for n in value["nodes"][:-1]),
                    "algorithm steps must not be fenced code")
    elif stage == "dependencies":
        validate_parents(value, data["nodes"])
    elif stage == "justify":
        validate_justifications(value, data["nodes"])
    else:
        raise ValueError("unknown stage")
