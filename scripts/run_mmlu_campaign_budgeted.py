"""Run a frozen MMLU campaign with subject-size-aware hard request ceilings.

Every source-eligible question can reach seven stages. The additional 20% is
reserved for bounded transient retries, not for changing a rejected answer or
repairing a semantic graph. A paused subject stops the campaign for audit.
"""

import argparse
import json
import math
import os
from dataclasses import replace
from pathlib import Path

from dag_builder.client import APIClient, load_key
from dag_builder.config import Config
from dag_builder.mmlu_campaign import run_all
from dag_builder.storage import read_json


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--max-total-calls", type=int, required=True)
    parser.add_argument("--max-total-reserved-tokens", type=int, required=True)
    parser.add_argument("--runtime-workers", type=int,
                        help="Execution-only concurrency (1..32); frozen task protocol is unchanged")
    args = parser.parse_args()

    base = Config.load(args.config)
    manifest = read_json(args.root / "campaign_manifest.json")
    subjects = list(manifest["subjects"])
    if manifest["subject_count"] != len(subjects) or not subjects:
        raise ValueError("invalid campaign manifest")
    planned_calls = sum(math.ceil(7 * row["selected_count"] * 1.2)
                        for row in manifest["subjects"].values())
    planned_tokens = 50_000 * planned_calls
    if planned_calls > args.max_total_calls or planned_tokens > args.max_total_reserved_tokens:
        raise ValueError("global ceilings do not cover subject-local worst cases")
    key = load_key(base.key_env, args.key_file)
    print(json.dumps({"event": "start", "subjects": len(subjects),
                      "selected_questions": manifest["selected_count"],
                      "planned_max_calls": planned_calls,
                      "planned_max_reserved_tokens": planned_tokens,
                      "prompt_version": base.prompt_version}), flush=True)
    for subject in subjects:
        selected = manifest["subjects"][subject]["selected_count"]
        calls = math.ceil(7 * selected * 1.2)
        config = replace(base, max_calls=calls,
                         max_reserved_tokens=50_000 * calls)
        result = run_all(args.root, config, APIClient(config, key), [subject],
                         args.max_total_calls, args.max_total_reserved_tokens,
                         resilient=True, runtime_workers=args.runtime_workers)
        print(json.dumps({"event": "subject_complete", "subject": subject,
                          "result": result["subjects_run"][subject]},
                         ensure_ascii=False), flush=True)
        if result["paused"]:
            print(json.dumps({"event": "paused", "subject": subject}), flush=True)
            return 2
    print(json.dumps({"event": "complete", "subjects": len(subjects)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
