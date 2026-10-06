"""Explicit-boundary extraction for greedy E3; no text correction or sentence splitting.

An omitted step closing tag is accepted only at the next explicit opening step
or answer tag. This defines a deterministic segmentation, not a semantic repair.
Old parsers remain unchanged. All spans refer to the original raw string.
"""
import re

from .decoupled import parse_decoupled
from .schema import require

VERSION = "explicit-step-boundaries-v1"
_MARKER = re.compile(r"</?(?:step|answer)\b")


def parse(raw: str, finish: str, benchmark: str) -> dict:
    old = parse_decoupled(raw, finish, benchmark)
    result = {**old, "parser_version": VERSION, "segmentation_methods": []}
    if old["process_valid"]:
        result["segmentation_methods"] = ["closed_tag"] * len(old["steps"])
        return result
    steps, methods, pos, boundary, failure = [], [], 0, None, None
    while True:
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if raw.startswith("<answer>", pos):
            boundary = pos
            break
        if not raw.startswith("<step>", pos):
            failure = "no_explicit_step_or_answer_boundary"
            break
        start = pos + 6
        marker = _MARKER.search(raw, start)
        if marker is None:
            failure = "unclosed_reasoning_at_end"
            break
        end = marker.start()
        if raw.startswith("</step>", end):
            right, method = end + 7, "closed_tag"
        elif raw.startswith("<step>", end) or raw.startswith("<answer>", end):
            right, method = end, "next_explicit_open_tag"
        elif raw.startswith("</step", end):
            # One known incomplete closing delimiter, immediately before a new block.
            tail = end + 6
            while tail < len(raw) and raw[tail].isspace():
                tail += 1
            if not (raw.startswith("<step>", tail) or raw.startswith("<answer>", tail)):
                failure = "ambiguous_step_delimiter"
                break
            right, method = tail, "incomplete_close_before_explicit_open"
        else:
            failure = "ambiguous_step_delimiter"
            break
        if not raw[start:end].strip():
            failure = "empty_step"
            break
        steps.append({"text": raw[start:end], "span": [start, end], "block_span": [pos, right]})
        methods.append(method)
        pos = right
    if failure is None and not steps:
        failure = "no_step"
    if failure is None and finish not in ("boundary", "eos", "length"):
        failure = "unknown_finish_reason"
    if failure is None:
        tail = raw[boundary:]
        if tail.count("<answer>") != 1 or tail.count("</answer>") > 1 or re.search(r"</?step\b", tail):
            failure = "ambiguous_answer_boundary"
        elif "</answer>" in tail and tail.split("</answer>", 1)[1].strip():
            failure = "text_after_answer"
        elif "</answer>" not in tail and finish not in ("eos", "length"):
            failure = "unfinished_answer_without_eos_or_length"
    valid = failure is None
    result.update(steps=steps, process_valid=valid, reason=failure,
                  segmentation_methods=methods,
                  answer_boundary={"offset": boundary, "kind": "answer_open_tag"} if valid else None)
    result["status"] = {**old["status"], "reasoning_structure_valid": valid}
    # length-ended answers remain invalid even when reasoning is complete.
    for step in steps:
        require(raw[slice(*step["span"])] == step["text"], "non-lossless extraction")
    return result
