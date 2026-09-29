"""Freeze original benchmark questions; keep all grading data in a private file."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path

from ..io import digest, read, read_jsonl, save, sha256, verify
from .schema import EXPECTED_COUNTS, SOURCE_FORMATS, make_problem, require, validate_problem

GPQA_REVISION = "56686c06f5e19865c153de0fdb11be3890014df7"
LCB_REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
GPQA_FIELDS = ("Question", "Explanation", "Correct Answer", "Incorrect Answer 1",
               "Incorrect Answer 2", "Incorrect Answer 3")
GPQA_CHOICE_SEED = 20260910


def _jsonl_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _load_source(spec: dict) -> list[dict]:
    path = Path(spec["path"]).resolve(strict=True)
    require(path.is_file() and not Path(spec["path"]).is_symlink(), "source must be a regular file")
    require(sha256(path) == spec["sha256"], "source SHA256 mismatch: " + spec["benchmark"])
    fmt = spec["format"]
    if fmt == "gsm8k_parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as error:
            raise RuntimeError("Install the optional E3 source dependency pyarrow") from error
        table = pq.read_table(path, columns=["question", "answer"])
        return table.to_pylist()
    if fmt == "humaneval_jsonl_gz":
        return _jsonl_gz(path)
    if fmt.endswith("_jsonl"):
        return read_jsonl(path)
    if fmt == "gpqa_raw_json":
        rows = read(path)
        require(isinstance(rows, list), "GPQA source must be a list")
        return rows
    raise ValueError("unknown source format")


def _gpqa(rows: list[dict], revision: str) -> tuple[list[dict], dict]:
    require(revision == GPQA_REVISION, "GPQA source revision mismatch")
    problems, grading, seen = [], {}, set()
    for index, row in enumerate(rows):
        record_id = row.get("Record ID")
        require(isinstance(record_id, str) and record_id and record_id not in seen,
                "missing/duplicate GPQA Record ID")
        seen.add(record_id)
        revised = [bool(row.get("Extra Revised " + field, "").strip()) for field in GPQA_FIELDS]
        require(not any(revised) or all(revised), "partial GPQA revision")
        prefix = "Extra Revised " if all(revised) else ""
        values = {field: row[prefix + field] for field in GPQA_FIELDS}
        require(all(isinstance(v, str) and v.strip() for v in values.values()), "empty GPQA field")
        labels = ["Correct Answer", "Incorrect Answer 1", "Incorrect Answer 2", "Incorrect Answer 3"]
        labels.sort(key=lambda field: digest({"seed": GPQA_CHOICE_SEED,
                                               "record_id": record_id, "field": field}))
        choices = [values[label] for label in labels]
        ambiguous = len(set(choices)) != 4
        identity = {"dataset": "Idavidrein/gpqa", "revision": revision,
                    "subset": "gpqa_diamond", "split": "train", "row": index,
                    "record_id": record_id}
        source_id = "Idavidrein/gpqa:gpqa_diamond:" + record_id
        problem = make_problem(benchmark="gpqa", problem_id="gpqa:" + digest(identity)[:20],
            subset="gpqa_diamond", source_id=source_id, source_revision=revision,
            source_record_sha256=digest(row), question=values["Question"], choices=choices)
        problems.append(problem)
        grading[problem["problem_id"]] = {"kind": "choice",
            "value": chr(65 + labels.index("Correct Answer")), "source_id": source_id,
            "outcome_eligible": not ambiguous,
            "ineligibility_reason": "duplicate_choice_text" if ambiguous else None}
    return problems, grading


def _gsm8k(rows: list[dict], revision: str) -> tuple[list[dict], dict]:
    problems, grading = [], {}
    for index, row in enumerate(rows):
        require(isinstance(row, dict) and isinstance(row.get("question"), str) and
                isinstance(row.get("answer"), str), "invalid GSM8K source row")
        require("####" in row["answer"], "GSM8K gold marker missing")
        gold = row["answer"].rsplit("####", 1)[1].strip()
        require(bool(gold), "empty GSM8K gold answer")
        problem = make_problem(benchmark="gsm8k", problem_id=f"gsm8k:test:{index}",
            subset="test", source_id=f"openai/gsm8k:main:test:{index}",
            source_revision=revision, source_record_sha256=digest(row), question=row["question"])
        problems.append(problem)
        grading[problem["problem_id"]] = {"kind": "numeric", "value": gold}
    return problems, grading


def _humaneval(rows: list[dict], revision: str) -> tuple[list[dict], dict]:
    problems, grading, seen = [], {}, set()
    for row in rows:
        task_id = row.get("task_id")
        require(isinstance(task_id, str) and task_id.startswith("HumanEval/") and
                task_id not in seen, "invalid/duplicate HumanEval task ID")
        seen.add(task_id)
        require(all(isinstance(row.get(k), str) and row[k].strip()
                    for k in ("prompt", "canonical_solution", "entry_point", "test")),
                "incomplete HumanEval row")
        problem = make_problem(benchmark="humaneval", problem_id="humaneval:" + task_id,
            subset="test", source_id=task_id, source_revision=revision,
            source_record_sha256=digest(row), question=row["prompt"],
            entry_point=row["entry_point"], io_type="functional")
        problems.append(problem)
        grading[problem["problem_id"]] = {"kind": "humaneval_code", "test": row["test"],
            "entry_point": row["entry_point"], "canonical_solution_sha256":
            hashlib.sha256(row["canonical_solution"].encode()).hexdigest()}
    require(seen == {f"HumanEval/{i}" for i in range(164)}, "HumanEval ID set mismatch")
    return problems, grading


def _livecodebench(rows: list[dict], revision: str) -> tuple[list[dict], dict]:
    require(revision == LCB_REVISION, "LCB v6 source revision mismatch")
    problems, grading, seen = [], {}, set()
    for row in rows:
        for field in ("platform", "question_id", "question_content", "public_test_cases",
                      "private_test_cases", "metadata", "starter_code"):
            require(isinstance(row.get(field), str), "missing LCB field: " + field)
        source_id = row["platform"] + ":" + row["question_id"]
        require(source_id not in seen, "duplicate LCB problem")
        seen.add(source_id)
        metadata = json.loads(row["metadata"])
        entry = metadata.get("func_name")
        require(entry is None or (isinstance(entry, str) and entry.isidentifier()),
                "invalid LCB entry point")
        examples = json.loads(row["public_test_cases"])
        require(isinstance(examples, list) and bool(examples), "LCB public examples missing")
        io_type = "functional" if entry else "stdin"
        require(all(isinstance(case, dict) and set(case) >= {"input", "output", "testtype"}
                    and case["testtype"] == io_type for case in examples), "LCB I/O mismatch")
        identity = {"dataset": "livecodebench/code_generation_lite", "revision": revision,
                    "release": "v6", "platform": row["platform"],
                    "question_id": row["question_id"]}
        problem = make_problem(benchmark="livecodebench",
            problem_id="livecodebench:" + digest(identity)[:20], subset="v6",
            source_id=source_id, source_revision=revision,
            source_record_sha256=digest(row), question=row["question_content"],
            public_examples=examples, entry_point=entry, starter_code=row["starter_code"],
            io_type=io_type)
        problems.append(problem)
        grading[problem["problem_id"]] = {"kind": "lcb_code", "io_type": io_type,
            "entry_point": entry, "public_test_cases": row["public_test_cases"],
            "private_test_cases": row["private_test_cases"], "metadata": row["metadata"]}
    return problems, grading


def _mmlu(rows: list[dict], revision: str) -> tuple[list[dict], dict]:
    problems, grading, seen = [], {}, set()
    for row in rows:
        require(row.get("schema_version") == "pals_dag_unified_v1" and
                row.get("benchmark") == "mmlu", "unexpected MMLU delivery")
        source_id = row["provenance"]["source_id"]
        require(source_id not in seen and row["provenance"]["revision"] == revision,
                "duplicate MMLU source or revision mismatch")
        seen.add(source_id)
        body = row["problem"]
        problem = make_problem(benchmark="mmlu", problem_id="mmlu:" + row["item_id"],
            subset=row["provenance"]["subset"], source_id=source_id,
            source_revision=revision, source_record_sha256=row["provenance"]["source_record_sha256"],
            question=body["question"], choices=body["choices"])
        problems.append(problem)
        require(row["answer"]["kind"] == "choice", "MMLU answer kind mismatch")
        grading[problem["problem_id"]] = {"kind": "choice", "value": row["answer"]["value"]}
    return problems, grading


ADAPTERS = {"gpqa": _gpqa, "gsm8k": _gsm8k, "humaneval": _humaneval,
            "livecodebench": _livecodebench, "mmlu": _mmlu}


def common_subset(problems: list[dict], seed: int, fraction: float = .1) -> list[str]:
    require(type(seed) is int and fraction == .1, "fixed common-scorer selection required")
    by_benchmark = {name: [] for name in EXPECTED_COUNTS}
    for problem in problems:
        by_benchmark[problem["benchmark"]].append(problem["problem_id"])
    chosen = []
    for benchmark, ids in sorted(by_benchmark.items()):
        ordered = sorted(ids, key=lambda item: (
            hashlib.sha256(f"e3-common-v1|{seed}|{benchmark}|{item}".encode()).hexdigest(), item))
        chosen.extend(ordered[:math.ceil(len(ids) * fraction)])
    return chosen


def prepare_sources(source_manifest: str | Path, output: str | Path) -> dict:
    manifest_path, output = Path(source_manifest), Path(output)
    manifest = read(manifest_path)
    require(isinstance(manifest, dict) and set(manifest) == {"schema_version", "sources", "seed"}
            and manifest["schema_version"] == "pals_e3_sources_v1", "invalid source manifest")
    require(type(manifest["seed"]) is int, "integer selection seed required")
    sources = manifest["sources"]
    require(isinstance(sources, list) and {s.get("benchmark") for s in sources} == set(EXPECTED_COUNTS)
            and len(sources) == len(EXPECTED_COUNTS), "exactly five benchmark sources required")
    problems, grading, inventory = [], {}, {}
    for source in sources:
        require(set(source) == {"benchmark", "path", "sha256", "revision", "format", "expected_count"},
                "source fields mismatch")
        benchmark = source["benchmark"]
        require(source["format"] == SOURCE_FORMATS[benchmark] and
                source["expected_count"] == EXPECTED_COUNTS[benchmark],
                "source protocol/count mismatch: " + benchmark)
        rows = _load_source(source)
        require(len(rows) == source["expected_count"], "actual source count mismatch: " + benchmark)
        prepared, answers = ADAPTERS[benchmark](rows, source["revision"])
        require(len(prepared) == len(rows), "adapter changed source count: " + benchmark)
        ids = {item["problem_id"] for item in prepared}
        require(len(ids) == len(prepared) and set(answers) == ids,
                "duplicate IDs or incomplete grading: " + benchmark)
        problems.extend(prepared)
        grading.update(answers)
        inventory[benchmark] = {"count": len(rows), "sha256": source["sha256"],
                                "revision": source["revision"], "path": str(Path(source["path"]).resolve())}
    require(len(problems) == sum(EXPECTED_COUNTS.values()) and
            len({p["problem_id"] for p in problems}) == len(problems), "global source identity mismatch")
    require(not output.exists(), "prepared output already exists")
    output.mkdir(parents=True, mode=0o700)
    (output / "grading").mkdir(mode=0o700)
    ordered = sorted(problems, key=lambda p: (p["benchmark"], p["problem_id"]))
    save(output / "problems.json", ordered)
    save(output / "grading" / "answers.json", grading)
    save(output / "common_subset.json", common_subset(ordered, manifest["seed"]))
    files = {"problems.json": sha256(output / "problems.json"),
             "grading/answers.json": sha256(output / "grading" / "answers.json"),
             "common_subset.json": sha256(output / "common_subset.json")}
    result = {"schema_version": "pals_e3_prepared_v1", "source_manifest_sha256": sha256(manifest_path),
              "sources": inventory, "counts": {name: inventory[name]["count"] for name in EXPECTED_COUNTS},
              "common_questions": len(read(output / "common_subset.json")), "files": files}
    save(output / "manifest.json", result)
    return result


def validate_prepared(folder: str | Path) -> tuple[dict, list[dict]]:
    folder = Path(folder)
    manifest = read(folder / "manifest.json")
    require(manifest["schema_version"] in ("pals_e3_prepared_v1", "pals_e3_mock_prepared_v1"),
            "prepared schema mismatch")
    verify(folder, manifest["files"])
    problems = read(folder / "problems.json")
    if manifest["schema_version"] == "pals_e3_prepared_v1":
        require(len(problems) == sum(EXPECTED_COUNTS.values()), "prepared count mismatch")
    else:
        require(manifest.get("scientific_evidence") is False, "mock prepared must be non-scientific")
    for problem in problems:
        validate_problem(problem)
    return manifest, problems
