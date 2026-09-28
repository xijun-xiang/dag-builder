"""Freeze a stopped MMLU campaign into a separate execution-only continuation.

The original campaign is never modified. Every request, response, failure and
terminal item is copied; only the per-subject implementation record is replaced
because the new runner adds a runtime worker override. The task configuration,
prompts, model and budget remain identical.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from dag_builder.config import Config
from dag_builder.storage import private_dir, read_json, run_lock, write_once


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


def prepare(source, destination, log, repository, expected_commit, stopped_subject):
    source = source.absolute()
    destination = destination.absolute()
    repository = repository.absolute()
    if (source.resolve() != source or destination.resolve() != destination
            or repository.resolve() != repository or destination.exists()
            or source == destination or source in destination.parents):
        raise ValueError("continuation requires a fresh, separate non-symlinked directory")
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    if not events or events[-1] != {"event": "paused", "subject": stopped_subject}:
        raise ValueError("source campaign has not reached the requested paused boundary")
    commit = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    if commit != expected_commit or subprocess.check_output(
        ["git", "-C", str(repository), "status", "--porcelain"], text=True).strip():
        raise ValueError("continuation code must be a clean, committed snapshot")
    old_package = source / "code/dag_builder"
    new_package = repository / "src/dag_builder"
    old_files, new_files = source_files(old_package), source_files(new_package)
    if (set(old_files) != set(new_files)
            or {name for name in old_files if old_files[name] != new_files[name]}
            != {"mmlu_campaign.py"}):
        raise ValueError("unexpected scientific implementation or prompt change")
    config = Config.load(source / "continuation_config.json")
    if (config.task_type != "mmlu" or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.content_gated_response):
        raise ValueError("unexpected frozen source protocol")
    selection = read_json(source / "campaign_manifest.json")
    if selection["subject_count"] != 27 or selection["selected_count"] != 7888:
        raise ValueError("unexpected frozen MMLU selection")
    stop = source / "subjects" / stopped_subject / "operator-stop-request.json"
    if not stop.is_file():
        raise ValueError("missing operator stop request")
    with run_lock(source):
        requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
        responses = [path.parent / "response.json" for path in requests
                     if (path.parent / "response.json").is_file()]
        errors = [path.parent / "error.json" for path in requests
                  if (path.parent / "error.json").is_file()]
        unknown = [str(path.relative_to(source)) for path in requests
                   if not (path.parent / "response.json").exists()
                   and not (path.parent / "error.json").exists()]
        if len(unknown) != 6 or any(not path.startswith("subjects/business_ethics/")
                                    for path in unknown):
            raise ValueError("new unknown remote requests require separate audit")
        reserved = sum(read_json(path)["reserved_tokens"] for path in requests)
        if len(requests) > 66273 or reserved > 3313650000:
            raise ValueError("source campaign exceeded its hard budget")
        private_dir(destination)
        copied = {}
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("source artifact symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if (path.name == ".lock" or path.name == "operator-stop-request.json"
                    or path.name == "implementation.json" or relative.parts[0] == "code"):
                continue
            copy_private(path, destination / relative)
            copied[str(relative)] = sha256(path)
        for name in new_files:
            copy_private(new_package / name, destination / "code/dag_builder" / name)
        runner = repository / "scripts/run_mmlu_campaign_budgeted.py"
        copy_private(runner, destination / "code/run_mmlu_campaign_budgeted.py")
        origin = {"git_commit": commit, "source_files": new_files}
        write_once(destination / "code/snapshot_origin.json", origin)
        write_once(destination / "copied_file_manifest.json", copied)
        proof = {
            "source_run": str(source), "source_log_sha256": sha256(log),
            "stop_subject": stopped_subject, "stop_request_sha256": sha256(stop),
            "source_code_commit": read_json(source / "code/snapshot_origin.json")["git_commit"],
            "continuation_code_commit": commit,
            "scientific_code_changes": ["mmlu_campaign.py: execution-only runtime worker override"],
            "frozen_config_sha256": sha256(source / "continuation_config.json"),
            "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
            "configured_workers": 6, "runtime_workers": 32,
            "requests_imported": len(requests), "responses_imported": len(responses),
            "errors_imported": len(errors), "unknown_requests": unknown,
            "reserved_tokens_imported": reserved,
            "copied_files": len(copied),
            "copied_file_manifest_sha256": sha256(destination / "copied_file_manifest.json"),
            "exclusions": ["run locks", "operator stop marker", "old code snapshot",
                           "old per-subject implementation records"],
            "budget_policy": "All imported attempts and reservations count inside the unchanged global and subject caps; no semantic resampling.",
        }
        write_once(destination / "runtime_continuation_provenance.json", proof)
        return proof


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--stopped-subject", required=True)
    args = parser.parse_args()
    proof = prepare(args.source, args.destination, args.log, args.repository,
                    args.expected_commit, args.stopped_subject)
    print(json.dumps({key: proof[key] for key in (
        "stop_subject", "requests_imported", "responses_imported", "errors_imported",
        "reserved_tokens_imported", "continuation_code_commit")}, sort_keys=True))


if __name__ == "__main__":
    main()
