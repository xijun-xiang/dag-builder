"""Preserve a paused MMLU run and explicitly omit exhausted transport items.

Only execution flow changes: three prehistory items remain unfinished, while
the remaining subjects may be run in a new, independently auditable directory.
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import private_dir, read_json, run_lock, write_once


OMISSIONS = {
    "ae625427c20fc16ccd62": "review_dag",
    "d3e6291ba7fa6f175676": "atomize",
    "d3e998612e060c4a093d": "atomize",
}
SOURCE_COMMIT = "72d3cbda94ceb0c89cec38f86d6707207631fe3e"
MAX_CALLS = 66273
MAX_RESERVED = 3313650000


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(source, log):
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    manifest = read_json(source / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    if (len(subjects) != 27 or manifest["selected_count"] != 7888
            or subjects[20] != "prehistory"
            or [row.get("subject") for row in events[1:21]] != subjects[:20]
            or any(row.get("event") != "subject_complete" or row["result"]["paused"]
                   for row in events[1:21])
            or events[-1] != {"event": "paused", "subject": "prehistory"}
            or events[-2].get("event") != "subject_complete"
            or events[-2].get("subject") != "prehistory"
            or not events[-2]["result"]["paused"]):
        raise ValueError("source did not stop at expected prehistory boundary")
    config = Config.load(source / "continuation_config.json")
    if (config.task_type != "mmlu" or config.prompt_version != "mmlu-general-thinking-v4"
            or config.model != "deepseek-v4-flash" or config.workers != 6
            or not config.content_gated_response):
        raise ValueError("frozen scientific config changed")
    origin = read_json(source / "code/snapshot_origin.json")
    if origin["git_commit"] != SOURCE_COMMIT:
        raise ValueError("frozen code revision changed")
    for relative, digest in origin["source_files"].items():
        if sha256(source / "code/dag_builder" / relative) != digest:
            raise ValueError("frozen package file changed: " + relative)
    for subject in subjects[:20]:
        root = source / "subjects" / subject
        if (len(list(root.glob("items/*/result.json")))
                != manifest["subjects"][subject]["selected_count"]):
            raise ValueError("previously completed subject has missing results")
    for subject in subjects[21:]:
        if list((source / "subjects" / subject).glob("items/*/*/attempt-*/request.json")):
            raise ValueError("remaining subject has already started")
    root = source / "subjects/prehistory"
    selected_ids = set(read_json(root / "selection.json")["selected_ids"])
    terminal = {path.parent.name: read_json(path)
                for path in root.glob("items/*/result.json")}
    if (len(selected_ids) != 323 or len(terminal) != 320
            or selected_ids - set(terminal) != set(OMISSIONS)
            or any(row["status"] not in ("model_accepted", "needs_review", "rejected")
                   for row in terminal.values())):
        raise ValueError("prehistory denominator or terminal results changed")
    invocations = [read_json(path) for path in (root / "invocations").glob("*.json")]
    matched = [row for row in invocations if row.get("paused") and
               {entry["item_id"]: entry["stage"] for entry in row["results"]
                if entry["status"] == "paused"} == OMISSIONS]
    if len(matched) != 1 or any(entry.get("reason") != "transient_retries_exhausted"
                                for entry in matched[0]["results"]
                                if entry["status"] == "paused"):
        raise ValueError("three omissions lack exhausted-transport evidence")
    for item_id, stage in OMISSIONS.items():
        attempts = sorted((root / "items" / item_id / stage).glob("attempt-*/request.json"))
        if ([path.parent.name for path in attempts]
                != [f"attempt-{index:02d}" for index in range(4)]
                or any(not (path.parent / "error.json").is_file() for path in attempts)):
            raise ValueError("omission lacks four recorded transport attempts")
    requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses = errors = reserved = 0
    unknown = []
    for request in requests:
        amount = read_json(request)["reserved_tokens"]
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid request reservation")
        reserved += amount
        response = (request.parent / "response.json").is_file()
        error = (request.parent / "error.json").is_file()
        if response and error:
            raise ValueError("attempt has response and error")
        responses += response
        errors += error
        if not response and not error:
            unknown.append(str(request.relative_to(source)))
    if ((len(requests), responses, errors, len(unknown), reserved)
            != (33171, 33055, 78, 38, 1226502059)
            or len(requests) > MAX_CALLS or reserved > MAX_RESERVED):
        raise ValueError("historical API ledger changed")
    return {
        "source_run": str(source), "source_log_sha256": sha256(log),
        "source_code_commit": SOURCE_COMMIT,
        "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
        "frozen_config_sha256": sha256(source / "continuation_config.json"),
        "completed_subjects": subjects[:20], "omitted_subject": "prehistory",
        "omitted_item_stages": OMISSIONS, "prehistory_terminal": 320,
        "remaining_subjects": subjects[21:], "requests_imported": len(requests),
        "responses_imported": responses, "errors_imported": errors,
        "unknown_requests": unknown, "reserved_tokens_imported": reserved,
        "max_total_calls": MAX_CALLS, "max_total_reserved_tokens": MAX_RESERVED,
        "omission_reason": "transport_attempt_reservations_exhausted; N/A, not semantic rejection",
    }


def prepare(source, destination, log, runner):
    source, destination, log, runner = (Path(path).absolute()
                                        for path in (source, destination, log, runner))
    if (source.resolve() != source or destination.resolve() != destination
            or not source.is_dir() or not log.is_file() or not runner.is_file()
            or destination.exists() or source == destination or source in destination.parents):
        raise ValueError("use a real source and a separate new destination")
    with run_lock(source):
        proof = audit(source, log)
        private_dir(destination)
        digest = hashlib.sha256()
        copied = 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("source contains symlink")
            if (not path.is_file() or path.name in (".lock", "launchd-mmlu27.plist")
                    or "__pycache__" in path.parts):
                continue
            relative = path.relative_to(source)
            target = destination / relative
            private_dir(target.parent)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            item_hash = sha256(path)
            if sha256(target) != item_hash:
                raise ValueError("copy hash mismatch: " + str(relative))
            digest.update(str(relative).encode() + b"\0" + item_hash.encode() + b"\n")
            copied += 1
        target = destination / "code/run_mmlu_after_infra_omission.py"
        if target.exists():
            raise ValueError("runner path already exists in source snapshot")
        shutil.copyfile(runner, target)
        target.chmod(0o600)
        proof["runner_sha256"] = sha256(target)
        proof["copied_files"] = copied
        proof["copied_tree_manifest_sha256"] = digest.hexdigest()
        write_once(destination / "infra_omission_continuation_provenance.json", proof)
        return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    args = parser.parse_args()
    proof = prepare(args.source, args.destination, args.log, args.runner)
    print(json.dumps({key: proof[key] for key in
                      ("prehistory_terminal", "remaining_subjects", "requests_imported",
                       "reserved_tokens_imported", "copied_files")}, sort_keys=True))


if __name__ == "__main__":
    main()
