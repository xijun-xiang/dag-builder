"""Pinned v6 import: preserve private tests as opaque data; NEVER unpickle/execute.

Only this first, fixed 175-problem tranche is supported. This module deliberately
does not label a generated solution as official gold or as execution-verified.
"""

import hashlib
import io
import json
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from .schemas import parse_object, require, text
from .storage import digest, private_dir, write_bytes_once, write_once

DATASET = "livecodebench/code_generation_lite"
RELEASE = "v6"
REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
SOURCE_SHA256 = "bb4c364f71921c4495a6ad15abe1a927350b720009f4933e2e71f8af0f6fd1f5"
MAX_SOURCE_BYTES = 256 * 1024**2
SEED = 20260922


def normalize_livecodebench(rows, revision):
    require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
            "immutable dataset revision required")
    items, tests, seen = [], {}, set()
    for index, row in enumerate(rows):
        require(isinstance(row, dict), "source row must be an object")
        require(all(text(row.get(k)) for k in (
            "question_id", "question_title", "question_content", "platform",
            "contest_date", "difficulty", "public_test_cases", "private_test_cases", "metadata")),
            "missing LiveCodeBench source field")
        platform, qid = row["platform"], row["question_id"]
        require(platform in ("leetcode", "atcoder", "codeforces"), "unknown platform")
        require((platform, qid) not in seen, "duplicate platform/question identity")
        seen.add((platform, qid))
        require(isinstance(row.get("starter_code"), str), "starter_code must be a string")
        require(row["difficulty"] in ("easy", "medium", "hard"), "unknown difficulty")
        # Source identity is the pinned test6 file, not a hand-picked date range.
        datetime.fromisoformat(row["contest_date"])
        metadata = parse_object(row["metadata"])
        function = metadata.get("func_name")
        require(function is None or (text(function) and function.isidentifier()),
                "invalid functional entry point")
        io_type = "functional" if function else "stdin"
        public = json.loads(row["public_test_cases"])
        require(isinstance(public, list) and bool(public), "public tests required")
        for case in public:
            require(isinstance(case, dict) and set(case) >= {"input", "output", "testtype"},
                    "invalid public test")
            require(isinstance(case["input"], str) and isinstance(case["output"], str),
                    "test I/O must be strings")
            require(case["testtype"] == io_type, "public test I/O type mismatch")
        identity = {"dataset": DATASET, "revision": revision, "release": RELEASE,
                    "platform": platform, "question_id": qid}
        item_id = digest(identity)[:20]
        test_bundle = {"item_id": item_id, "public_test_cases": row["public_test_cases"],
                       "private_test_cases": row["private_test_cases"], "metadata": row["metadata"]}
        tests[item_id] = test_bundle
        items.append({**identity, "item_id": item_id, "task_type": "livecodebench",
                      "subset": RELEASE, "split": "test", "row": index, "domain": "code",
                      "question": row["question_content"], "question_title": row["question_title"],
                      "contest_id": row.get("contest_id"), "contest_date": row["contest_date"],
                      "difficulty": row["difficulty"], "starter_code": row["starter_code"],
                      "io_type": io_type, "entry_point": function,
                      "public_examples": public, "source_content_sha256": digest(row),
                      "tests_sha256": digest(test_bundle), "reference_origin": "not_available",
                      "reference_execution": "not_executed"})
    require(bool(items), "empty LiveCodeBench source")
    return items, tests


def select_canary(items, count, seed):
    """Round-robin I/O × difficulty strata; all choices are score-blind hashes."""
    require(type(count) is int and 1 <= count <= len(items), "invalid count")
    require(type(seed) is int, "integer seed required")
    groups = defaultdict(list)
    for item in items:
        groups[(item["io_type"], item["difficulty"])].append(item)
    queues = {key: deque(sorted(group, key=lambda i: digest({"seed": seed, "item_id": i["item_id"]})))
              for key, group in sorted(groups.items())}
    # Alternate I/O types within each difficulty before beginning another round.
    keys = sorted(queues, key=lambda k: (("easy", "medium", "hard").index(k[1]), k[0]))
    chosen = []
    while len(chosen) < count:
        for key in keys:
            if queues[key]:
                chosen.append(queues[key].popleft())
                if len(chosen) == count:
                    break
    return chosen


def prepare_livecodebench(root, source_file, revision, expected_sha256, count=5, seed=SEED):
    require(revision == REVISION and expected_sha256 == SOURCE_SHA256,
            "v1 importer requires its pinned official v6 revision and SHA256")
    with Path(source_file).open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    require(len(raw) <= MAX_SOURCE_BYTES, "source exceeds bounded input limit")
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, "source SHA256 mismatch")
    rows = [parse_object(line) for line in io.StringIO(raw.decode("utf-8")) if line.strip()]
    items, tests = normalize_livecodebench(rows, revision)
    require(len(items) == 175, "v6 requires exactly 175 source problems")
    chosen = select_canary(items, count, seed)
    root = private_dir(root)
    selection = {"dataset": DATASET, "release": RELEASE, "revision": revision,
                 "candidate_count": len(items), "selected_count": count, "seed": seed,
                 "selected_ids": [i["item_id"] for i in chosen],
                 "selected_problem_ids": [{"platform": i["platform"], "question_id": i["question_id"]}
                                          for i in chosen],
                 "sampling": "round_robin_io_difficulty_then_SHA256(seed,item_id)",
                 "scope": "canary; no score-based replacement or formal PALS results"}
    write_bytes_once(root / "source" / "test6.jsonl", raw)
    write_once(root / "source" / "provenance.json", {
        "dataset": DATASET, "release": RELEASE, "revision": revision,
        "source_file": "test6.jsonl", "sha256": expected_sha256, "rows": len(items),
        "url": f"https://huggingface.co/datasets/{DATASET}/resolve/{revision}/test6.jsonl",
        "execution": "JSON only; private test payload not decoded or executed"})
    write_once(root / "source" / "normalized.json", items)
    write_once(root / "items.json", chosen)
    write_once(root / "selection.json", selection)
    for item in chosen:
        write_once(root / "tests" / (item["item_id"] + ".json"), tests[item["item_id"]])
    write_once(root / "prepared-manifest.json", {
        "protocol": "livecodebench-v6-source-v1", "source_sha256": expected_sha256,
        "items_sha256": digest(chosen), "selection_sha256": digest(selection),
        "tests": {i["item_id"]: i["tests_sha256"] for i in chosen}})
    return selection


def main():
    import argparse
    import os
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-file", type=Path, required=True)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.root):
        result = prepare_livecodebench(args.root, args.source_file, REVISION, SOURCE_SHA256, args.count)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
