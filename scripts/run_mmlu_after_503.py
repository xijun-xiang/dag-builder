"""Continue untouched MMLU subjects, then the unfinished law items.

Subjects are independent. Changing execution order and runtime concurrency does
not change frozen prompts, model, selection, validation, or retry ceilings.
"""

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


def preflight(root, config_path, max_calls, max_tokens):
    root = Path(root)
    proof = read_json(root / "post503_continuation_provenance.json")
    manifest = read_json(root / "campaign_manifest.json")
    config = Config.load(config_path)
    subjects = list(manifest["subjects"])
    planned = sum(math.ceil(7 * row["selected_count"] * 1.2)
                  for row in manifest["subjects"].values())
    if (manifest["subject_count"] != 27 or manifest["selected_count"] != 7888
            or sha256(root / "campaign_manifest.json") != proof["frozen_selection_sha256"]
            or sha256(config_path) != proof["frozen_config_sha256"]
            or sha256(__file__) != proof["runner_sha256"]
            or config.model != "deepseek-v4-flash"
            or config.prompt_version != "mmlu-general-thinking-v4"
            or not config.strict_response_contract or not config.content_gated_response
            or max_calls != proof["max_total_calls"]
            or max_tokens != proof["max_total_reserved_tokens"]
            or planned > max_calls or 50000 * planned > max_tokens
            or subjects[23:] != proof["unstarted_subjects"]
            or subjects[22] != "professional_law"
            or (root / "subjects/professional_law/operator-stop-request.json").exists()):
        raise ValueError("post-503 continuation or frozen protocol changed")
    return config, manifest, [*proof["unstarted_subjects"], "professional_law"]


def law_pauses_are_only_known_exhausted(root):
    proof = read_json(root / "post503_continuation_provenance.json")
    invocations = [read_json(p) for p in
                   (root / "subjects/professional_law/invocations").glob("*.json")]
    if not invocations:
        return False
    latest = max(invocations, key=lambda row: row["ended_at"])
    paused = {row["item_id"]: row["stage"] for row in latest["results"]
              if row["status"] == "paused" and row.get("reason") == "transient_retries_exhausted"}
    all_paused = {row["item_id"] for row in latest["results"] if row["status"] == "paused"}
    return (paused == proof["law_exhausted_503"]
            and all_paused == set(paused) and not latest.get("global_stop"))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config", "key-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-total-calls", type=int, required=True)
    parser.add_argument("--max-total-reserved-tokens", type=int, required=True)
    parser.add_argument("--runtime-workers", type=int, default=32)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.runtime_workers not in (16, 32, 64):
        raise ValueError("runtime workers must be 16, 32, or 64")
    base, manifest, subjects = preflight(args.root, args.config,
                                         args.max_total_calls,
                                         args.max_total_reserved_tokens)
    if args.preflight_only:
        print(json.dumps({"preflight": "pass", "subjects": subjects,
                          "runtime_workers": args.runtime_workers}))
        return 0
    key = load_key(base.key_env, args.key_file)
    print(json.dumps({"event": "start_post503", "subjects": subjects,
                      "runtime_workers": args.runtime_workers}), flush=True)
    for subject in subjects:
        selected = manifest["subjects"][subject]["selected_count"]
        calls = math.ceil(7 * selected * 1.2)
        config = replace(base, max_calls=calls, max_reserved_tokens=50000 * calls)
        result = run_all(args.root, config, APIClient(config, key), [subject],
                         args.max_total_calls, args.max_total_reserved_tokens,
                         resilient=True, runtime_workers=args.runtime_workers)
        row = result["subjects_run"][subject]
        print(json.dumps({"event": "subject_complete", "subject": subject,
                          "result": row}, ensure_ascii=False), flush=True)
        if row["paused"]:
            if subject == "professional_law" and law_pauses_are_only_known_exhausted(args.root):
                print(json.dumps({"event": "complete_with_known_transport_unresolved",
                                  "subject": subject,
                                  "count": len(read_json(args.root / "post503_continuation_provenance.json")
                                               ["law_exhausted_503"])}), flush=True)
                return 0
            print(json.dumps({"event": "paused", "subject": subject}), flush=True)
            return 2
    print(json.dumps({"event": "complete_post503", "subjects": subjects}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
