"""Local-only CLI. Submitting Slurm jobs always requires a separate user action."""
import argparse
import json
import os
from .prepare import prepare
from .run import init_run, worker
from .analyze import analyze


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Frozen GPQA/HumanEval/LiveCodeBench PALS E1/E2 validation")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=20260915)
    p.add_argument("--expected-sha256")
    p.add_argument("--parent-probe", action="store_true")
    p.add_argument("--benchmark", choices=("gpqa", "humaneval", "livecodebench", "gsm8k", "mmlu"), default="gpqa")
    p.add_argument("--e1-break-overrides", help="Frozen, score-blind MMLU psychology E1 edge manifest")
    p = commands.add_parser("init")
    p.add_argument("--prepared", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--experiment", choices=("e1", "e2"), required=True)
    p.add_argument("--shards", type=int, default=8)
    p = commands.add_parser("worker")
    p.add_argument("--run", required=True)
    p.add_argument("--shard", type=int, required=True)
    p = commands.add_parser("analyze")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.source, args.output, args.seed, args.parent_probe,
                         args.expected_sha256, args.benchmark, args.e1_break_overrides)
    elif args.command == "init":
        result = init_run(args.prepared, args.config, args.output, args.experiment, args.shards)
    elif args.command == "worker":
        result = worker(args.run, args.shard)
    else:
        result = analyze(args.run, args.output)
        result = {k: result[k] for k in ("experiment", "accepted_jobs", "scientific_evidence", "token_arithmetic_verified")}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
