"""Lossless, versioned separation of reasoning structure and answer extraction.

This does not replace strict-tag-v1. Old parses and generations remain immutable.
Only a contiguous sequence of explicit steps followed by an explicit answer
boundary is a complete process. No prose is relabelled or repaired.
"""

from __future__ import annotations

import re

from .protocol import TAGS, parse_trace
from .schema import BENCHMARKS, CODE_BENCHMARKS, require

PARSER_VERSION = "process-answer-separated-v1"
_STEP_TAG = re.compile(r"</?step\b", re.IGNORECASE)
_ANSWER_TAG = re.compile(r"</?answer\b", re.IGNORECASE)
_FENCE = re.compile(r"\A[ \t]*```(?:python|py)?[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*```[ \t\r\n]*\Z", re.DOTALL)
_LABEL = re.compile(r"(?:final[ \t]+)?answer:[ \t]*\r?\n", re.IGNORECASE)


def _empty_answer(reason: str) -> dict:
    return {"valid": False, "text": None, "span": None, "method": None,
            "reason": reason}


def _payload(raw: str, start: int, end: int, benchmark: str, method: str) -> dict:
    """Select one unchanged contiguous raw span; never compile or execute code."""
    body = raw[start:end]
    if not body.strip() or _STEP_TAG.search(body) or _ANSWER_TAG.search(body):
        return _empty_answer("empty_or_nested_answer")
    if benchmark in CODE_BENCHMARKS and "```" in body:
        left = len(body) - len(body.lstrip())
        match = _FENCE.fullmatch(body[left:])
        if match is None or "```" in match["body"]:
            return _empty_answer("ambiguous_code_fence")
        start, end = start + left + match.start("body"), start + left + match.end("body")
        method += "+single_python_fence"
    if not raw[start:end].strip():
        return _empty_answer("empty_answer")
    return {"valid": True, "text": raw[start:end], "span": [start, end],
            "method": method, "reason": None}


def _tagged_answer(raw: str, finish: str, benchmark: str) -> dict:
    # Preserve the previous independent-answer capability even with bad steps.
    # EOF fallback is only used after a verified contiguous process below.
    if raw.count("<answer>") != 1 or raw.count("</answer>") != 1:
        return _empty_answer("no_unique_closed_answer")
    start = raw.index("<answer>") + len("<answer>")
    end = raw.index("</answer>")
    if end < start or len(_ANSWER_TAG.findall(raw)) != 2:
        return _empty_answer("ambiguous_answer_tags")
    if finish not in ("boundary", "eos"):
        return _empty_answer("generation_not_complete")
    return _payload(raw, start, end, benchmark, "closed_answer_tag")


def parse_decoupled(raw: str, finish_reason: str, benchmark: str) -> dict:
    require(isinstance(raw, str), "raw output must be text")
    require(benchmark in BENCHMARKS, "unknown benchmark")
    strict = parse_trace(raw, finish_reason)
    answer = _tagged_answer(raw, finish_reason, benchmark)
    steps, pos, reason, boundary = [], 0, None, None
    while True:
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if not raw.startswith("<step>", pos):
            break
        end = raw.find("</step>", pos + len("<step>"))
        if end < 0:
            reason = "unclosed_step"
            break
        text = raw[pos + len("<step>"):end]
        if not text.strip() or any(tag in text for tag in TAGS) or _STEP_TAG.search(text) or _ANSWER_TAG.search(text):
            reason = "invalid_step_block"
            break
        steps.append({"text": text, "span": [pos + len("<step>"), end],
                      "block_span": [pos, end + len("</step>")]})
        pos = end + len("</step>")

    tail = raw[pos:]
    if reason is None and not steps:
        reason = "no_contiguous_steps_from_start"
    if reason is None and finish_reason not in ("boundary", "eos"):
        reason = "generation_not_complete"
    if reason is None and _STEP_TAG.search(tail):
        reason = "steps_or_step_fragments_after_gap"
    if reason is None:
        if tail.startswith("<answer>"):
            # Even malformed code may delimit a complete reasoning section.
            # Multiple answer blocks or text after a closed answer are ambiguous.
            closes = tail.count("</answer>")
            expected_tags = 1 + closes
            if (tail.count("<answer>") != 1 or closes > 1 or
                    len(_ANSWER_TAG.findall(tail)) != expected_tags):
                reason = "ambiguous_answer_boundary"
            elif closes == 1 and tail[tail.index("</answer>") + len("</answer>"):].strip():
                reason = "text_after_answer"
            elif closes == 0 and finish_reason != "eos":
                reason = "unclosed_answer_without_eos"
            else:
                boundary = {"offset": pos, "kind": "answer_open_tag"}
                if closes == 0:
                    answer = _payload(raw, pos + len("<answer>"), len(raw),
                                      benchmark, "answer_open_to_eos")
        elif benchmark in CODE_BENCHMARKS and finish_reason == "eos":
            label = _LABEL.match(tail)
            fence_offset = pos + (label.end() if label else 0)
            fenced = _FENCE.fullmatch(raw[fence_offset:])
            if fenced is None or "```" in fenced["body"] or _ANSWER_TAG.search(tail):
                reason = "no_unambiguous_answer_boundary"
            else:
                boundary = {"offset": pos, "kind": "labelled_python_fence" if label else "python_fence"}
                answer = _payload(raw, fence_offset + fenced.start("body"),
                                  fence_offset + fenced.end("body"), benchmark,
                                  boundary["kind"])
        else:
            reason = "no_unambiguous_answer_boundary"
    process_valid = reason is None
    return {"schema_version": "pals_e3_decoupled_trace_v1", "parser_version": PARSER_VERSION,
            "raw_text": raw, "finish_reason": finish_reason, "steps": steps,
            "process_valid": process_valid, "reason": reason, "answer": answer,
            "answer_boundary": boundary, "strict_process_valid": strict["process_valid"],
            "strict_reason": strict["reason"],
            "status": {"generation_complete": finish_reason in ("boundary", "eos"),
                       "reasoning_structure_valid": process_valid,
                       "answer_extractable": answer["valid"],
                       "answer_execution": "not_evaluated", "answer_correct": None}}
