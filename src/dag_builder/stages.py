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
NATIVE_PROMPT_VERSIONS = ("mmlu-thinking-v1", "mmlu-thinking-v2")
V2_STAGE_TOKEN_CAPS = {
    "structure_solution": 2048,
    "review_solution": 2048,
    "atomize": 4096,
    "dependencies": 2048,
    "justify": 4096,
    "review_dag": 4096,
}


def stages_for(config):
    if config.prompt_version == "gpqa-revision-v1":
        return REVISION_STAGES
    if config.prompt_version == "gpqa-repair-v1":
        return REPAIR_STAGES
    if config.task_type == "gpqa":
        return REFERENCE_STAGES
    return THINKING_STAGES if config.prompt_version in NATIVE_PROMPT_VERSIONS else STAGES


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
        if version in NATIVE_PROMPT_VERSIONS
        else STAGES
    )
    if stage not in allowed:
        raise ValueError("unknown stage or prompt version")
    if task_type == "mmlu" and version not in ("v1", *NATIVE_PROMPT_VERSIONS):
        raise ValueError("unsupported mmlu prompt version")
    if task_type == "gsm8k" and version not in ("gsm8k-v1", "gsm8k-v2"):
        raise ValueError("gsm8k requires a supported gsm8k prompt version")
    if task_type == "gpqa" and version not in (
        "gpqa-reference-v1",
        "gpqa-repair-v1",
        "gpqa-revision-v1",
    ):
        raise ValueError("gpqa requires the official-reference protocol")
    if task_type not in ("mmlu", "gsm8k", "gpqa"):
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
    fallback_versions = {
        "mmlu-thinking-v1": ("v1",),
        "mmlu-thinking-v2": ("mmlu-thinking-v1", "v1"),
        "gsm8k-v2": ("gsm8k-v1",),
    }.get(version, ())
    for fallback in fallback_versions:
        if location.is_file():
            break
        location = files("dag_builder").joinpath("prompts", fallback, filename)
    return location.read_text(encoding="utf-8")


def reference_solution(item, results):
    """Official references are source data, never a fabricated solve completion."""
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


def stage_input(
    stage,
    item,
    results,
    solution_source="independent_generation",
    prompt_version=None,
):
    data = {"question": public_question(item)}
    if item.get("task_type") == "gpqa":
        require(stage in REFERENCE_STAGES, "official-reference protocol has no solve")
        data["reference_sources"] = {
            "correct_answer": item["choices"]["ABCD".index(item["gold_answer"])],
            **{
                f"choice_{label}": value
                for label, value in zip("ABCD", item["choices"])
            },
        }
    elif (
        item.get("task_type") == "mmlu"
        and prompt_version == "mmlu-thinking-v2"
        and stage in ("atomize", "review_dag")
    ):
        # Choices are public task input, not answer authority.  They are exposed
        # only after solve so the DAG can prove the final label-to-text mapping.
        data["reference_sources"] = {
            f"choice_{label}": value
            for label, value in zip("ABCD", item["choices"])
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
    native = config.prompt_version in NATIVE_PROMPT_VERSIONS
    if native or config.task_type == "gpqa":
        # Explicit labels in every model request; preserve the original item bytes.
        data = dict(data, question=dict(data["question"]))
        data["question"]["choices"] = dict(zip("ABCD", data["question"]["choices"]))
    request = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": (
            min(config.max_tokens, V2_STAGE_TOKEN_CAPS[stage])
            if config.prompt_version == "mmlu-thinking-v2"
            and stage in V2_STAGE_TOKEN_CAPS
            else config.max_tokens
        ),
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
    thinking = config.thinking
    reasoning_effort = config.reasoning_effort
    if config.prompt_version == "mmlu-thinking-v2" and stage != "solve":
        # Native reasoning is required only for solve. Fixed-schema transforms
        # are cheaper and more reliable without hidden long-form reasoning.
        thinking = "disabled"
        reasoning_effort = None
    if thinking is not None:
        request["thinking"] = {"type": thinking}
    if thinking == "enabled":
        request.pop(
            "temperature"
        )  # Official thinking mode ignores sampling temperature.
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
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
        if solution_source in (
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
    elif stage == "dependencies":
        validate_parents(value, data["nodes"])
    elif stage == "justify":
        validate_justifications(value, data["nodes"])
    else:
        raise ValueError("unknown stage")
