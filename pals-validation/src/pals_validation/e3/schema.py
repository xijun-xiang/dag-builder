"""Small, closed contracts for E3. Keep private grading material out of problems."""

from __future__ import annotations

from ..io import digest

BENCHMARKS = frozenset({"gpqa", "gsm8k", "humaneval", "livecodebench", "mmlu"})
CODE_BENCHMARKS = frozenset({"humaneval", "livecodebench"})
EXPECTED_COUNTS = {"gpqa": 198, "gsm8k": 1319, "humaneval": 164,
                   "livecodebench": 175, "mmlu": 1876}
SOURCE_FORMATS = {"gpqa": "gpqa_raw_json", "gsm8k": "gsm8k_parquet",
                  "humaneval": "humaneval_jsonl_gz", "livecodebench": "lcb_v6_jsonl",
                  "mmlu": "mmlu_unique_jsonl"}
PROBLEM_FIELDS = frozenset({
    "schema_version", "problem_id", "benchmark", "subset", "source_id", "source_revision",
    "source_record_sha256", "question", "choices", "public_examples", "entry_point",
    "starter_code", "io_type", "prompt_payload_sha256",
})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def closed(value: object, fields: set[str] | frozenset[str], name: str) -> dict:
    require(isinstance(value, dict) and set(value) == set(fields), f"{name} fields mismatch")
    return value


def validate_problem(row: dict) -> dict:
    closed(row, PROBLEM_FIELDS, "problem")
    require(row["schema_version"] == "pals_e3_problem_v1", "unknown problem schema")
    require(row["benchmark"] in BENCHMARKS, "unknown benchmark")
    require(isinstance(row["problem_id"], str) and
            row["problem_id"].startswith(row["benchmark"] + ":"), "invalid problem ID")
    for field in ("source_id", "source_revision", "source_record_sha256", "question"):
        require(isinstance(row[field], str) and bool(row[field].strip()), f"empty {field}")
    choices = row["choices"]
    require(choices is None or (isinstance(choices, list) and len(choices) >= 2 and
            all(isinstance(s, str) and s.strip() for s in choices)), "invalid choices")
    if row["benchmark"] in ("gpqa", "mmlu"):
        require(choices is not None, "MCQ requires choices")
    else:
        require(choices is None, "non-MCQ choices forbidden")
    require(isinstance(row["public_examples"], list), "invalid public examples")
    require(row["entry_point"] is None or
            (isinstance(row["entry_point"], str) and row["entry_point"].isidentifier()),
            "invalid entry point")
    require(row["io_type"] in (None, "functional", "stdin"), "invalid I/O type")
    require(isinstance(row["starter_code"], str), "invalid starter code")
    public = {k: row[k] for k in ("question", "choices", "public_examples", "entry_point",
                                  "starter_code", "io_type")}
    require(row["prompt_payload_sha256"] == digest(public), "prompt payload hash mismatch")
    return row


def make_problem(*, benchmark: str, problem_id: str, subset: str, source_id: str,
                 source_revision: str, source_record_sha256: str, question: str,
                 choices: list[str] | None = None, public_examples: list | None = None,
                 entry_point: str | None = None, starter_code: str = "",
                 io_type: str | None = None) -> dict:
    row = {"schema_version": "pals_e3_problem_v1", "problem_id": problem_id,
           "benchmark": benchmark, "subset": subset, "source_id": source_id,
           "source_revision": source_revision, "source_record_sha256": source_record_sha256,
           "question": question, "choices": choices, "public_examples": public_examples or [],
           "entry_point": entry_point, "starter_code": starter_code, "io_type": io_type}
    row["prompt_payload_sha256"] = digest({k: row[k] for k in
        ("question", "choices", "public_examples", "entry_point", "starter_code", "io_type")})
    return validate_problem(row)
