"""Snapshot an operator-paused MMLU campaign for a 64-worker continuation.

The old run remains untouched. Only execution concurrency validators and the
runner change; original task data, prompts, model, quality checks and budget
are copied unchanged. Three earlier transport omissions remain explicit N/A.
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


MAX_CALLS = 66273
MAX_RESERVED = 3313650000
EXPECTED_OMISSIONS = {
    "ae625427c20fc16ccd62": "review_dag",
    "d3e6291ba7fa6f175676": "atomize",
    "d3e998612e060c4a093d": "atomize",
}


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
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    selection = read_json(source / "campaign_manifest.json")
    subjects = list(selection["subjects"])
    if (len(subjects) != 27 or selection["selected_count"] != 7888
            or subjects[20:22] != ["prehistory", "professional_accounting"]
            or events[0].get("event") != "start_after_infra_omission"
            or events[-1] != {"event": "paused", "subject": "professional_law"}
            or events[-2].get("event") != "subject_complete"
            or events[-2].get("subject") != "professional_law"
            or not events[-2]["result"]["paused"]
            or events[-3].get("event") != "subject_complete"
            or events[-3].get("subject") != "professional_accounting"
            or events[-3]["result"]["paused"]):
        raise ValueError("source did not pause at clean law boundary")
    old_proof = read_json(source / "infra_omission_continuation_provenance.json")
    if (old_proof["omitted_item_stages"] != EXPECTED_OMISSIONS
            or old_proof["remaining_subjects"] != subjects[21:]
            or old_proof["prehistory_terminal"] != 320
            or old_proof["frozen_selection_sha256"] != sha256(source / "campaign_manifest.json")
            or old_proof["frozen_config_sha256"] != sha256(source / "continuation_config.json")):
        raise ValueError("prior omission provenance changed")
    config = Config.load(source / "continuation_config.json")
    if (config.task_type != "mmlu" or config.model != "deepseek-v4-flash"
            or config.prompt_version != "mmlu-general-thinking-v4"
            or config.workers != 6 or not config.content_gated_response):
        raise ValueError("frozen scientific configuration changed")
    old_origin = read_json(source / "code/snapshot_origin.json")
    old_package = source / "code/dag_builder"
    old_files = source_files(old_package)
    if (old_origin["git_commit"] != old_proof["source_code_commit"]
            or old_files != old_origin["source_files"]):
        raise ValueError("frozen source package changed")
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"],
                                     text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"],
                                    text=True).strip()
    if commit != expected_commit or dirty:
        raise ValueError("continuation code must be a clean committed snapshot")
    new_package = repository / "src/dag_builder"
    new_files = source_files(new_package)
    if (set(old_files) != set(new_files)
            or {name for name in old_files if old_files[name] != new_files[name]}
            != {"mmlu_campaign.py", "pipeline.py"}):
        raise ValueError("unexpected scientific code or prompt change")
    stop = source / "subjects/professional_law/operator-stop-request.json"
    if not stop.is_file():
        raise ValueError("missing operator stop marker")
    for subject in (*subjects[:20], "professional_accounting"):
        if (len(list((source / "subjects" / subject).glob("items/*/result.json")))
                != selection["subjects"][subject]["selected_count"]):
            raise ValueError("completed subject has missing results")
    if len(list((source / "subjects/prehistory").glob("items/*/result.json"))) != 320:
        raise ValueError("prehistory omission denominator changed")
    for subject in subjects[22:]:
        if list((source / "subjects" / subject).glob("items/*/result.json")):
            raise ValueError("unstarted subject has results")
        if list((source / "subjects" / subject).glob("items/*/*/attempt-*/request.json")):
            raise ValueError("unstarted subject has paid requests")
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
            raise ValueError("request has conflicting terminal evidence")
        responses += response
        errors += error
        if not response and not error:
            unknown.append(str(request.relative_to(source)))
    if ((len(requests), responses, errors, len(unknown), reserved)
            != (34881, 34765, 78, 38, 1290449058)
            or set(unknown) != set(old_proof["unknown_requests"])
            or len(requests) > MAX_CALLS or reserved > MAX_RESERVED):
        raise ValueError("historical request ledger changed")
    runner = repository / "scripts/run_mmlu_after_infra_omission.py"
    if not runner.is_file():
        raise ValueError("missing continuation runner")
    return {
        "source_run": str(source), "source_log_sha256": sha256(log),
        "stop_subject": "professional_law", "stop_request_sha256": sha256(stop),
        "source_code_commit": old_origin["git_commit"],
        "continuation_code_commit": commit,
        "old_source_files": old_files, "new_source_files": new_files,
        "frozen_selection_sha256": sha256(source / "campaign_manifest.json"),
        "frozen_config_sha256": sha256(source / "continuation_config.json"),
        "completed_subjects": subjects[:20],
        "completed_after_omission": ["professional_accounting"],
        "omitted_subject": "prehistory", "omitted_item_stages": EXPECTED_OMISSIONS,
        "prehistory_terminal": 320, "remaining_subjects": subjects[22:],
        "configured_workers": 6, "runtime_workers": 64,
        "requests_imported": len(requests), "responses_imported": responses,
        "errors_imported": errors, "unknown_requests": unknown,
        "reserved_tokens_imported": reserved,
        "max_total_calls": MAX_CALLS, "max_total_reserved_tokens": MAX_RESERVED,
        "scientific_code_changes": [
            "mmlu_campaign.py: runtime worker upper bound 32 to 64",
            "pipeline.py: MMLU-only runtime worker upper bound 32 to 64",
        ],
    }


def prepare(source, destination, log, repository, expected_commit):
    source, destination, log, repository = (Path(path).absolute()
                                            for path in (source, destination, log, repository))
    if (source.resolve() != source or destination.resolve() != destination
            or repository.resolve() != repository or not source.is_dir()
            or not log.is_file() or destination.exists() or source == destination
            or source in destination.parents):
        raise ValueError("continuation requires a real source and fresh separate destination")
    with run_lock(source):
        proof = audit(source, log, repository, expected_commit)
        private_dir(destination)
        digest = hashlib.sha256()
        copied = 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("source contains symlink")
            relative = path.relative_to(source)
            if (not path.is_file() or path.name in (".lock", "operator-stop-request.json",
                                                    "launchd-mmlu27.plist")
                    or relative == Path("subjects/professional_law/implementation.json")
                    or relative == Path("infra_omission_continuation_provenance.json")
                    or relative.parts[0] == "code"
                    or "__pycache__" in path.parts):
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
        proof.pop("old_source_files")
        write_once(destination / "code/snapshot_origin.json", origin)
        proof["runner_sha256"] = sha256(destination / "code/run_mmlu_after_infra_omission.py")
        proof["copied_files"] = copied
        proof["copied_tree_manifest_sha256"] = digest.hexdigest()
        proof["exclusions"] = ["run lock", "operator stop marker", "old launchd configuration",
                                "old code snapshot", "Python bytecode cache",
                                "unstarted law subject's 32-worker implementation record",
                                "prior continuation provenance superseded by this one"]
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
