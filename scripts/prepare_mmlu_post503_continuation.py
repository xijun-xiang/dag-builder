"""Freeze the user-paused MMLU run in a fresh, auditable continuation.

This only copies evidence and execution code. Exhausted HTTP 503 stages remain
unresolved; their lifetime retry count is neither reset nor reclassified.
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


EXPECTED_LEDGER = (40960, 40148, 774, 38, 1539483894)
EXPECTED_LAW = {
    "model_accepted": 239,
    "needs_review": 551,
    "rejected": 266,
    "infrastructure_omitted": 2,
}
MAX_CALLS = 66273
MAX_TOKENS = 3313650000
OLD_COMMIT = "5e4259cec3e3b09dfcbe18c54553d8033449a3bd"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(source, log):
    manifest = read_json(source / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    previous = read_json(source / "infra_omission_continuation_provenance.json")
    origin = read_json(source / "code/snapshot_origin.json")
    config = Config.load(source / "continuation_config.json")
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if (manifest.get("subject_count") != 27 or manifest.get("selected_count") != 7888
            or subjects[20] != "prehistory" or subjects[22] != "professional_law"
            or subjects[23:] != ["public_relations", "security_studies",
                                  "us_foreign_policy", "world_religions"]
            or previous["continuation_code_commit"] != OLD_COMMIT
            or origin["git_commit"] != OLD_COMMIT
            or sha256(source / "campaign_manifest.json") != previous["frozen_selection_sha256"]
            or sha256(source / "continuation_config.json") != previous["frozen_config_sha256"]
            or config.model != "deepseek-v4-flash"
            or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.strict_response_contract
            or not config.content_gated_response
            or events[-1] != {"event": "paused", "subject": "professional_law"}
            or events[-2].get("event") != "subject_complete"
            or not events[-2]["result"]["paused"]
            or events[-2]["result"]["status_counts"] != {**EXPECTED_LAW, "paused": 476}):
        raise ValueError("source run, protocol, or paused boundary changed")
    for relative, expected in origin["source_files"].items():
        if sha256(source / "code/dag_builder" / relative) != expected:
            raise ValueError("frozen implementation changed: " + relative)
    if sha256(source / "code/run_mmlu_after_infra_omission.py") != previous["runner_sha256"]:
        raise ValueError("frozen runner changed")
    stop = source / "subjects/professional_law/operator-stop-request.json"
    if not stop.is_file():
        raise ValueError("user pause marker missing")
    for subject in subjects[:22]:
        expected = 320 if subject == "prehistory" else manifest["subjects"][subject]["selected_count"]
        if len(list((source / "subjects" / subject).glob("items/*/result.json"))) != expected:
            raise ValueError("previous subject denominator changed: " + subject)
    law = source / "subjects/professional_law"
    law_status = Counter(read_json(p)["status"] for p in law.glob("items/*/result.json"))
    if law_status != EXPECTED_LAW:
        raise ValueError("professional_law terminal results changed")
    for subject in subjects[23:]:
        root = source / "subjects" / subject
        if list(root.glob("items/*/result.json")) or list(root.glob("items/*/*/attempt-*/request.json")):
            raise ValueError("following subject was already started: " + subject)
    requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses = errors = reserved = 0
    unknown = []
    for request in requests:
        amount = read_json(request).get("reserved_tokens")
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid reservation")
        reserved += amount
        response = request.with_name("response.json").is_file()
        error = request.with_name("error.json").is_file()
        if response and error:
            raise ValueError("attempt has conflicting evidence")
        responses += response
        errors += error
        if not response and not error:
            unknown.append(str(request.relative_to(source)))
    if ((len(requests), responses, errors, len(unknown), reserved) != EXPECTED_LEDGER
            or set(unknown) != set(previous["unknown_requests"])
            or len(requests) > MAX_CALLS or reserved > MAX_TOKENS):
        raise ValueError("historical request ledger changed")
    exhausted = {}
    for last in law.glob("items/*/*/attempt-03/error.json"):
        evidence = [read_json(last.parent.parent / f"attempt-{n:02d}" / "error.json")
                    for n in range(4)]
        if all(row.get("category") == "uncertain_remote_state"
               and row.get("http_status") == 503 for row in evidence):
            item_id, stage = last.parents[2].name, last.parents[1].name
            if item_id in exhausted:
                raise ValueError("one item exhausted more than one stage")
            exhausted[item_id] = stage
    if len(exhausted) != 129 or any((law / "items" / item / "result.json").exists()
                                    for item in exhausted):
        raise ValueError("exhausted HTTP 503 cohort changed")
    return {
        "source_run": str(source), "source_log_sha256": sha256(log),
        "source_code_commit": OLD_COMMIT,
        "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
        "frozen_config_sha256": sha256(source / "continuation_config.json"),
        "stop_request_sha256": sha256(stop),
        "requests_imported": len(requests), "responses_imported": responses,
        "errors_imported": errors, "unknown_requests": unknown,
        "reserved_tokens_imported": reserved,
        "law_terminal_before_resume": dict(law_status),
        "law_exhausted_503": dict(sorted(exhausted.items())),
        "unstarted_subjects": subjects[23:],
        "max_total_calls": MAX_CALLS,
        "max_total_reserved_tokens": MAX_TOKENS,
        "retry_policy": "Four lifetime transport attempts; no reset, semantic retry, or silent N/A",
    }


def prepare(source, destination, log, runner):
    source, destination, log, runner = (Path(p).absolute()
                                        for p in (source, destination, log, runner))
    if (source.resolve() != source or destination.resolve() != destination
            or not source.is_dir() or not log.is_file() or not runner.is_file()
            or destination.exists() or source == destination or source in destination.parents):
        raise ValueError("use a real paused source and a fresh separate destination")
    with run_lock(source):
        proof = audit(source, log)
        private_dir(destination)
        digest = hashlib.sha256()
        copied = 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("source contains symlink")
            if (not path.is_file() or path.name in
                    (".lock", "operator-stop-request.json", "launchd-mmlu27.plist")
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
        target = destination / "code/run_mmlu_after_503.py"
        shutil.copyfile(runner, target)
        target.chmod(0o600)
        proof["runner_sha256"] = sha256(target)
        proof["copied_files"] = copied
        proof["copied_tree_manifest_sha256"] = digest.hexdigest()
        proof["exclusions"] = ["run lock", "operator stop marker", "old launchd configuration",
                               "Python bytecode cache"]
        write_once(destination / "post503_continuation_provenance.json", proof)
        return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "destination", "log", "runner"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    proof = prepare(args.source, args.destination, args.log, args.runner)
    print(json.dumps({key: proof[key] for key in
                      ("requests_imported", "responses_imported", "errors_imported",
                       "reserved_tokens_imported", "copied_files")}, sort_keys=True))


if __name__ == "__main__":
    main()
