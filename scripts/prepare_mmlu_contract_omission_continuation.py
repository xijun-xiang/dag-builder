"""Copy a stopped MMLU run and authorize exactly one provider-overrun omission.

The source is never repaired in place. All raw attempts, including queued
pauses and the anomalous response, remain in the copied audit ledger.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from dag_builder.config import Config
from dag_builder.operator_omission import OMISSION, load_operator_omission
from dag_builder.storage import private_dir, read_json, write_once


MAX_CALLS = 66273
MAX_RESERVED = 3313650000
EXPECTED_COUNTS = (39515, 39368, 109, 38, 1481520701)
LAW_COUNTS = {"model_accepted": 201, "needs_review": 474,
              "rejected": 227, "paused": 632}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(package):
    return {str(path.relative_to(package)): sha256(path)
            for path in sorted(package.rglob("*")) if path.suffix in (".py", ".md")}


def copy_private(source, destination):
    private_dir(destination.parent)
    if source.is_symlink() or not source.is_file() or destination.exists():
        raise ValueError("unsafe or duplicate continuation file")
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
    if sha256(source) != sha256(destination):
        raise ValueError("continuation copy mismatch")


def audit(source, log, repository, expected_commit):
    manifest = read_json(source / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    previous = read_json(source / "infra_omission_continuation_provenance.json")
    old_origin = read_json(source / "code/snapshot_origin.json")
    old_files = source_files(source / "code/dag_builder")
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"],
                                     text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"],
                                    text=True).strip()
    config = Config.load(source / "continuation_config.json")
    if (len(subjects) != 27 or manifest["selected_count"] != 7888
            or previous["remaining_subjects"] != subjects[22:]
            or previous["omitted_subject"] != "prehistory"
            or previous["prehistory_terminal"] != 320
            or previous["max_total_calls"] != MAX_CALLS
            or previous["max_total_reserved_tokens"] != MAX_RESERVED
            or previous["frozen_selection_sha256"] != sha256(source / "campaign_manifest.json")
            or previous["frozen_config_sha256"] != sha256(source / "continuation_config.json")
            or config.task_type != "mmlu" or config.model != "deepseek-v4-flash"
            or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.content_gated_response
            or old_origin["git_commit"] != previous["continuation_code_commit"]
            or old_origin["source_files"] != old_files
            or commit != expected_commit or dirty):
        raise ValueError("source, scientific protocol, or committed code changed")
    if (events[-1] != {"event": "paused", "subject": "professional_law"}
            or events[-2].get("event") != "subject_complete"
            or events[-2].get("subject") != "professional_law"
            or events[-2]["result"]["status_counts"] != LAW_COUNTS
            or events[-2]["result"]["attempt_count"] != 4634
            or events[-2]["result"]["reserved_tokens"] != 191076747
            or not events[-2]["result"]["paused"]):
        raise ValueError("source did not pause at the documented contract breach")
    law = source / "subjects/professional_law"
    if (law / "items" / OMISSION["item_id"] / "result.json").exists():
        raise ValueError("omitted item already has an outcome")
    for subject in subjects[:22]:
        expected = manifest["subjects"][subject]["selected_count"]
        terminal = len(list((source / "subjects" / subject).glob("items/*/result.json")))
        if subject == "prehistory":
            expected = 320
        if subject == "professional_law":
            expected = sum(value for key, value in LAW_COUNTS.items() if key != "paused")
        if terminal != expected:
            raise ValueError(f"unexpected terminal count: {subject}")
    for subject in subjects[23:]:
        if list((source / "subjects" / subject).glob("items/*/result.json")) or list(
                (source / "subjects" / subject).glob("items/*/*/attempt-*/request.json")):
            raise ValueError("following subject was already started")
    new_files = source_files(repository / "src/dag_builder")
    if (set(new_files) != set(old_files) | {"operator_omission.py"}
            or {name for name in old_files if old_files[name] != new_files[name]}
            != {"pipeline.py", "mmlu_campaign.py"}):
        raise ValueError("unexpected source package change")
    requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses = errors = reserved = 0
    unknown = []
    for path in requests:
        amount = read_json(path).get("reserved_tokens")
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid request reservation")
        reserved += amount
        response = (path.parent / "response.json").is_file()
        error = (path.parent / "error.json").is_file()
        if response and error:
            raise ValueError("conflicting attempt evidence")
        responses += response
        errors += error
        if not response and not error:
            unknown.append(str(path.relative_to(source)))
    if ((len(requests), responses, errors, len(unknown), reserved) != EXPECTED_COUNTS
            or set(unknown) != set(previous["unknown_requests"])
            or len(requests) > MAX_CALLS or reserved > MAX_RESERVED):
        raise ValueError("historical request ledger changed")
    runner = repository / "scripts/run_mmlu_after_infra_omission.py"
    if not runner.is_file():
        raise ValueError("missing continuation runner")
    return {**previous,
            "source_run": str(source), "source_log_sha256": sha256(log),
            "source_code_commit": old_origin["git_commit"],
            "continuation_code_commit": commit,
            "new_source_files": new_files,
            "remaining_subjects": subjects[22:],
            "requests_imported": len(requests), "responses_imported": responses,
            "errors_imported": errors, "unknown_requests": unknown,
            "reserved_tokens_imported": reserved,
            "provider_overrun_reported_tokens": 41360,
            "provider_overrun_extra_accounted_tokens": 5104,
            "operator_omission": OMISSION,
            "scientific_code_changes": [
                "one hash-bound professional_law infrastructure omission",
                "all other response contract violations remain fail-closed",
            ]}


def prepare(source, destination, log, repository, expected_commit):
    source, destination, log, repository = (Path(path).absolute()
                                            for path in (source, destination, log, repository))
    if (source.resolve() != source or destination.resolve() != destination
            or repository.resolve() != repository or not source.is_dir()
            or not log.is_file() or destination.exists() or source == destination
            or source in destination.parents):
        raise ValueError("continuation requires a real source and fresh separate destination")
    proof = audit(source, log, repository, expected_commit)
    private_dir(destination)
    digest = hashlib.sha256()
    copied = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("source contains symlink")
        relative = path.relative_to(source)
        if (not path.is_file() or path.name in (".lock", "launchd-mmlu27.plist")
                or relative == Path("subjects/professional_law/implementation.json")
                or relative == Path("infra_omission_continuation_provenance.json")
                or relative.parts[0] == "code" or "__pycache__" in path.parts):
            continue
        copy_private(path, destination / relative)
        digest.update(str(relative).encode() + b"\0" + sha256(path).encode() + b"\n")
        copied += 1
    for relative in proof["new_source_files"]:
        copy_private(repository / "src/dag_builder" / relative,
                     destination / "code/dag_builder" / relative)
    runner = repository / "scripts/run_mmlu_after_infra_omission.py"
    copy_private(runner, destination / "code/run_mmlu_after_infra_omission.py")
    origin = {"git_commit": expected_commit,
              "source_files": proof.pop("new_source_files")}
    write_once(destination / "code/snapshot_origin.json", origin)
    subject_root = destination / "subjects/professional_law"
    write_once(subject_root / "operator_infrastructure_omission.json", OMISSION)
    if load_operator_omission(subject_root) != OMISSION:
        raise ValueError("operator omission failed exact evidence validation")
    proof["runner_sha256"] = sha256(destination / "code/run_mmlu_after_infra_omission.py")
    proof["copied_files"] = copied
    proof["copied_tree_manifest_sha256"] = digest.hexdigest()
    proof["exclusions"] = ["run lock", "old launchd configuration", "old code snapshot",
                           "Python bytecode cache", "old professional_law implementation",
                           "superseded continuation provenance"]
    write_once(destination / "infra_omission_continuation_provenance.json", proof)
    return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    proof = prepare(args.source, args.destination, args.log, args.repository,
                    args.expected_commit)
    print(json.dumps({key: proof[key] for key in
                      ("requests_imported", "responses_imported", "errors_imported",
                       "reserved_tokens_imported", "remaining_subjects", "copied_files")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
