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
    if config.task_type == "livecodebench":
        if config.prompt_version in ("calibri-lcb-repair-v1", "calibri-lcb-repair-v2"):
            return ("repair", "dependencies", "review_dag")
        if config.prompt_version in ("calibri-lcb-normalize-v1", "calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3"):
            return ("normalize", "dependencies", "review_dag")
        if config.prompt_version == "livecodebench-editorial-pilot-v1":
            return ("atomize", "review_dag")
        return ("reference_code",) if config.prompt_version == "livecodebench-reference-v1" else STAGES
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
    if task_type == "livecodebench":
        if not ((version == "livecodebench-reference-v1" and stage == "reference_code")
                or (version == "livecodebench-dag-v1" and stage in STAGES)
                or (version == "livecodebench-editorial-pilot-v1" and stage in ("atomize", "review_dag"))
                or (version in ("calibri-lcb-repair-v1", "calibri-lcb-repair-v2") and stage in ("repair", "dependencies", "review_dag"))
                or (version in ("calibri-lcb-normalize-v1", "calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3")
                    and stage in ("normalize", "dependencies", "review_dag"))):
            raise ValueError("unsupported LiveCodeBench stage or protocol")
        if version == "calibri-lcb-normalize-v2" and stage != "normalize":
            version = "calibri-lcb-normalize-v1"
        if version == "calibri-lcb-normalize-v3" and stage != "review_dag":
            version = "calibri-lcb-normalize-v2" if stage == "normalize" else "calibri-lcb-normalize-v1"
        if version == "calibri-lcb-repair-v1" and stage == "dependencies":
            version = "calibri-lcb-normalize-v1"
        if version == "calibri-lcb-repair-v2" and stage in ("dependencies", "review_dag"):
            base_version = "calibri-lcb-normalize-v1" if stage == "dependencies" else "calibri-lcb-repair-v1"
            base = files("dag_builder").joinpath("prompts", base_version, stage + ".md").read_text(encoding="utf-8")
            supplement = files("dag_builder").joinpath("prompts", version, stage + ".md").read_text(encoding="utf-8")
            return base + "\n" + supplement
        return files("dag_builder").joinpath("prompts", version, stage + ".md").read_text(encoding="utf-8")
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
    if task_type == "humaneval" and version not in ("humaneval-reference-v1", "humaneval-reference-v2", "humaneval-reference-v3", "humaneval-reference-v4", "humaneval-reference-v5"):
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
    # v5 changes only atomization and DAG review. Shared v4 prompts stay frozen.
    if version == "humaneval-reference-v5" and stage not in ("atomize", "review_dag"):
        location = files("dag_builder").joinpath("prompts", "humaneval-reference-v4", filename)
    if version == "gpqa-repair-v1" and stage == "justify":
        location = files("dag_builder").joinpath(
            "prompts", "gpqa-reference-v1", "justify.md"
        )
    if version == "mmlu-thinking-v1" and not location.is_file():
        location = files("dag_builder").joinpath("prompts", "v1", stage + ".md")
    return location.read_text(encoding="utf-8")


def reference_solution(item, results):
    """Official references are source data, never a fabricated solve completion."""
    if item.get("task_type") == "livecodebench":
        return {"answer": item["reference_code"], "rationale": results["solve"]["rationale"],
                "origin": "model_explanation_of_test_verified_candidate",
                "reference_execution": "passed_frozen_tests_not_exhaustive_proof"}
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
    if item.get("task_type") in ("humaneval", "livecodebench"):
        require(stage in STAGES, "unknown HumanEval stage")
        # Tests are intentionally absent; allowlist fields instead of copying source.
        is_lcb = item.get("task_type") == "livecodebench"
        data["reference_code"] = item["reference_code"] if is_lcb else item["canonical_solution"]
        data["reference_execution"] = "passed_frozen_tests_not_exhaustive_proof" if is_lcb else "not_executed"
        if stage != "solve":
            data["solution"] = reference_solution(item, results)
        if stage == "atomize":
            data["reference_sources"] = {"reference_code": data["reference_code"]}
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


def request_controls(config, *, native_solve=False):
    """Shared production/probe serialization; never guess provider overrides."""
    request = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.thinking is not None:
        request["thinking"] = {"type": config.thinking}
    if config.thinking == "enabled":
        request.pop(
            "temperature"
        )  # Official thinking mode ignores sampling temperature.
    if config.reasoning_effort is not None:
        request["reasoning_effort"] = config.reasoning_effort
    if config.response_format is not None and not native_solve:
        request["response_format"] = {"type": config.response_format}
    return request


def payload(stage, data, config):
    native = config.prompt_version == "mmlu-thinking-v1"
    if native or config.task_type == "gpqa":
        # Explicit labels in every model request; preserve the original item bytes.
        data = dict(data, question=dict(data["question"]))
        data["question"]["choices"] = dict(zip("ABCD", data["question"]["choices"]))
    request = request_controls(config, native_solve=native and stage == "solve")
    request["messages"] = [
        {
            "role": "system",
            "content": prompt(stage, config.prompt_version, config.task_type, config.solution_source),
        },
        {
            "role": "user",
            "content": json.dumps(data, ensure_ascii=False, sort_keys=True),
        },
    ]
    return request


def validate(stage, value, data, solution_source="independent_generation", *, prompt_version=None):
    if stage == "structure_solution":
        validate_solution(value)
        require(
            value["answer"] == data["native_solution"]["answer"],
            "structured answer changed; no repair allowed",
        )
    elif stage == "solve":
        if data["question"].get("task_type") in ("humaneval", "livecodebench"):
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
            allow_reference_code_facts=(prompt_version in ("humaneval-reference-v5", "livecodebench-dag-v1")
                                        and data["question"].get("task_type") in ("humaneval", "livecodebench")),
        )
        if data["question"].get("task_type") in ("humaneval", "livecodebench"):
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
    if prompt_version in ("humaneval-reference-v4", "humaneval-reference-v5"):
        from .humaneval_quality import validate_quality
        validate_quality(stage, value, data, version=prompt_version)
    if prompt_version == "livecodebench-dag-v1":
        # Reuse exactly the reviewed code-fact/root/positional-reference checks.
        from .humaneval_quality import validate_quality
        validate_quality(stage, value, data, version="humaneval-reference-v5")
