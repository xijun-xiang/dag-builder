"""Pinned public-source import. Selection never uses generated text or scores."""

import hashlib
import io
import re
from pathlib import Path
from urllib.request import urlopen

from .math_answers import extract_gsm8k_answer
from .schemas import Question, require, text
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

DATASET = "cais/mmlu"
GSM8K_DATASET = "openai/gsm8k"
VISUAL_REFERENCE = re.compile(
    r"\b(diagram|pictured|illustrated|accompanying figure|figure below|shown below)\b|<img|!\[",
    re.IGNORECASE,
)
GSM8K_CALCULATOR_MARKUP = re.compile(
    r"<<(?P<expression>[^<>\n]+)>>(?P<result>[-+]?[$]?\d[\d,./]*%?)"
)


def canonicalize_gsm8k_rationale(raw_answer):
    """Remove calculator markup while keeping the original GSM8K answer immutable.

    A GSM8K calculation is encoded as ``<<expression>>result``.  The result is
    already contained in the expression's right-hand side, so keeping both
    creates malformed text such as ``6*12=7272``.  Only an immediately adjacent
    numeric result is removed; the caller keeps ``raw_answer`` for provenance.
    """
    require(text(raw_answer) and "\n####" in raw_answer, "missing official rationale")
    rationale = raw_answer.rsplit("\n####", 1)[0].strip()
    return GSM8K_CALCULATOR_MARKUP.sub(
        lambda match: match["expression"], rationale
    )


def normalize(rows, revision, subset, split):
    items = []
    for index, row in enumerate(rows):
        Question(row["question"], tuple(row["choices"]))
        require(
            type(row["answer"]) is int and 0 <= row["answer"] < 4,
            "invalid source answer",
        )
        require(row.get("subject", subset) == subset, "source subset mismatch")
        identity = {
            "dataset": DATASET,
            "revision": revision,
            "subset": subset,
            "split": split,
            "row": index,
        }
        items.append(
            dict(
                identity,
                task_type="mmlu",
                item_id=digest(identity)[:20],
                question=row["question"],
                choices=row["choices"],
                gold_answer="ABCD"[row["answer"]],
                source_content_sha256=digest(row),
            )
        )
    return items


def normalize_gsm8k(rows, revision, config_name, split):
    """Normalize GSM8K while retaining the complete source rationale for review."""
    items = []
    for index, row in enumerate(rows):
        require(
            isinstance(row.get("question"), str) and row["question"].strip(),
            "empty GSM8K question",
        )
        raw_answer = row.get("answer")
        gold_answer = extract_gsm8k_answer(raw_answer)
        identity = {
            "dataset": GSM8K_DATASET,
            "revision": revision,
            "config": config_name,
            "split": split,
            "row": index,
        }
        items.append(
            dict(
                identity,
                task_type="gsm8k",
                item_id=digest(identity)[:20],
                question=row["question"],
                gold_answer=gold_answer,
                raw_answer=raw_answer,
                source_content_sha256=digest(row),
            )
        )
    return items


def select(items, count, seed):
    eligible, excluded, seen = [], [], set()
    for item in items:
        duplicate_key = digest(
            {
                "q": " ".join(item["question"].split()),
                "choices": item.get("choices"),
            }
        )
        reason = None
        if duplicate_key in seen:
            reason = "duplicate_question_and_choices"
        elif VISUAL_REFERENCE.search(item["question"]):
            reason = "possible_missing_visual_reference"
        seen.add(duplicate_key)
        if reason:
            excluded.append({"item_id": item["item_id"], "reason": reason})
        else:
            eligible.append(item)
    require(
        type(count) is int and count > 0 and len(eligible) >= count,
        "insufficient eligible source items",
    )
    ordered = sorted(
        eligible, key=lambda x: digest({"seed": seed, "item_id": x["item_id"]})
    )
    chosen = ordered[:count]
    return chosen, {
        "seed": seed,
        "candidate_count": len(items),
        "eligible_count": len(eligible),
        "selected_count": count,
        "selected_ids": [i["item_id"] for i in chosen],
        "excluded": excluded,
        "sampling": "ascending SHA256(seed,item_id), without replacement",
        "scope": "engineering pilot, not confirmatory; no score-based selection or replenishment",
    }


def prepare(
    root,
    revision,
    subset="high_school_physics",
    split="test",
    count=30,
    seed=20260909,
    dataset="mmlu",
    source_parquet=None,
):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be an immutable 40-character commit SHA")
    if dataset not in ("mmlu", "gsm8k"):
        raise ValueError("dataset must be mmlu or gsm8k")
    if not re.fullmatch(r"[a-z_]+", subset) or split not in (
        "dev",
        "validation",
        "test",
    ):
        raise ValueError("invalid subset or split")
    root = private_dir(root)
    source_dir = private_dir(root / "source")
    dataset_id = DATASET if dataset == "mmlu" else GSM8K_DATASET
    url = f"https://huggingface.co/datasets/{dataset_id}/resolve/{revision}/{subset}/{split}-00000-of-00001.parquet"
    source_path = None
    if source_parquet is not None:
        source_path = Path(source_parquet).expanduser().resolve()
        if not source_path.is_file() or source_path.suffix.lower() != ".parquet":
            raise ValueError("source_parquet must be an existing Parquet file")
    source_locator = str(source_path) if source_path is not None else url
    path = source_dir / "original.parquet"
    if path.exists():
        provenance = read_json(source_dir / "provenance.json")
        require(
            provenance.get("source", provenance.get("url")) == source_locator,
            "existing source belongs to another revision/subset",
        )
        payload = path.read_bytes()
        require(
            hashlib.sha256(payload).hexdigest() == provenance["sha256"],
            "source file hash mismatch",
        )
    else:
        if source_path is not None:
            payload = source_path.read_bytes()
        else:
            with urlopen(url, timeout=60) as response:
                payload = response.read(10_000_001)
        if len(payload) > 10_000_000:
            raise ValueError("unexpectedly large subset download")
        write_bytes_once(path, payload)
        write_once(
            source_dir / "provenance.json",
            {
                "source": source_locator,
                "url": None if source_path is not None else url,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "revision": revision,
                "dataset": dataset_id,
                "config": subset,
                "split": split,
            },
        )
    try:
        from pyarrow import parquet
    except ImportError:
        raise RuntimeError(
            "install the optional [source] dependency to import Parquet"
        ) from None
    rows = parquet.read_table(io.BytesIO(payload)).to_pylist()
    items = (
        normalize(rows, revision, subset, split)
        if dataset == "mmlu"
        else normalize_gsm8k(rows, revision, subset, split)
    )
    chosen, manifest = select(items, count, seed)
    write_once(source_dir / "normalized.json", items)
    write_once(root / "selection.json", manifest)
    write_once(root / "items.json", chosen)
    return manifest
