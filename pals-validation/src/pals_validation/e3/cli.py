"""Explicit E3 stage commands. Model inference is only in worker."""

import argparse
import json
import os

from .audit import audit_run
from .analyze import analyze_runs
from .data import prepare_sources
from .fixture import prepare_fixture
from .reference import reference_check
from .run import init_run, worker
from .report import create_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Freeze original sources; no inference")
    prepare.add_argument("--sources", required=True)
    prepare.add_argument("--out", required=True)
    fixture = commands.add_parser("mock-prepare", help="Synthetic fixture; never paper evidence")
    fixture.add_argument("--out", required=True)
    init = commands.add_parser("init", help="Freeze a model run; no inference")
    init.add_argument("--prepared", required=True)
    init.add_argument("--config", required=True)
    init.add_argument("--out", required=True)
    reference = commands.add_parser("reference", help="Numerical gate on a Slurm GPU before generation")
    reference.add_argument("--run", required=True)
    work = commands.add_parser("worker", help="Execute exactly one selected stage; inference may use GPU")
    work.add_argument("--run", required=True)
    work.add_argument("--stage", choices=("generate", "score", "evaluate", "common-score"), required=True)
    work.add_argument("--shard", type=int, required=True)
    work.add_argument("--shards", type=int, default=8)
    work.add_argument("--source-run")
    work.add_argument("--canary", action="store_true", help="Use only the frozen first batch per benchmark")
    audit = commands.add_parser("audit", help="Replay stored evidence offline")
    audit.add_argument("--run", required=True)
    audit.add_argument("--stage", choices=("generate", "score", "evaluate", "all", "canary"), default="all")
    audit.add_argument("--out", required=True)
    analyze = commands.add_parser("analyze", help="Aggregate three audited runs; no inference")
    analyze.add_argument("--runs", nargs=3, required=True)
    analyze.add_argument("--audits", nargs=3, required=True)
    analyze.add_argument("--out", required=True)
    report = commands.add_parser("report", help="Write the audited Chinese report")
    report.add_argument("--analysis", required=True)
    report.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == "prepare":
        result = prepare_sources(args.sources, args.out)
    elif args.command == "mock-prepare":
        result = prepare_fixture(args.out)
    elif args.command == "init":
        result = init_run(args.prepared, args.config, args.out)
    elif args.command == "reference":
        result = reference_check(args.run)
    elif args.command == "worker":
        if args.shards != 8:
            raise ValueError("E3 protocol fixes eight shards")
        result = worker(args.run, args.stage, args.shard, args.source_run, args.canary)
    elif args.command == "audit":
        result = audit_run(args.run, args.stage, args.out)
    elif args.command == "analyze":
        result = analyze_runs(args.runs, args.audits, args.out)
    else:
        result = create_report(args.analysis, args.out)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
