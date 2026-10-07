"""Explicit-boundary extraction for greedy E3; no text correction or sentence splitting.

An omitted step closing tag is accepted only at the next explicit opening step
or answer tag. This defines a deterministic segmentation, not a semantic repair.
Old parsers remain unchanged. All spans refer to the original raw string.
"""
import re

from .decoupled import parse_decoupled
from .schema import require

V1_VERSION = "explicit-step-boundaries-v1"
VERSION = "explicit-step-boundaries-v2-title-body"
_MARKER = re.compile(r"</?(?:step|answer)\b")


def parse_v1(raw: str, finish: str, benchmark: str) -> dict:
    old = parse_decoupled(raw, finish, benchmark)
    result = {**old, "parser_version": V1_VERSION, "segmentation_methods": []}
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


def parse(raw: str, finish: str, benchmark: str) -> dict:
    """Keep v1-valid spans exact; recover only explicit title/body step blocks.

    A closed title followed by prose and the next explicit opening tag is one
    step. Its target is one unchanged contiguous span, including the internal
    closing delimiter. We neither drop prose nor move the delimiter in raw.
    This format is labelled separately for sensitivity analyses. No implicit
    sentence boundary, preface, truncated reasoning or guessed answer is used.
    """
    old = parse_v1(raw, finish, benchmark)
    result = {**old, "parser_version": VERSION}
    if old["process_valid"] or old["reason"] != "no_explicit_step_or_answer_boundary":
        return result
    steps, methods, pos = [], [], 0
    while True:
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if raw.startswith("<answer>", pos):
            break
        if not raw.startswith("<step>", pos):
            return result
        start = pos + len("<step>")
        marker = _MARKER.search(raw, start)
        if marker is None or not raw[start:marker.start()].strip():
            return result
        end = marker.start()
        if raw.startswith("</step>", end):
            right = end + len("</step>")
            following = _MARKER.search(raw, right)
            if following is None:
                return result  # no explicit boundary: never salvage truncation
            next_pos = following.start()
            if not (raw.startswith("<step>", next_pos) or raw.startswith("<answer>", next_pos)):
                return result
            if raw[right:next_pos].strip():
                end, right, method = next_pos, next_pos, "title_and_body_explicit_boundary"
            else:
                method = "closed_tag"
        elif raw.startswith("<step>", end) or raw.startswith("<answer>", end):
            right, method = end, "next_explicit_open_tag"
        else:
            return result
        steps.append({"text": raw[start:end], "span": [start, end], "block_span": [pos, right]})
        methods.append(method)
        pos = right
    tail = raw[pos:]
    if (not steps or "title_and_body_explicit_boundary" not in methods or
            finish not in ("boundary", "eos", "length") or tail.count("<answer>") != 1 or
            tail.count("</answer>") > 1 or re.search(r"</?step\b", tail) or
            ("</answer>" in tail and tail.split("</answer>", 1)[1].strip()) or
            ("</answer>" not in tail and finish not in ("eos", "length"))):
        return result
    result.update(steps=steps, process_valid=True, reason=None, segmentation_methods=methods,
                  answer_boundary={"offset": pos, "kind": "answer_open_tag"})
    result["status"] = {**old["status"], "reasoning_structure_valid": True}
    for step in steps:
        require(raw[slice(*step["span"])] == step["text"], "non-lossless title/body extraction")
    return result
