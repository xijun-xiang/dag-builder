"""Versioned full-trajectory prompt and lossless tag parsing."""

from __future__ import annotations

import re

from .schema import CODE_BENCHMARKS, validate_problem

SYSTEM_PROMPT = (
    "Solve the task. Express your reasoning as a sequence of coherent steps. "
    "Put each reasoning step inside <step>...</step>. Use as many steps as the "
    "solution actually needs; do not split a sentence merely to create more "
    "steps. Then put the final answer inside one <answer>...</answer> block. "
    "Do not write text outside these blocks. Stop after </answer>."
)
SUFFIX = {
    "gpqa": "In <answer>, write only the chosen option label.",
    "mmlu": "In <answer>, write only the chosen option label.",
    "gsm8k": "In <answer>, write only the final numerical answer.",
    "humaneval": "Explain the algorithm in <step> blocks. In <answer>, write a complete Python "
                 "function implementation, not a Markdown code fence.",
    "livecodebench": "Explain the algorithm in <step> blocks. In <answer>, write complete "
                     "Python code, not a Markdown code fence.",
}
ANSWER_PATTERN = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)
TAGS = ("<step>", "</step>", "<answer>", "</answer>")


def messages(problem: dict) -> list[dict]:
    validate_problem(problem)
    task = problem["benchmark"]
    question = problem["question"]
    if problem["choices"] is not None:
        question += "\n\n" + "\n".join(
            f"{chr(65 + i)}. {choice}" for i, choice in enumerate(problem["choices"]))
    if problem["starter_code"]:
        question += "\n\nStarter code:\n" + problem["starter_code"]
    # LCB question text can already contain sample I/O; never insert private tests.
    if task == "livecodebench" and problem["public_examples"] and "Sample Input" not in question:
        question += "\n\nPublic examples:\n" + "\n".join(
            f"Input: {case['input']}\nOutput: {case['output']}"
            for case in problem["public_examples"])
    return [{"role": "system", "content": SYSTEM_PROMPT + " " + SUFFIX[task]},
            {"role": "user", "content": question}]


def _answer_independently(raw: str) -> dict:
    matches = list(ANSWER_PATTERN.finditer(raw))
    if len(matches) != 1 or not matches[0].group(1).strip():
        return {"valid": False, "text": None, "span": None}
    match = matches[0]
    return {"valid": True, "text": match.group(1),
            "span": [match.start(1), match.end(1)]}


def parse_trace(raw: str, finish_reason: str) -> dict:
    if not isinstance(raw, str):
        raise TypeError("raw output must be text")
    answer = _answer_independently(raw)
    steps, pos, error = [], 0, None
    while pos < len(raw):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos == len(raw):
            break
        if raw.startswith("<answer>", pos):
            end = raw.find("</answer>", pos + 8)
            if end < 0:
                error = "unclosed_answer"
                break
            content = raw[pos + 8:end]
            if not content.strip() or any(tag in content for tag in TAGS):
                error = "invalid_answer_block"
                break
            pos = end + len("</answer>")
            if raw[pos:].strip():
                error = "trailing_content"
            break
        if not raw.startswith("<step>", pos):
            error = "unexpected_text_or_tag"
            break
        end = raw.find("</step>", pos + 6)
        if end < 0:
            error = "unclosed_step"
            break
        content = raw[pos + 6:end]
        if not content.strip() or any(tag in content for tag in TAGS):
            error = "invalid_step_block"
            break
        steps.append({"text": content, "span": [pos + 6, end],
                      "block_span": [pos, end + len("</step>")]})
        pos = end + len("</step>")
    if error is None and not steps:
        error = "no_step"
    if error is None and not answer["valid"]:
        error = "no_unique_answer"
    if error is None and finish_reason not in ("boundary", "eos", "length"):
        error = "unknown_finish_reason"
    if error is None and raw.rstrip()[-len("</answer>"):] != "</answer>":
        error = "answer_not_last"
    return {"schema_version": "pals_e3_trace_v1", "raw_text": raw,
            "finish_reason": finish_reason, "steps": steps, "answer": answer,
            "process_valid": error is None, "reason": error}


def score_text_pair(base_prompt: str, parsed: dict, step_index: int) -> tuple[str, str, str]:
    """Return full context, deleted context, shared target text for index >= 1."""
    if not parsed["process_valid"]:
        raise ValueError("Only complete process may be scored")
    steps = parsed["steps"]
    if not 1 <= step_index < len(steps):
        raise IndexError("Step index must have an immediate predecessor")
    target, previous = steps[step_index], steps[step_index - 1]
    raw = parsed["raw_text"]
    full_prefix = raw[:target["span"][0]]
    left, right = previous["block_span"]
    deleted_prefix = raw[:left] + raw[right:target["span"][0]]
    if full_prefix[:left] + full_prefix[right:] != deleted_prefix:
        raise AssertionError("Deletion changed more than the predecessor")
    return base_prompt + full_prefix, base_prompt + deleted_prefix, target["text"]
