"""Snapshot the second stopped MMLU run with a user-approved overrun policy.

Only the execution classification changes. The source run and every raw
request/response remain untouched; no semantic failure is resampled.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from dag_builder.config import Config
from dag_builder.operator_omission import OMISSION, load_operator_omission
from dag_builder.provider_overrun import POLICY
from dag_builder.response_contract import check_response, reported_tokens
from dag_builder.storage import private_dir, read_json, write_once

from prepare_mmlu_contract_omission_continuation import copy_private, sha256, source_files


COUNTS = (39971, 39824, 109, 38, 1501423422)
LAW_STATUS = {"model_accepted": 223, "needs_review": 519, "rejected": 248,
              "infrastructure_omitted": 1, "paused": 543}
SECOND_ITEM = "84e63503b744a2d5d40e"
SECOND_REQUEST_SHA = "47e549d27f6cd12714e6549ea2d00073c9631b3c98c3f9b3bacd1a41821265b2"
SECOND_RESPONSE_SHA = "049e3b4c369b6ecd8b77835333e069da7fd4b8ab187315f6f54ce037bf7ad9f8"


def audit(source, log, repository, expected_commit):
    manifest = read_json(source / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    previous = read_json(source / "infra_omission_continuation_provenance.json")
    origin = read_json(source / "code/snapshot_origin.json")
    old_files = source_files(source / "code/dag_builder")
    config = Config.load(source / "continuation_config.json")
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"],
                                     text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"],
                                    text=True).strip()
    if (len(subjects) != 27 or manifest["selected_count"] != 7888
            or subjects[22:] != previous["remaining_subjects"]
            or previous["continuation_code_commit"] != origin["git_commit"]
            or old_files != origin["source_files"]
            or previous["frozen_selection_sha256"] != sha256(source / "campaign_manifest.json")
            or previous["frozen_config_sha256"] != sha256(source / "continuation_config.json")
            or previous["max_total_calls"] != 66273
            or previous["max_total_reserved_tokens"] != 3313650000
            or config.task_type != "mmlu" or config.model != POLICY["model"]
            or config.prompt_version != POLICY["prompt_version"]
            or config.workers != 6 or not config.strict_response_contract
            or not config.content_gated_response or commit != expected_commit or dirty):
        raise ValueError("source, protocol, or committed code changed")
    if (events[-1] != {"event": "paused", "subject": "professional_law"}
            or events[-2].get("event") != "subject_complete"
            or events[-2].get("subject") != "professional_law"
            or events[-2]["result"]["status_counts"] != LAW_STATUS
            or events[-2]["result"]["attempt_count"] != 5090
            or events[-2]["result"]["reserved_tokens"] != 210984481
            or not events[-2]["result"]["paused"]):
        raise ValueError("source did not stop at the second observed overrun")
    law = source / "subjects/professional_law"
    if (load_operator_omission(law) != OMISSION
            or read_json(law / "items" / OMISSION["item_id"] / "result.json")["status"]
            != "infrastructure_omitted"
            or (law / "items" / SECOND_ITEM / "result.json").exists()):
        raise ValueError("the first omission or second stopped item changed")
    for subject in subjects[:22]:
        terminal = len(list((source / "subjects" / subject).glob("items/*/result.json")))
        expected = manifest["subjects"][subject]["selected_count"]
        if subject == "prehistory":
            expected = 320
        if subject == "professional_law":
            expected = sum(value for key, value in LAW_STATUS.items() if key != "paused")
        if terminal != expected:
            raise ValueError(f"unexpected terminal count: {subject}")
    for subject in subjects[23:]:
        if list((source / "subjects" / subject).glob("items/*/result.json")) or list(
                (source / "subjects" / subject).glob("items/*/*/attempt-*/request.json")):
            raise ValueError("a following subject was already started")
    second = law / "items" / SECOND_ITEM / "solve/attempt-00"
    if (sha256(second / "request.json") != SECOND_REQUEST_SHA
            or sha256(second / "response.json") != SECOND_RESPONSE_SHA):
        raise ValueError("second overrun raw evidence changed")
    request = read_json(second / "request.json")
    response = read_json(second / "response.json")["body"]
    check = check_response(request["payload"], response, request["reserved_tokens"],
                           strict=True, content_gated=True)
    if (request["reserved_tokens"] != 34509
            or check["reported_tokens"] != 39522
            or check["violations"] != [POLICY["only_violation"]]
            or check["warnings"] != [POLICY["required_warning"]]):
        raise ValueError("second overrun contract classification changed")
    requests = sorted(source.glob("subjects/*/items/*/*/attempt-*/request.json"))
    responses = errors = reserved = 0
    unknown = []
    overages = []
    for path in requests:
        req = read_json(path)
        amount = req.get("reserved_tokens")
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid request reservation")
        reserved += amount
        response_path = path.parent / "response.json"
        error_path = path.parent / "error.json"
        if response_path.is_file() and error_path.is_file():
            raise ValueError("conflicting attempt evidence")
        if response_path.is_file():
            responses += 1
            if reported_tokens(read_json(response_path)["body"]) > amount:
                overages.append(str(path.relative_to(source)))
        elif error_path.is_file():
            errors += 1
        else:
            unknown.append(str(path.relative_to(source)))
    expected_overages = {
        str((law / "items" / OMISSION["item_id"] / "solve/attempt-00/request.json")
            .relative_to(source)),
        str((second / "request.json").relative_to(source)),
    }
    if ((len(requests), responses, errors, len(unknown), reserved) != COUNTS
            or set(unknown) != set(previous["unknown_requests"])
            or set(overages) != expected_overages):
        raise ValueError("historical ledger or overrun cohort changed")
    new_files = source_files(repository / "src/dag_builder")
    if (set(new_files) != set(old_files) | {"provider_overrun.py"}
            or {name for name in old_files if old_files[name] != new_files[name]}
            != {"pipeline.py", "mmlu_campaign.py"}):
        raise ValueError("unexpected package change")
    runner = repository / "scripts/run_mmlu_after_infra_omission.py"
    if not runner.is_file():
        raise ValueError("missing continuation runner")
    return {**previous,
            "source_run": str(source), "source_log_sha256": sha256(log),
            "source_code_commit": origin["git_commit"],
            "continuation_code_commit": commit,
            "new_source_files": new_files,
            "requests_imported": len(requests), "responses_imported": responses,
            "errors_imported": errors, "unknown_requests": unknown,
            "reserved_tokens_imported": reserved,
            "provider_overrun_extra_accounted_tokens": 10117,
            "provider_overrun_policy": POLICY,
            "provider_overrun_attempts": sorted(expected_overages),
            "scientific_code_changes": [
                "MMLU-only user-approved N/A for an otherwise valid provider token-cap overrun",
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
    write_once(destination / "code/snapshot_origin.json", {
        "git_commit": expected_commit, "source_files": proof.pop("new_source_files")})
    write_once(destination / "provider_overrun_policy.json", POLICY)
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
                       "reserved_tokens_imported", "copied_files")}, sort_keys=True))


if __name__ == "__main__":
    main()
