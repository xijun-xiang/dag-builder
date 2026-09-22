"""Small explicit data contracts; gold labels never enter solve requests."""

import re
from dataclasses import asdict, dataclass

from .math_answers import parse_numeric_answer


class InvalidOutput(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidOutput(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class Question:
    question: str
    choices: tuple[str, ...]

    def __post_init__(self):
        require(text(self.question), "empty question")
        require(
            len(self.choices) == 4 and all(text(x) for x in self.choices),
            "invalid choices",
        )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MathQuestion:
    question: str

    def __post_init__(self):
        require(text(self.question), "empty question")

    def to_dict(self):
        return asdict(self)


def public_question(item):
    if item.get("task_type") == "livecodebench":
        require(text(item.get("question")), "empty LiveCodeBench question")
        require(item.get("io_type") in ("functional", "stdin"), "invalid LiveCodeBench I/O type")
        return {key: item[key] for key in ("task_type", "question", "io_type", "entry_point", "starter_code")}
    if item.get("task_type") == "humaneval":
        require(text(item.get("question")), "empty HumanEval prompt")
        require(text(item.get("entry_point")) and item["entry_point"].isidentifier(), "invalid entry point")
        return {"task_type": "humaneval", "question": item["question"], "entry_point": item["entry_point"]}
    if item.get("task_type") == "gsm8k":
        return {"task_type": "gsm8k", **MathQuestion(item["question"]).to_dict()}
    return Question(item["question"], tuple(item["choices"])).to_dict()


def validate_code_explanation(value):
    require(isinstance(value, dict) and set(value) == {"rationale"},
            "reference explanation must contain only rationale, not rewritten code")
    require(text(value["rationale"]), "empty algorithm explanation")
    require("```" not in value["rationale"], "algorithm explanation must be prose, not fenced code")


def validate_solution(value):
    require(isinstance(value, dict), "solution must be an object")
    require(value.get("answer") in ("A", "B", "C", "D"), "invalid answer label")
    require(text(value.get("rationale")), "missing rationale")
    difficulty = value.get("estimated_difficulty", {})
    require(isinstance(difficulty, dict), "invalid difficulty")
    require(
        difficulty.get("level") in ("low", "medium", "high"), "invalid difficulty level"
    )
    require(text(difficulty.get("reason")), "missing difficulty rationale")


def parse_native_solution(message):
    """Read the dedicated reasoning field and one terminal answer, without repair."""
    require(isinstance(message, dict), "invalid assistant message")
    reasoning = message.get("reasoning_content")
    content = message.get("content")
    require(text(reasoning), "missing native reasoning_content; no content fallback")
    require(text(content), "missing final response")
    require(reasoning.strip() != content.strip(), "native reasoning and final content are duplicated; no field fallback")
    matches = re.findall(r"^Final answer:[ \t]*([ABCD])[ \t]*$", content, re.MULTILINE)
    require(len(matches) == 1, "expected exactly one Final answer line")
    require(
        re.fullmatch(
            r"Final answer:[ \t]*[ABCD][ \t]*", content.strip().splitlines()[-1]
        )
        is not None,
        "answer line must terminate final response",
    )
    return {
        "answer": matches[0],
        "reasoning_content": reasoning,
        "final_response": content,
        "answer_extraction": "unique_terminal_final_answer_line_v1",
        "reasoning_source": "message.reasoning_content",
    }


def validate_math_solution(value):
    require(isinstance(value, dict), "solution must be an object")
    require(text(value.get("answer")), "missing numeric answer")
    require(
        parse_numeric_answer(value["answer"]) is not None,
        "answer must be one numeric literal",
    )
    require(text(value.get("rationale")), "missing rationale")
    difficulty = value.get("estimated_difficulty", {})
    require(isinstance(difficulty, dict), "invalid difficulty")
    require(
        difficulty.get("level") in ("low", "medium", "high"), "invalid difficulty level"
    )
    require(text(difficulty.get("reason")), "missing difficulty rationale")


def validate_conditioned_math_rationale(value):
    """Validate rationale-only output when the answer was supplied in the request."""
    require(isinstance(value, dict), "solution must be an object")
    require("answer" not in value, "conditioned generation must not repeat the answer field")
    require(text(value.get("rationale")), "missing rationale")
    difficulty = value.get("estimated_difficulty", {})
    require(isinstance(difficulty, dict), "invalid difficulty")
    require(
        difficulty.get("level") in ("low", "medium", "high"), "invalid difficulty level"
    )
    require(text(difficulty.get("reason")), "missing difficulty rationale")


REVIEW_CHECKS = {
    "review_solution": (
        "answer_correct",
        "intermediate_correct",
        "premises_complete",
        "trace_sufficient",
    ),
    "review_dag": (
        "statements_correct",
        "faithful_to_solution",
        "dependencies_sufficient",
        "dependencies_minimal",
        "justifications_complete",
        "no_new_facts",
    ),
}


def validate_review(value, stage):
    require(isinstance(value, dict), "review must be an object")
    require(
        value.get("decision") in ("accept", "reject", "needs_review"),
        "invalid review decision",
    )
    checks = value.get("checks")
    require(isinstance(checks, dict), "missing checks")
    for key in REVIEW_CHECKS[stage]:
        require(
            key in checks and (type(checks[key]) is bool or checks[key] is None),
            "invalid check",
        )
    require(text(value.get("reason")), "review reason is required")
    issues = value.get("issues")
    require(isinstance(issues, list) and all(text(x) for x in issues), "invalid issues")
    if value["decision"] == "accept":
        require(
            all(checks[k] is True for k in REVIEW_CHECKS[stage]) and not issues,
            "accept requires all checks true and no unresolved issues",
        )


def validate_nodes(value, question, rationale, extra_sources=None, *, allow_reference_code_facts=False):
    require(isinstance(value, dict), "nodes output must be an object")
    nodes = value.get("nodes")
    require(isinstance(nodes, list) and len(nodes) >= 2, "at least two nodes required")
    sources = {"question": question, "solution": rationale}
    if extra_sources:
        require(not set(extra_sources) & set(sources), "source field collision")
        sources.update(extra_sources)
    for i, node in enumerate(nodes, 1):
        require(isinstance(node, dict), "invalid node")
        require(
            type(node.get("node_id")) is int and node["node_id"] == i,
            "IDs must be consecutive integers",
        )
        require(
            node.get("kind") in ("given", "knowledge", "derived", "answer"),
            "invalid node kind",
        )
        require(text(node.get("statement")), "empty assertion")
        field = node.get("source_field")
        require(field in sources, "invalid source field")
        if field == "reference_code" and allow_reference_code_facts:
            require(node["kind"] in ("given", "answer"),
                    "reference-code observations must be given, not derived or knowledge")
        elif field in ("correct_answer", "reference_code"):
            require(
                node["kind"] == "answer", "answer label cannot be a reasoning premise"
            )
        quote = node.get("source_quote")
        require(
            text(quote) and quote in sources[field],
            "source quote must occur verbatim in the declared source",
        )
    require(
        nodes[-1]["kind"] == "answer"
        and sum(n["kind"] == "answer" for n in nodes) == 1,
        "exactly one terminal answer node required",
    )


def parse_object(content):
    import json

    require(isinstance(content, str), "response content must be text")
    content = content.strip()
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4]

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite JSON constant")

    try:
        value = json.loads(
            content, object_pairs_hook=unique_fields, parse_constant=reject_constant
        )
    except (ValueError, TypeError):
        raise InvalidOutput("response is not one JSON object") from None
    require(isinstance(value, dict), "response is not an object")
    return value
