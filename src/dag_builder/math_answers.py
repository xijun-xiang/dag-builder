"""GSM8K final-answer extraction and conservative numeric equivalence checks."""

import re
from fractions import Fraction


FINAL_ANSWER = re.compile(r"(?m)^####\s*(?P<answer>[^\r\n]+?)\s*$")
BOXED_ANSWER = re.compile(r"^\\boxed\{(?P<answer>.*)\}$")
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
FRACTION = re.compile(r"^[+-]?\d+\s*/\s*[+-]?\d+$")


def extract_gsm8k_answer(raw_answer: str) -> str:
    """Extract the final answer after the canonical GSM8K ``####`` marker."""
    if not isinstance(raw_answer, str):
        raise ValueError("GSM8K answer must be text")
    matches = FINAL_ANSWER.findall(raw_answer)
    if len(matches) != 1 or not matches[0].strip():
        raise ValueError("GSM8K answer must contain exactly one non-empty #### final answer")
    return matches[0].strip()


def _clean_numeric_text(value: str) -> str:
    text = value.strip().replace(",", "")
    boxed = BOXED_ANSWER.fullmatch(text)
    if boxed:
        text = boxed.group("answer").strip()
    if text.startswith("$"):
        text = text[1:].strip()
    return text


def parse_numeric_answer(value: str) -> Fraction | None:
    """Parse only simple exact numbers/fractions; return None for ambiguity."""
    if not isinstance(value, str):
        return None
    text = _clean_numeric_text(value)
    if FRACTION.fullmatch(text):
        numerator, denominator = re.split(r"\s*/\s*", text)
        if int(denominator) == 0:
            return None
        return Fraction(int(numerator), int(denominator))
    if NUMBER.fullmatch(text):
        return Fraction(text)
    return None


def numeric_answers_equivalent(left: str, right: str) -> bool:
    """Compare GSM8K answers without accepting arbitrary text extraction."""
    parsed_left = parse_numeric_answer(left)
    parsed_right = parse_numeric_answer(right)
    return parsed_left is not None and parsed_left == parsed_right
