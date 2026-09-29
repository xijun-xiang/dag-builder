"""Deterministic answer extraction; code execution requires a pinned sandbox adapter."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from .schema import require

CHOICE_PATTERN = re.compile(r"\s*([A-Z])\.?\s*", re.ASCII)
NUMBER_PATTERN = re.compile(r"\s*([+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?(?:\s*/\s*[+-]?\d+)?)\s*", re.ASCII)


def numeric(value: str) -> Fraction | None:
    match = NUMBER_PATTERN.fullmatch(value)
    if not match:
        return None
    text = match.group(1).replace(",", "").replace(" ", "")
    try:
        if "/" in text:
            left, right = text.split("/", 1)
            if Decimal(right) == 0:
                return None
            return Fraction(Decimal(left)) / Fraction(Decimal(right))
        return Fraction(Decimal(text))
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return None


def evaluate_answer(problem: dict, gold: dict, answer: dict, *, code_policy: dict | None = None,
                    scratch: Path | None = None) -> dict:
    """No text outside the independently parsed answer block may influence grading."""
    if gold.get("outcome_eligible") is False:
        return {"status": "ambiguous_reference", "correct": None,
                "reason": gold.get("ineligibility_reason")}
    if not answer["valid"]:
        return {"status": "invalid_answer", "correct": False, "reason": "missing_or_ambiguous_block"}
    text = answer["text"].strip()
    if gold["kind"] == "choice":
        match = CHOICE_PATTERN.fullmatch(text.upper())
        if match is None or ord(match.group(1)) - 65 >= len(problem["choices"]):
            return {"status": "invalid_answer", "correct": False, "reason": "choice_parse"}
        selected = match.group(1)
        return {"status": "correct" if selected == gold["value"] else "incorrect",
                "correct": selected == gold["value"], "parsed_answer": selected}
    if gold["kind"] == "numeric":
        selected, reference = numeric(text), numeric(gold["value"])
        require(reference is not None, "invalid frozen numeric reference")
        if selected is None:
            return {"status": "invalid_answer", "correct": False, "reason": "numeric_parse"}
        return {"status": "correct" if selected == reference else "incorrect",
                "correct": selected == reference, "parsed_answer": str(selected)}
    if gold["kind"] in ("humaneval_code", "lcb_code"):
        name = "human_eval_code" if gold["kind"] == "humaneval_code" else "livecodebench_code"
        approved = "official_tests_seccomp_v1" if name == "human_eval_code" else "official_compatible_tests_seccomp_v1"
        if not code_policy or code_policy.get(name) != approved:
            return {"status": "harness_unsupported", "correct": None,
                    "reason": "isolated_evaluator_not_frozen"}
        if scratch is None:
            raise RuntimeError("Approved code evaluator needs isolated Slurm scratch")
        from .code_harness import POLICY, grade_code_answer
        from .code_tests import __file__ as decoder_file
        from ..io import sha256
        from . import code_harness
        require(code_policy.get("harness_policy") == POLICY and
                code_policy.get("harness_sha256") == sha256(code_harness.__file__) and
                code_policy.get("decoder_sha256") == sha256(decoder_file),
                "Frozen code evaluator changed")
        return grade_code_answer(problem, gold, text, scratch)
    raise ValueError("unknown grading kind")
