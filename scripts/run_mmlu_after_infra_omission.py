"""Run only unstarted MMLU subjects after documented transport N/A items."""

import argparse
import hashlib
import json
import math
import os
from dataclasses import replace
from pathlib import Path

from dag_builder.client import APIClient, load_key
from dag_builder.config import Config
from dag_builder.mmlu_campaign import run_all
from dag_builder.storage import read_json


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(root, config_path, max_calls, max_reserved):
    root = Path(root)
    proof = read_json(root / "infra_omission_continuation_provenance.json")
    manifest = read_json(root / "campaign_manifest.json")
    config = Config.load(config_path)
    subjects = list(manifest["subjects"])
    completed_after_omission = proof.get("completed_after_omission", [])
    if (not isinstance(completed_after_omission, list)
            or completed_after_omission != subjects[21:21 + len(completed_after_omission)]):
        raise ValueError("post-omission completed-subject list changed")
    next_index = 21 + len(completed_after_omission)
    if (manifest["subject_count"] != 27 or manifest["selected_count"] != 7888
            or subjects[:20] != proof["completed_subjects"]
            or subjects[20] != proof["omitted_subject"] == "prehistory"
            or subjects[next_index:] != proof["remaining_subjects"]
            or set(proof["omitted_item_stages"]) !=
            {"ae625427c20fc16ccd62", "d3e6291ba7fa6f175676",
             "d3e998612e060c4a093d"}
            or proof["prehistory_terminal"] != 320
            or sha256(root / "campaign_manifest.json") != proof["frozen_selection_sha256"]
            or sha256(config_path) != proof["frozen_config_sha256"]
            or sha256(__file__) != proof["runner_sha256"]
            or max_calls != proof["max_total_calls"]
            or max_reserved != proof["max_total_reserved_tokens"]
            or config.prompt_version != "mmlu-general-thinking-v4"
            or config.model != "deepseek-v4-flash"):
        raise ValueError("continuation proof or frozen protocol mismatch")
    for subject in subjects[:20]:
        if (len(list((root / "subjects" / subject).glob("items/*/result.json")))
                != manifest["subjects"][subject]["selected_count"]):
            raise ValueError("completed subject changed")
    for subject in completed_after_omission:
        if (len(list((root / "subjects" / subject).glob("items/*/result.json")))
                != manifest["subjects"][subject]["selected_count"]):
            raise ValueError("post-omission completed subject changed")
    if len(list((root / "subjects/prehistory").glob("items/*/result.json"))) != 320:
        raise ValueError("omitted subject changed")
    planned_calls = sum(math.ceil(7 * row["selected_count"] * 1.2)
                        for row in manifest["subjects"].values())
    if planned_calls > max_calls or 50000 * planned_calls > max_reserved:
        raise ValueError("budget does not cover subject-local caps")
    return config, manifest, subjects[next_index:]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--max-total-calls", type=int, required=True)
    parser.add_argument("--max-total-reserved-tokens", type=int, required=True)
    parser.add_argument("--runtime-workers", type=int, default=32)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.runtime_workers not in (32, 64):
        raise ValueError("execution concurrency must be 32 or 64")
    base, manifest, subjects = preflight(args.root, args.config,
                                         args.max_total_calls,
                                         args.max_total_reserved_tokens)
    if args.preflight_only:
        print(json.dumps({"preflight": "pass", "remaining_subjects": subjects}))
        return 0
    key = load_key(base.key_env, args.key_file)
    print(json.dumps({"event": "start_after_infra_omission", "subjects": subjects,
                      "omitted_subject": "prehistory", "omitted_items": 3,
                      "prompt_version": base.prompt_version}), flush=True)
    for subject in subjects:
        selected = manifest["subjects"][subject]["selected_count"]
        calls = math.ceil(7 * selected * 1.2)
        config = replace(base, max_calls=calls,
                         max_reserved_tokens=50000 * calls)
        result = run_all(args.root, config, APIClient(config, key), [subject],
                         args.max_total_calls, args.max_total_reserved_tokens,
                         resilient=True, runtime_workers=args.runtime_workers)
        print(json.dumps({"event": "subject_complete", "subject": subject,
                          "result": result["subjects_run"][subject]},
                         ensure_ascii=False), flush=True)
        if result["paused"]:
            print(json.dumps({"event": "paused", "subject": subject}), flush=True)
            return 2
    print(json.dumps({"event": "complete_after_infra_omission",
                      "subjects": len(subjects), "omitted_items": 3}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
