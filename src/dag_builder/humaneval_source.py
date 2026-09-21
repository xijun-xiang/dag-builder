"""Offline HumanEval import. Parse Python syntax; NEVER import or execute it."""

import ast
import gzip
import hashlib
import io
import re
from pathlib import Path

from .schemas import parse_object, require, text
from .storage import digest, private_dir, write_bytes_once, write_once

MAX_BYTES = 20_000_000


def normalize_humaneval(rows, revision):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "immutable revision required")
    items, seen = [], set()
    for index, row in enumerate(rows):
        task_id = row.get("task_id")
        require(isinstance(task_id, str) and re.fullmatch(r"HumanEval/\d+", task_id), "invalid task ID")
        require(task_id not in seen, "duplicate task ID")
        seen.add(task_id)
        require(all(text(row.get(k)) for k in ("prompt", "canonical_solution", "entry_point", "test")),
                "missing HumanEval source field")
        require(row["entry_point"].isidentifier(), "invalid entry point")
        try:
            tree = ast.parse(row["prompt"] + row["canonical_solution"])
            test_tree = ast.parse(row["test"])
        except (SyntaxError, ValueError) as error:
            raise ValueError("invalid reference Python syntax") from error
        functions = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        require(functions.count(row["entry_point"]) == 1, "reference entry point mismatch")
        require(any(isinstance(n, ast.FunctionDef) and n.name == "check" for n in test_tree.body),
                "test has no check function")
        identity = {"dataset": "openai/human-eval", "revision": revision, "task_id": task_id}
        items.append({**identity, "item_id": digest(identity)[:20], "task_type": "humaneval",
                      "subset": "humaneval", "split": "test", "row": index, "domain": "code",
                      "question": row["prompt"], "entry_point": row["entry_point"],
                      "canonical_solution": row["canonical_solution"],
                      "source_content_sha256": digest(row),
                      "test_sha256": hashlib.sha256(row["test"].encode()).hexdigest(),
                      "reference_origin": "official_canonical_solution",
                      "reference_execution": "not_executed"})
    require(bool(items), "empty HumanEval source")
    return items


def prepare_humaneval(root, source_file, revision, expected_sha256, count=164, seed=20260921):
    """Require the full 164-task source even when selecting a small canary."""
    path = Path(source_file)
    require(path.suffix in (".jsonl", ".gz"), "expected JSONL or JSONL.gz")
    require(isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
            "explicit source SHA256 required")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "source too large")
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, "source hash mismatch")
    if path.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            decoded = stream.read(MAX_BYTES + 1)
    else:
        decoded = raw
    require(len(decoded) <= MAX_BYTES, "decompressed source too large")
    # StringIO follows actual JSONL newlines, not Unicode paragraph separators.
    rows = [parse_object(line) for line in io.StringIO(decoded.decode("utf-8")) if line.strip()]
    items = normalize_humaneval(rows, revision)
    require({i["task_id"] for i in items} == {f"HumanEval/{i}" for i in range(164)},
            "expected exactly the 164 original HumanEval IDs")
    require(type(count) is int and 1 <= count <= 164, "count must be 1..164")
    require(type(seed) is int, "integer seed required")
    chosen = sorted(items, key=lambda i: digest({"seed": seed, "item_id": i["item_id"]}))[:count]
    selection = {"seed": seed, "candidate_count": len(items), "selected_count": count,
                 "selected_ids": [i["item_id"] for i in chosen],
                 "selected_task_ids": [i["task_id"] for i in chosen], "excluded": [],
                 "sampling": "all tasks" if count == 164 else "ascending SHA256(seed,item_id)",
                 "scope": "fixed candidates before construction; no score-based replacement"}
    root = private_dir(root)
    source = private_dir(root / "source")
    write_bytes_once(source / ("original.jsonl.gz" if path.suffix == ".gz" else "original.jsonl"), raw)
    write_once(source / "provenance.json", {"dataset": "openai/human-eval", "revision": revision,
               "source": str(path.resolve()), "sha256": expected_sha256, "rows": len(items),
               "revision_verification": "caller-supplied revision and verified file hash; no remote identity claim",
               "execution": "syntax_only; no code or tests executed"})
    write_once(source / "normalized.json", items)
    write_once(root / "items.json", chosen)
    write_once(root / "selection.json", selection)
    return selection
