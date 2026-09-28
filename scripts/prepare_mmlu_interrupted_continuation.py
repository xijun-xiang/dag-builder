"""Copy an unexpectedly interrupted MMLU run into a new audited continuation.

The source remains immutable. Unknown remote attempts are retained and charged;
the frozen resilient runner may advance to a later attempt for those stages.
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import private_dir, read_json, run_lock, write_once


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def attempts(root):
    requests = sorted(root.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses = []
    errors = []
    unknown = []
    reserved = 0
    for request in requests:
        body = read_json(request)
        amount = body.get("reserved_tokens")
        if type(amount) is not int or amount <= 0:
            raise ValueError(f"invalid reservation: {request}")
        reserved += amount
        response = request.parent / "response.json"
        error = request.parent / "error.json"
        if response.is_file() and error.is_file():
            raise ValueError(f"response and error both present: {request}")
        if response.is_file():
            responses.append(response)
        elif error.is_file():
            errors.append(error)
        else:
            unknown.append(str(request.relative_to(root)))
    return requests, responses, errors, unknown, reserved


def prepare(source, destination, log, *, expected_commit, expected_counts,
            interrupted_subject, max_calls, max_reserved_tokens):
    source, destination, log = source.absolute(), destination.absolute(), log.absolute()
    if (source.resolve() != source or destination.resolve() != destination
            or log.resolve() != log or destination.exists()
            or source == destination or source in destination.parents):
        raise ValueError("source/log must be real paths and destination must be new")
    if not source.is_dir() or not log.is_file():
        raise ValueError("missing source or log")
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if (not events or events[0].get("event") != "start"
            or events[-1].get("event") != "subject_complete"
            or any(event.get("event") in ("complete", "paused") for event in events)):
        raise ValueError("source is not an interrupted campaign")
    completed_subjects = [row["subject"] for row in events[1:]]
    selection = read_json(source / "campaign_manifest.json")
    if (selection.get("subject_count") != 27 or selection.get("selected_count") != 7888
            or interrupted_subject not in selection["subjects"]
            or completed_subjects != list(selection["subjects"])[:len(completed_subjects)]
            or list(selection["subjects"])[len(completed_subjects)] != interrupted_subject):
        raise ValueError("log and frozen subject ordering disagree")
    config = Config.load(source / "continuation_config.json")
    if (config.task_type != "mmlu" or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.content_gated_response):
        raise ValueError("unexpected frozen scientific protocol")
    origin = read_json(source / "code/snapshot_origin.json")
    runtime = read_json(source / "runtime_continuation_provenance.json")
    if (origin["git_commit"] != expected_commit or runtime["runtime_workers"] != 32
            or runtime["frozen_config_sha256"] != sha256(source / "continuation_config.json")
            or runtime["frozen_selection_sha256"] != sha256(source / "campaign_manifest.json")):
        raise ValueError("frozen code or input provenance mismatch")
    with run_lock(source):
        requests, responses, errors, unknown, reserved = attempts(source)
        if ((len(requests), len(responses), len(errors), len(unknown)) != expected_counts
                or len(requests) > max_calls or reserved > max_reserved_tokens):
            raise ValueError("source attempt counts or hard budget changed")
        prior_unknown = set(runtime["unknown_requests"])
        new_unknown = sorted(set(unknown) - prior_unknown)
        if (not prior_unknown.issubset(unknown) or len(new_unknown) != 32
                or any(not path.startswith(f"subjects/{interrupted_subject}/")
                       for path in new_unknown)):
            raise ValueError("unexpected remote-unknown request set")
        if any(path.name == "operator-stop-request.json" for path in source.rglob("*")):
            raise ValueError("unexpected operator stop marker")
        private_dir(destination)
        copied = {}
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"source contains symlink: {path}")
            if not path.is_file() or path.name == ".lock":
                continue
            relative = path.relative_to(source)
            target = destination / relative
            private_dir(target.parent)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            digest = sha256(path)
            if sha256(target) != digest:
                raise ValueError(f"copy mismatch: {relative}")
            copied[str(relative)] = digest
        write_once(destination / "copied_file_manifest-interrupted.json", copied)
        proof = {
            "source_run": str(source),
            "source_log_sha256": sha256(log),
            "source_code_commit": expected_commit,
            "frozen_config_sha256": sha256(source / "continuation_config.json"),
            "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
            "interrupted_subject": interrupted_subject,
            "completed_subjects": completed_subjects,
            "requests_imported": len(requests),
            "responses_imported": len(responses),
            "errors_imported": len(errors),
            "reserved_tokens_imported": reserved,
            "previous_unknown_requests": sorted(prior_unknown),
            "new_unknown_requests": new_unknown,
            "unknown_requests": unknown,
            "retry_policy": "Frozen resilient runner may advance unknown stages to a later lifetime attempt; all old attempts stay charged; affected items are not clean first-call evidence.",
            "max_total_calls": max_calls,
            "max_total_reserved_tokens": max_reserved_tokens,
            "copied_files": len(copied),
            "copied_file_manifest_sha256": sha256(destination / "copied_file_manifest-interrupted.json"),
            "exclusions": ["run locks"],
        }
        write_once(destination / "interruption_continuation_provenance.json", proof)
        return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-requests", type=int, required=True)
    parser.add_argument("--expected-responses", type=int, required=True)
    parser.add_argument("--expected-errors", type=int, required=True)
    parser.add_argument("--expected-unknown", type=int, required=True)
    parser.add_argument("--interrupted-subject", required=True)
    parser.add_argument("--max-total-calls", type=int, required=True)
    parser.add_argument("--max-total-reserved-tokens", type=int, required=True)
    args = parser.parse_args()
    proof = prepare(
        args.source, args.destination, args.log,
        expected_commit=args.expected_commit,
        expected_counts=(args.expected_requests, args.expected_responses,
                         args.expected_errors, args.expected_unknown),
        interrupted_subject=args.interrupted_subject,
        max_calls=args.max_total_calls,
        max_reserved_tokens=args.max_total_reserved_tokens,
    )
    print(json.dumps({
        "completed_subjects": len(proof["completed_subjects"]),
        "requests_imported": proof["requests_imported"],
        "responses_imported": proof["responses_imported"],
        "errors_imported": proof["errors_imported"],
        "reserved_tokens_imported": proof["reserved_tokens_imported"],
        "new_unknown_requests": len(proof["new_unknown_requests"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
