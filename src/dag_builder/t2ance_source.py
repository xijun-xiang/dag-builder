"""Pinned, read-only discovery of tested LiveCodeBench programs from t2ance.

An upstream ``passed`` flag selects a candidate, not a reference solution or a
valid DAG. The original v6 question and independent CPU execution remain gates.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from .calibri_source import file_sha256
from .livecodebench_reference import validate_reference
from .livecodebench_source import REVISION as LCB_REVISION
from .livecodebench_source import SOURCE_SHA256, normalize_livecodebench
from .schemas import require
from .storage import digest, private_dir, read_json, write_once

DATASET = "t2ance/code-solutions"
REVISION = "eff8865b42b8badf2d8f7912b0ab9cfe5df3dc1a"
SEED = 20260923
# Sizes and SHA-256 are the pinned Hugging Face tree's LFS metadata.
FILES = {
    "data/lcb/deepseek/chunk_00000.parquet": (2353850, "a407a90e62b177ab7c8153b69785235d5b5ebe7700334ccb1e4718a1873c2e75"),
    "data/lcb/deepseek/chunk_00001.parquet": (2467008, "f51d63bc027f042c2ec62ec52a90ea733ae5dff1a6966f2d4f161573695fcc1e"),
    "data/lcb/deepseek/chunk_00002.parquet": (257169, "9d3b60b1a4d4a35a5677592df5eeae498efb5d4be3532b172223df3df92bceae"),
    "data/lcb/o4_mini/chunk_00000.parquet": (4797146, "43dd7b809d1364cd257d875ea6ae215ade1b6bada8d8b5734ed87d05980fefb8"),
    "data/lcb/qwen2_5_32b/chunk_00000.parquet": (2526643, "89a16564f5c22e75a192282d68fed4f11093d1628c5fab0d85bf7d6cf5dd1aa9"),
    "data/lcb/qwen2_5_32b/chunk_00001.parquet": (2978552, "1efc1aeebdbf1cdbf7944fb27769577a1fafdb50b629e38b50e48ec9b7e8a15d"),
    "data/lcb/qwen2_5_32b/chunk_00002.parquet": (3392343, "b0413e839c8dc4fceb91a155d07947ecf00c4691efd813f63abc9a4d39d49421"),
    "data/lcb/qwen2_5_32b/chunk_00003.parquet": (3634351, "368d2299867470c83a400fdb95673376e30d57a5be8d44bb9829d73790c827f9"),
    "data/lcb/qwen2_5_32b/chunk_00004.parquet": (981900, "6861f59a03320029782e00927a6e189f73e5673337069c058cac43cf6e73616e"),
    "data/lcb/qwen3_30b/chunk_00000.parquet": (5498919, "b76f0e8cdf4ba248ecbf47a81910fe2a65ae8e7c8fd9cecd208e79fe00f4125f"),
    "data/lcb/qwen3_30b/chunk_00001.parquet": (8670462, "e1e429e93c65edb7c90038bfbd423c62c3f45dc556981bfae700b41d7c9685b9"),
    "data/lcb/qwen3_30b/chunk_00002.parquet": (6330607, "6961295a8b9e76af0c87cd09d313538b37eae835cac3cede6737806e77b05965"),
    "data/lcb/qwen3_30b/chunk_00003.parquet": (9862138, "24426f0e551af2ad05c0627ce8fd15f8e8a7723b553767fc608b758f5afe9e8d"),
    "data/lcb/qwen3_30b/chunk_00004.parquet": (5173260, "6938003a406dd1de73cd53ba518dc72b265c6d659e8cb103d7058cd738abf74f"),
}


def verify_file(path, name):
    require(name in FILES, "unknown t2ance source file")
    path = Path(path)
    size, expected = FILES[name]
    require(path.is_file() and not path.is_symlink() and path.stat().st_size == size,
            "t2ance file type/size changed")
    require(file_sha256(path) == expected, "t2ance source SHA-256 changed")


def fetch_file(cache, name, *, wall_seconds=180):
    """Download only a pinned public Parquet, with no API key or test execution."""
    require(name in FILES and 1 <= wall_seconds <= 600, "unapproved source download")
    cache = private_dir(cache)
    target = cache / name
    private_dir(target.parent)
    if target.exists() or target.is_symlink():
        verify_file(target, name)
        return target
    size, _ = FILES[name]
    fd, temporary = tempfile.mkstemp(prefix=".t2ance-partial-", dir=target.parent)
    started = time.monotonic()
    try:
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{name}"
        with os.fdopen(fd, "wb") as output, urlopen(url, timeout=30) as response:
            total = 0
            while True:
                if time.monotonic() - started > wall_seconds:
                    raise TimeoutError("t2ance download wall budget exceeded")
                chunk = response.read(min(1024 * 1024, size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                require(total <= size, "t2ance response larger than pinned file")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify_file(temporary, name)
        try:
            os.link(temporary, target)
        except FileExistsError:
            verify_file(target, name)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def explanation_without_code(response, code):
    """Cheap text-bearing filter; never a semantic quality certificate."""
    if not isinstance(response, str) or not isinstance(code, str) or not code.strip():
        return ""
    if response.count("```") % 2:
        return ""
    remainder = response.replace(code, "")
    remainder = re.sub(r"```[^\n]*\n[\s\S]*?```", "", remainder)
    return remainder.strip()


def inspect_candidate(row, original, static_check):
    """Return all mechanical rejection reasons, preserving unfavorable rows."""
    problem = row.get("problem")
    require(isinstance(problem, dict), "missing upstream problem identity")
    require(row.get("task_id") == original["question_id"], "upstream task ID mismatch")
    for upstream, local in (("question_title", "question_title"),
                            ("question_content", "question"), ("platform", "platform"),
                            ("contest_date", "contest_date"), ("difficulty", "difficulty"),
                            ("starter_code", "starter_code")):
        require(problem.get(upstream) == original[local],
                f"upstream {upstream} differs from pinned v6")
    reasons = []
    passed = row.get("passed") is True
    tests = row.get("num_tests")
    if not passed or type(tests) is not int or tests <= 0 or row.get("num_passed") != tests:
        reasons.append("upstream_tests_not_all_passed")
    code, response = row.get("solution_code"), row.get("full_response")
    if not isinstance(code, str) or not code.strip():
        reasons.append("missing_program")
    if not isinstance(response, str) or not response.strip():
        reasons.append("missing_full_response")
    if isinstance(code, str) and isinstance(response, str) and code.strip() not in response:
        reasons.append("program_not_verbatim_in_response")
    prose = explanation_without_code(response, code)
    if len(prose) < 100:
        reasons.append("insufficient_noncode_text")
    if isinstance(code, str) and code.strip():
        try:
            validate_reference({"language": "python3", "code": code,
                                "rationale": "Source discovery, not semantic approval."}, original)
            static_check(code)
        except (ValueError, TypeError, SyntaxError, RecursionError) as error:
            reasons.append("invalid_or_unsupported_program:" + str(error))
    return {"candidate": not reasons, "upstream_pass": passed,
            "upstream_num_tests": tests, "prose_chars": len(prose),
            "reasons": reasons, "program_sha256": digest(code),
            "response_sha256": digest(response)}


def prepare(cache, source_file, calibri_selection, output, static_check):
    """Select one score-blind, text-bearing candidate per CALIBRI-missing task."""
    require(file_sha256(source_file) == SOURCE_SHA256, "pinned v6 source changed")
    with Path(source_file).open(encoding="utf-8") as stream:
        originals, _ = normalize_livecodebench([json.loads(s) for s in stream if s.strip()], LCB_REVISION)
    require(len(originals) == 175, "original v6 denominator changed")
    selection = read_json(calibri_selection)
    missing = selection["excluded"]
    require(len(missing) == 82 and all(x["reason"] ==
            "no_matched_test_flagged_text_bearing_candidate" for x in missing),
            "unexpected CALIBRI missing-source cohort")
    by_id = {i["question_id"]: i for i in originals}
    target = {x["question_id"] for x in missing}
    require(len(target) == 82 and all(by_id[x["question_id"]]["item_id"] == x["item_id"]
                                      for x in missing), "missing cohort identity changed")
    try:
        import pyarrow.parquet as pq
    except ImportError as error:
        raise RuntimeError("t2ance source import requires pyarrow") from error
    candidates = {qid: [] for qid in target}
    index, identity_errors, counts, seen = [], [], {}, set()
    columns = ["task_id", "solution_idx", "solution_code", "full_response", "passed",
               "num_passed", "num_tests", "problem"]
    for name in FILES:
        path = Path(cache) / name
        verify_file(path, name)
        table = pq.ParquetFile(path).read(columns=columns, use_threads=False)
        counts[name] = table.num_rows
        for row_number, row in enumerate(table.to_pylist()):
            qid = row.get("task_id")
            if qid not in target:
                continue
            split = name.split("/")[2]
            origin = {"dataset": DATASET, "revision": REVISION, "file": name,
                      "file_sha256": FILES[name][1], "row": row_number,
                      "split": split, "solution_idx": row.get("solution_idx"),
                      "selected_columns_sha256": digest(row)}
            identity = (split, qid, row.get("solution_idx"))
            require(identity not in seen, "duplicate upstream solution identity")
            seen.add(identity)
            try:
                assessment = inspect_candidate(row, by_id[qid], static_check)
            except ValueError as error:
                identity_errors.append({"question_id": qid, "origin": origin, "reason": str(error)})
                continue
            index.append({"question_id": qid, "origin": origin, **assessment})
            if assessment["candidate"]:
                candidates[qid].append({"origin": origin, "reference_code": row["solution_code"],
                                        "raw_output": row["full_response"], "raw_row": row})
    require(not identity_errors, "upstream question identity mismatch; quarantine source before selection")
    require(len(index) > 0, "no matching upstream solutions")
    items, excluded = [], []
    output = private_dir(output)
    for original in originals:
        qid = original["question_id"]
        if qid not in target:
            continue
        options = candidates[qid]
        if not options:
            excluded.append({"item_id": original["item_id"], "question_id": qid,
                             "reason": "no_test_passed_text_bearing_safe_candidate"})
            continue
        chosen = min(options, key=lambda c: digest({"seed": SEED, "origin": c["origin"]}))
        write_once(output / "source-rows" / (original["item_id"] + ".json"), chosen["raw_row"])
        items.append({**original, "origin": chosen["origin"],
                      "reference_code": chosen["reference_code"], "raw_output": chosen["raw_output"],
                      "reference_origin": "t2ance_model_output",
                      "reference_execution": "upstream_pass_only_not_locally_executed",
                      "formal_eligible": False})
    require(len(items) + len(excluded) == 82, "incomplete missing-source flow")
    audit = {"protocol": "t2ance-lcb-source-v1", "dataset": DATASET, "revision": REVISION,
             "original_v6_questions": 175, "calibri_missing_questions": 82,
             "matched_upstream_rows": len(index), "upstream_file_rows": counts,
             "text_bearing_candidates": len(items), "excluded": excluded,
             "identity_errors": identity_errors,
             "claim": "upstream tests and mechanical text/code checks only; not independent execution or DAG approval"}
    selected = {"seed": SEED, "selected_ids": [i["item_id"] for i in items],
                "excluded": excluded, "no_score_selection": True, "no_replacement": True,
                "sampling": "minimum SHA256(seed,source location) among passed text-bearing safe candidates"}
    write_once(output / "candidate-index.json", index)
    write_once(output / "audit.json", audit)
    write_once(output / "items.json", items)
    write_once(output / "selection.json", selected)
    write_once(output / "t2ance-manifest.json", {
        "protocol": audit["protocol"], "dataset_revision": REVISION,
        "source_files": {n: {"size": s, "sha256": h} for n, (s, h) in FILES.items()},
        "original_v6_sha256": SOURCE_SHA256,
        "calibri_selection_sha256": file_sha256(calibri_selection),
        "source_importer_sha256": file_sha256(__file__),
        "full_cohort_sha256": digest(originals), "items_sha256": digest(items),
        "index_sha256": digest(index), "audit_sha256": digest(audit),
        "selection_sha256": digest(selected), "formal_eligible": False, "model_calls": 0})
    return audit
