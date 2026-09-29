"""Copy an operator-paused MMLU campaign into an audited fresh run.

The source is never changed. All API attempts, errors, results and frozen code
are copied byte-for-byte. Only the old operator stop marker, lock and launchd
configuration are excluded; the new run receives its own launch configuration.
"""

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import private_dir, read_json, run_lock, write_once


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(source, log, expected_counts, expected_subject):
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if (not events or events[0].get("event") != "start"
            or events[-1] != {"event": "paused", "subject": expected_subject}
            or events[-2].get("event") != "subject_complete"
            or events[-2].get("subject") != expected_subject
            or not events[-2]["result"].get("paused")):
        raise ValueError("source is not at the expected paused boundary")
    manifest = read_json(source / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    completed = [event["subject"] for event in events[1:-2]]
    if (manifest.get("subject_count") != 27 or manifest.get("selected_count") != 7888
            or completed != subjects[:len(completed)]
            or subjects[len(completed)] != expected_subject
            or len(completed) != 20):
        raise ValueError("frozen subject order or denominator changed")
    config = Config.load(source / "continuation_config.json")
    if (config.task_type != "mmlu" or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.content_gated_response):
        raise ValueError("frozen MMLU protocol changed")
    origin = read_json(source / "code/snapshot_origin.json")
    package = source / "code/dag_builder"
    for relative, expected_hash in origin["source_files"].items():
        if sha256(package / relative) != expected_hash:
            raise ValueError("frozen code snapshot changed")
    if origin["git_commit"] != "72d3cbda94ceb0c89cec38f86d6707207631fe3e":
        raise ValueError("unexpected frozen code revision")
    previous = read_json(source / "interruption_continuation_provenance.json")
    if (previous["source_code_commit"] != origin["git_commit"]
            or previous["frozen_config_sha256"] != sha256(source / "continuation_config.json")
            or previous["frozen_selection_sha256"] != sha256(source / "campaign_manifest.json")):
        raise ValueError("prior continuation provenance mismatch")
    stop = source / "subjects" / expected_subject / "operator-stop-request.json"
    if not stop.is_file():
        raise ValueError("missing operator stop marker")
    requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses, errors, unknown, reserved = [], [], [], 0
    for request in requests:
        amount = read_json(request).get("reserved_tokens")
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid API reservation")
        reserved += amount
        response, error = request.parent / "response.json", request.parent / "error.json"
        if response.is_file() and error.is_file():
            raise ValueError("attempt has both response and error")
        if response.is_file():
            responses.append(response)
        elif error.is_file():
            errors.append(error)
        else:
            unknown.append(str(request.relative_to(source)))
    if ((len(requests), len(responses), len(errors), len(unknown), reserved)
            != expected_counts or set(unknown) != set(previous["unknown_requests"])
            or len(unknown) != 38 or len(requests) > 66273 or reserved > 3313650000):
        raise ValueError("attempt ledger, unknown set or hard budget changed")
    prehistory = source / "subjects" / expected_subject
    selected = read_json(prehistory / "selection.json")["selected_count"]
    terminal = list(prehistory.glob("items/*/result.json"))
    if selected != 323 or len(terminal) != 247:
        raise ValueError("paused subject denominator changed")
    if any(read_json(path).get("status") not in
           ("model_accepted", "needs_review", "rejected") for path in terminal):
        raise ValueError("invalid terminal result in paused subject")
    for subject in completed:
        root = source / "subjects" / subject
        if len(list(root.glob("items/*/result.json"))) != manifest["subjects"][subject]["selected_count"]:
            raise ValueError("completed subject has missing results")
    kinds = Counter(read_json(path).get("transport_kind") or
                    read_json(path).get("category") for path in errors
                    if expected_subject in path.parts)
    if kinds != {"curl_exit_28": 47, "curl_exit_56": 9, "paused": 16}:
        raise ValueError("transport failure ledger changed")
    return {
        "source_run": str(source), "source_log_sha256": sha256(log),
        "source_code_commit": origin["git_commit"],
        "frozen_config_sha256": sha256(source / "continuation_config.json"),
        "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
        "completed_subjects": completed, "paused_subject": expected_subject,
        "stop_request_sha256": sha256(stop),
        "requests_imported": len(requests), "responses_imported": len(responses),
        "errors_imported": len(errors), "unknown_requests": unknown,
        "reserved_tokens_imported": reserved,
        "paused_subject_selected": selected,
        "paused_subject_terminal": len(terminal),
        "paused_subject_unfinished": selected - len(terminal),
        "transport_error_counts": dict(kinds),
        "max_total_calls": 66273,
        "max_total_reserved_tokens": 3313650000,
        "retry_policy": "Only the frozen lifetime-bounded transport retry policy; no semantic resampling. Prior attempts stay charged.",
    }


def prepare(source, destination, log, expected_counts, expected_subject):
    source, destination, log = source.absolute(), destination.absolute(), log.absolute()
    if (source.resolve() != source or destination.resolve() != destination
            or log.resolve() != log or not source.is_dir() or not log.is_file()
            or destination.exists() or source == destination or source in destination.parents):
        raise ValueError("use a real source and a new, separate destination")
    with run_lock(source):
        proof = audit(source, log, expected_counts, expected_subject)
        private_dir(destination)
        copied = {}
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("source contains symlink")
            if not path.is_file() or path.name in (".lock", "operator-stop-request.json", "launchd-mmlu27.plist") or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(source)
            target = destination / relative
            private_dir(target.parent)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            digest = sha256(path)
            if sha256(target) != digest:
                raise ValueError("copy hash mismatch: " + str(relative))
            copied[str(relative)] = digest
        write_once(destination / "copied_file_manifest-paused.json", copied)
        proof["copied_files"] = len(copied)
        proof["copied_file_manifest_sha256"] = sha256(destination / "copied_file_manifest-paused.json")
        proof["exclusions"] = ["run locks", "operator stop marker", "old launchd configuration", "Python bytecode cache"]
        write_once(destination / "paused_continuation_provenance.json", proof)
        return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--subject", default="prehistory")
    parser.add_argument("--expected-requests", type=int, required=True)
    parser.add_argument("--expected-responses", type=int, required=True)
    parser.add_argument("--expected-errors", type=int, required=True)
    parser.add_argument("--expected-unknown", type=int, required=True)
    parser.add_argument("--expected-reserved-tokens", type=int, required=True)
    args = parser.parse_args()
    counts = (args.expected_requests, args.expected_responses, args.expected_errors,
              args.expected_unknown, args.expected_reserved_tokens)
    proof = prepare(args.source, args.destination, args.log, counts, args.subject)
    print(json.dumps({key: proof[key] for key in
                      ("paused_subject", "requests_imported", "responses_imported",
                       "errors_imported", "reserved_tokens_imported", "copied_files")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
