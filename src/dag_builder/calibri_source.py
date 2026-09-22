"""Pinned public CALIBRI files; no model calls and no candidate-code execution.

The upstream correctness flag is evidence from its authors, NOT our execution
acceptance. Keep raw Parquet intact; downstream readers can select columns.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from .schemas import require
from .storage import digest, private_dir, read_json, write_once

DATASET = "lavis-nlp/CALIBRI"
REVISION = "7a4a7dc7aceb1b4a426c48761d0f128f2daf6a54"
# Size and SHA-256 from the pinned Hugging Face tree's LFS metadata.
FILES = {
    "livecodebench_qwen3/train-00000-of-00001.parquet":
        (89382069, "3aee997a10af1eafa7dc1e2ee56bea3dfe97fa5cd4cb875146ac36f5fc6f8b07"),
    "livecodebench_qwen3/validation-00000-of-00001.parquet":
        (48501069, "324c10df81f8d49c7dcd23ace33a8b94b20d963a593308b8230de491e3f8ea4a"),
    "livecodebench_qwen3/test-00000-of-00001.parquet":
        (46135996, "787bd1bf73a343ce3bee3c89d472b61f4e2312294b3d9aa659742b5ad19920ff"),
    "livecodebench_gpt-oss/train-00000-of-00001.parquet":
        (137450644, "1d2104cce8d696a7849f5473b7c6a4b7f4bbfbf09a99952bf195ba5b0aa91333"),
    "livecodebench_gpt-oss/validation-00000-of-00001.parquet":
        (74175596, "9ffae47c020ae14c0b49d30126af876505b71ad320b671fd1dfb1bb1987c8824"),
    "livecodebench_gpt-oss/test-00000-of-00001.parquet":
        (70168940, "f085d8c6d26ce2e930a1186ef5d52352ed3d98d7ab76fe38a59a89328e9fedfa"),
}


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_file(path, size, expected_sha256):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "regular source file required")
    require(path.stat().st_size == size, "CALIBRI source size mismatch")
    require(file_sha256(path) == expected_sha256, "CALIBRI source SHA256 mismatch")


def fetch_file(cache, name, *, wall_seconds=180):
    """Fetch only allowlisted immutable files, capped by their exact byte size.

    Socket waits are capped at 30 s and the streaming wall budget is checked per
    chunk. A failed partial is removed; an existing verified file is never fetched
    again. No keys, cookies, model calls, or unpickling are involved.
    """
    require(name in FILES, "file is not in the frozen CALIBRI allowlist")
    require(type(wall_seconds) is int and 1 <= wall_seconds <= 600, "invalid download time budget")
    size, expected = FILES[name]
    cache = private_dir(cache)
    target = cache / name
    private_dir(target.parent)
    if target.exists() or target.is_symlink():
        verify_file(target, size, expected)
        return target
    fd, tmp = tempfile.mkstemp(prefix=".calibri-partial-", dir=target.parent)
    started = time.monotonic()
    try:
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{name}"
        with os.fdopen(fd, "wb") as output, urlopen(url, timeout=30) as response:
            total = 0
            while True:
                if time.monotonic() - started > wall_seconds:
                    raise TimeoutError("CALIBRI download wall budget exceeded")
                chunk = response.read(min(1024 * 1024, size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                require(total <= size, "CALIBRI response exceeded frozen size")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify_file(tmp, size, expected)
        try:
            os.link(tmp, target)
        except FileExistsError:
            verify_file(target, size, expected)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def read_rows(cache, name, columns):
    """Read only selected Parquet columns after file integrity verification."""
    require(name in FILES, "unknown CALIBRI file")
    path = Path(cache) / name
    verify_file(path, *FILES[name])
    try:
        import pyarrow.parquet as pq
    except ImportError as error:
        raise RuntimeError("CALIBRI import needs the existing optional source/pyarrow dependency") from error
    table = pq.ParquetFile(path).read(columns=columns, use_threads=False)
    return table.to_pylist()


def prose_without_fences(output):
    """Discovery filter only, never a reasoning-quality certificate."""
    if not isinstance(output, str):
        return ""
    # An unfinished fence is deliberately NOT repaired or guessed away.
    # GPT-OSS outputs may attach the opening fence to an assistant-channel
    # marker. Count inline delimiters too; do not mistake that for truncation.
    if len(re.findall(r"```", output)) % 2:
        return ""
    return re.sub(r"```[^\n]*\n[\s\S]*?```", "", output).strip()


def inspect_row(row, item):
    """Exact source match and cheap candidate checks, without running any code."""
    from .livecodebench_reference import validate_reference

    require(row.get("id") == item["question_id"], "CALIBRI question ID mismatch")
    require(row.get("name") == item["question_title"], "CALIBRI title mismatch")
    require(row.get("prompt") == item["question"], "CALIBRI question text mismatch")
    for key in ("program", "output", "is_correct"):
        require(isinstance(row.get(key), list) and len(row[key]) == 10,
                "CALIBRI requires ten aligned samples")
    require(all(type(x) is bool for x in row["is_correct"]), "non-boolean correctness label")
    samples = []
    for index, (code, output, flag) in enumerate(zip(row["program"], row["output"], row["is_correct"])):
        reasons = []
        if not flag:
            reasons.append("upstream_test_not_passed")
        if not isinstance(code, str) or not code.strip():
            reasons.append("missing_code")
        if not isinstance(output, str) or not output.strip():
            reasons.append("missing_output")
        prose = prose_without_fences(output)
        if len(prose) < 100:
            reasons.append("insufficient_noncode_text_for_pilot")
        if isinstance(code, str) and isinstance(output, str) and code.strip() not in output:
            reasons.append("program_not_verbatim_in_output")
        if isinstance(code, str) and code.strip():
            try:
                validate_reference({"language": "python3", "code": code,
                                    "rationale": "Source discovery only; not semantic approval."}, item)
            except (ValueError, TypeError) as error:
                reasons.append("invalid_code_contract:" + str(error))
        samples.append({"sample_index": index, "upstream_pass": flag,
                        "prose_chars": len(prose), "candidate": not reasons,
                        "reasons": reasons, "program_sha256": digest(code),
                        "output_sha256": digest(output)})
    return samples


def prepare(cache, source, output):
    """Audit both published models and keep the ORIGINAL five-question canary.

    Sampling is deterministic among passed, text-bearing candidates, never based
    on PALS or model confidence. Selection remains provisional until isolated
    execution plus semantic review. No hidden tests enter these output artifacts.
    """
    source, output = Path(source), private_dir(output)
    originals = read_json(source / "source/normalized.json")
    frozen = read_json(source / "items.json")
    manifest = read_json(source / "prepared-manifest.json")
    selection = read_json(source / "selection.json")
    require(digest(frozen) == manifest["items_sha256"]
            and digest(selection) == manifest["selection_sha256"], "original canary changed")
    require(len(originals) == 175 and len(frozen) == 5, "expected frozen 175/5 source")
    by_id = {i["question_id"]: i for i in originals}
    require(len(by_id) == len(originals), "ambiguous cross-platform question IDs")
    require(all(i == by_id.get(i["question_id"]) for i in frozen), "canary differs from full source")
    columns = ["id", "name", "prompt", "program", "output", "is_correct", "model", "difficulty"]
    candidates = {qid: [] for qid in by_id}
    found = {cfg: set() for cfg in ("livecodebench_qwen3", "livecodebench_gpt-oss")}
    passed = {cfg: set() for cfg in found}
    usable = {cfg: set() for cfg in found}
    seen = {cfg: set() for cfg in found}
    errors, index, row_counts = [], [], {cfg: 0 for cfg in found}
    canary_ids = {i["question_id"] for i in frozen}
    for name in FILES:
        cfg, filename = name.split("/")
        rows = read_rows(cache, name, columns)
        row_counts[cfg] += len(rows)
        for row_number, row in enumerate(rows):
            qid = row["id"]
            require(qid not in seen[cfg], "duplicate CALIBRI ID across splits")
            seen[cfg].add(qid)
            if qid not in by_id:
                continue
            found[cfg].add(qid)
            try:
                samples = inspect_row(row, by_id[qid])
            except ValueError as error:
                errors.append({"config": cfg, "question_id": qid, "reason": str(error)})
                continue
            if any(s["upstream_pass"] for s in samples):
                passed[cfg].add(qid)
            origin = {"dataset": DATASET, "revision": REVISION, "file": name,
                      "file_sha256": FILES[name][1], "row": row_number, "config": cfg,
                      "split": filename.split("-")[0], "model": row["model"],
                      "selected_columns_sha256": digest(row)}
            index.append({"question_id": qid, "origin": origin, "samples": samples})
            if qid in canary_ids:
                write_once(output / "source-rows" / cfg / (by_id[qid]["item_id"] + ".json"), row)
            for sample in samples:
                if not sample["candidate"]:
                    continue
                usable[cfg].add(qid)
                if qid in canary_ids:
                    n = sample["sample_index"]
                    candidates[qid].append({"origin": {**origin, "sample_index": n},
                                            "reference_code": row["program"][n],
                                            "raw_output": row["output"][n]})
    require(all(n == 1055 for n in row_counts.values()), "partial CALIBRI release")
    items, missing = [], []
    for item in frozen:
        options = candidates[item["question_id"]]
        if not options:
            missing.append({"item_id": item["item_id"], "question_id": item["question_id"],
                            "reason": "no_matched_test_flagged_text_bearing_candidate"})
            continue
        chosen = min(options, key=lambda c: digest({"seed": selection["seed"], "origin": c["origin"]}))
        # Explicit source allowlist: hidden tests and labels never become prompt text.
        items.append({**item, **chosen, "reference_origin": "calibri_model_output",
                      "reference_execution": "upstream_pass_only_not_locally_executed",
                      "formal_eligible": False})
    audit = {"protocol": "calibri-lcb-source-v2", "dataset": DATASET, "revision": REVISION,
             "target_questions": len(originals), "models": {
                 cfg: {"source_rows": row_counts[cfg], "matched_questions": len(found[cfg]),
                       "upstream_passed_questions": len(passed[cfg]),
                       "text_bearing_candidate_questions": len(usable[cfg]),
                       "missing_ids": sorted(set(by_id) - found[cfg])} for cfg in found},
             "union_upstream_passed_questions": len(set.union(*passed.values())),
             "union_text_bearing_candidate_questions": len(set.union(*usable.values())),
             "identity_or_schema_errors": errors, "original_canary_count": 5,
             "selected_canary_count": len(items), "excluded_canary": missing,
             "claim": "upstream labels and mechanical discovery, NOT local tests or semantic approval"}
    write_once(output / "candidate-index.json", index)
    write_once(output / "audit.json", audit)
    write_once(output / "items.json", items)
    write_once(output / "selection.json", {
        "seed": selection["seed"], "original_selected_ids": selection["selected_ids"],
        "selected_ids": [i["item_id"] for i in items], "excluded": missing,
        "sampling": "original five, then minimum SHA256(seed,origin) among mechanical candidates",
        "no_score_selection": True, "no_replacement": True})
    write_once(output / "calibri-manifest.json", {
        "protocol": audit["protocol"], "dataset_revision": REVISION,
        "source_files": {n: {"size": v[0], "sha256": v[1]} for n, v in FILES.items()},
        "source_prepared_manifest_sha256": digest(manifest),
        "importer_sha256": file_sha256(__file__),
        "full_cohort_sha256": digest(originals), "items_sha256": digest(items),
        "audit_sha256": digest(audit), "index_sha256": digest(index),
        "selection_sha256": digest(read_json(output / "selection.json")),
        "formal_eligible": False, "model_calls": 0})
    return audit


def main():
    import argparse
    from .storage import run_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="fetch only the six pinned public LCB files")
    args = parser.parse_args()
    os.umask(0o077)
    with run_lock(args.output):
        if args.download:
            for name in FILES:
                fetch_file(args.cache, name)
        print(json.dumps(prepare(args.cache, args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
