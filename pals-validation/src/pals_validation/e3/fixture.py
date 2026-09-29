"""Tiny synthetic cohort for offline lifecycle checks; never scientific evidence."""

from pathlib import Path

from ..io import read, save, sha256
from .data import common_subset
from .schema import make_problem, require


def prepare_fixture(output: str | Path) -> dict:
    output = Path(output)
    require(not output.exists(), "fixture output exists")
    output.mkdir(parents=True, mode=0o700)
    (output / "grading").mkdir(mode=0o700)
    spec = [
        ("gpqa", "Synthetic question: which option is one?", ["one", "two", "three", "four"], "choice", "A"),
        ("gsm8k", "Synthetic question: what is 1 + 0?", None, "numeric", "1"),
        ("humaneval", "def solution():\n    \"\"\"Return one.\"\"\"\n", None, "humaneval_code", None),
        ("livecodebench", "Read one integer and print it.", None, "lcb_code", None),
        ("mmlu", "Synthetic question: which option is one?", ["one", "two", "three", "four"], "choice", "A"),
    ]
    problems, grading = [], {}
    for benchmark, question, choices, kind, answer in spec:
        problem = make_problem(benchmark=benchmark, problem_id=benchmark + ":synthetic-0",
            subset="synthetic", source_id=benchmark + ":synthetic-0", source_revision="synthetic-v1",
            source_record_sha256="synthetic-" + benchmark, question=question, choices=choices,
            io_type="functional" if benchmark == "humaneval" else
                    "stdin" if benchmark == "livecodebench" else None,
            entry_point="solution" if benchmark == "humaneval" else None)
        problems.append(problem)
        grading[problem["problem_id"]] = {"kind": kind, "value": answer}
    save(output / "problems.json", problems)
    save(output / "grading" / "answers.json", grading)
    save(output / "common_subset.json", common_subset(problems, 2026092903))
    files = {name: sha256(output / name) for name in
             ("problems.json", "grading/answers.json", "common_subset.json")}
    manifest = {"schema_version": "pals_e3_mock_prepared_v1",
                "scientific_evidence": False, "files": files,
                "counts": {p["benchmark"]: 1 for p in problems}}
    save(output / "manifest.json", manifest)
    return manifest
