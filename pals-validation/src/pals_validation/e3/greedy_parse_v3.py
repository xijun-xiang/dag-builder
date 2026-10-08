"""Opt-in candidate: lossless explicit title/body blocks with one redundant close.

The frozen v2 entry point is deliberately unchanged. Promotion requires a new
protocol and cross-model raw replay. No sentence/Markdown splitting, new answer
boundary, repaired truncation, or semantic judgment is performed here.
"""
import re

from .greedy_parse import _MARKER, parse as parse_v2
from .schema import require

VERSION = "explicit-step-boundaries-v3-redundant-close"


def parse(raw: str, finish: str, benchmark: str) -> dict:
    old = parse_v2(raw, finish, benchmark)
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
        start = pos + 6
        marker = _MARKER.search(raw, start)
        if marker is None or not raw[start:marker.start()].strip():
            return result
        end = marker.start()
        if raw.startswith("</step>", end):
            right = end + 7
            following = _MARKER.search(raw, right)
            if following is None:
                return result
            next_pos = following.start()
            if raw.startswith("</step>", next_pos):
                # Exactly: <step>title</step>nonempty body</step> <step/answer>.
                # Keep the internal close in the target; never concatenate spans.
                if not raw[right:next_pos].strip():
                    return result
                end, right = next_pos, next_pos + 7
                following = _MARKER.search(raw, right)
                if following is None or raw[right:following.start()].strip():
                    return result
                next_pos = following.start()
                method = "title_body_redundant_close_boundary"
            elif raw[right:next_pos].strip():
                end, right = next_pos, next_pos
                method = "title_and_body_explicit_boundary"
            else:
                method = "closed_tag"
            if not (raw.startswith("<step>", next_pos) or raw.startswith("<answer>", next_pos)):
                return result
        elif raw.startswith("<step>", end) or raw.startswith("<answer>", end):
            right, method = end, "next_explicit_open_tag"
        else:
            return result
        steps.append({"text": raw[start:end], "span": [start, end], "block_span": [pos, right]})
        methods.append(method)
        pos = right
    tail = raw[pos:]
    if ("title_body_redundant_close_boundary" not in methods or
            finish not in ("boundary", "eos", "length") or tail.count("<answer>") != 1 or
            tail.count("</answer>") > 1 or re.search(r"</?step\b", tail)):
        return result
    if "</answer>" in tail:
        if tail.split("</answer>", 1)[1].strip():
            return result
    elif finish not in ("eos", "length"):
        return result
    result.update(steps=steps, process_valid=True, reason=None, segmentation_methods=methods,
                  answer_boundary={"offset": pos, "kind": "answer_open_tag"})
    result["status"] = {**old["status"], "reasoning_structure_valid": True}
    for step in steps:
        require(raw[slice(*step["span"])] == step["text"], "non-lossless redundant close extraction")
    return result
