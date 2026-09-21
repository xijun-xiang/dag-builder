"""CLI never accepts a literal API key or executes a credential setup script."""

import argparse
import json
import os
import sys
from pathlib import Path

from .client import APIClient, CallFailure, load_key
from .config import Config
from .export import release
from .gpqa_source import prepare_gpqa
from .humaneval_source import prepare_humaneval
from .humaneval_export import export_validation
from .pipeline import Pipeline
from .repair import RepairPipeline
from .repair_loop import RevisionPipeline
from .repair_source import prepare_repair
from .report import overview, render
from .source import prepare
from .stages import THINKING_STAGES
from .storage import private_dir, run_lock


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    humaneval = commands.add_parser("prepare-humaneval", help="Offline pinned HumanEval reference-code import; no execution")
    humaneval.add_argument("--root", required=True, type=Path)
    humaneval.add_argument("--source-file", required=True, type=Path)
    humaneval.add_argument("--revision", required=True)
    humaneval.add_argument("--expected-sha256", required=True)
    humaneval.add_argument("--count", type=int, default=164)
    humaneval.add_argument("--seed", type=int, default=20260921)
    repair_source = commands.add_parser(
        "prepare-repair",
        help="Snapshot terminal GPQA failures without changing their source run",
    )
    repair_source.add_argument("--root", required=True, type=Path)
    repair_source.add_argument("--source-root", required=True, type=Path)
    gpqa = commands.add_parser(
        "prepare-gpqa", help="Pinned official Diamond explanations; no solve stage"
    )
    gpqa.add_argument("--root", required=True, type=Path)
    gpqa.add_argument("--count", type=int, default=30)
    gpqa.add_argument("--seed", type=int, default=20260910)
    gpqa.add_argument(
        "--exclude-root",
        type=Path,
        help="Exclude all items in this prior run; sample proportionally from remaining domains",
    )
    gpqa.add_argument(
        "--source-archive",
        type=Path,
        help="Optional official ZIP, verified against pinned hash",
    )
    source = commands.add_parser("prepare")
    source.add_argument("--root", required=True, type=Path)
    source.add_argument("--revision", required=True)
    source.add_argument("--dataset", choices=("mmlu", "gsm8k"), default="mmlu")
    source.add_argument(
        "--source-parquet",
        type=Path,
        help="Optional already-downloaded source Parquet; its bytes and hash are preserved",
    )
    source.add_argument(
        "--subset",
        default=None,
        help="MMLU subset or GSM8K config; defaults to high_school_physics or main",
    )
    source.add_argument(
        "--split", default="test", choices=("dev", "validation", "test")
    )
    source.add_argument("--count", type=int, default=30)
    source.add_argument("--seed", type=int, default=20260909)
    for name in ("probe", "run", "repair"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument(
            "--key-file",
            type=Path,
            help="Private credential file path, NEVER a literal key",
        )
        if name in ("run", "repair"):
            command.add_argument(
                "--resilient",
                action="store_true",
                help="Retry transient transport errors up to 4 lifetime stage attempts; continue other items after exhaustion",
            )
            command.add_argument("--root", required=True, type=Path)
            command.add_argument("--limit", type=int)
        if name == "run":
            command.add_argument(
                "--isolate-uncertain-failures",
                action="store_true",
                help="Skip uncertain items without retry; stop after 3 new uncertain failures",
            )
            command.add_argument(
                "--through", choices=THINKING_STAGES, default="review_dag"
            )
            command.add_argument(
                "--retry-safe-failures",
                action="store_true",
                help="Explicitly retry prior authentication failures only",
            )
    for name in ("status", "report", "release", "export-humaneval-validation"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        if name == "release":
            command.add_argument("--human-review", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare-humaneval":
            with run_lock(args.root):
                result = prepare_humaneval(args.root, args.source_file, args.revision,
                                          args.expected_sha256, args.count, args.seed)
        elif args.command == "prepare-repair":
            with run_lock(args.root):
                result = prepare_repair(args.root, args.source_root)
        elif args.command == "prepare-gpqa":
            with run_lock(args.root):
                result = prepare_gpqa(
                    args.root,
                    args.count,
                    args.seed,
                    args.source_archive,
                    args.exclude_root,
                )
        elif args.command == "prepare":
            with run_lock(args.root):
                result = prepare(
                    args.root,
                    args.revision,
                    args.subset
                    or ("high_school_physics" if args.dataset == "mmlu" else "main"),
                    args.split,
                    args.count,
                    args.seed,
                    args.dataset,
                    args.source_parquet,
                )
        elif args.command in ("probe", "run", "repair"):
            config = Config.load(args.config)
            client = APIClient(config, load_key(config.key_env, args.key_file))
            if args.command == "probe":
                result = client.probe()
            else:
                progress = lambda row: print(json.dumps(row), flush=True)
                if args.command == "repair":
                    pipeline_type = (
                        RevisionPipeline
                        if config.prompt_version == "gpqa-revision-v1"
                        else RepairPipeline
                    )
                    runner = pipeline_type(
                        args.root, config, client, resilient=args.resilient
                    )
                    result = runner.run(args.limit, progress=progress)
                else:
                    runner = Pipeline(
                        args.root,
                        config,
                        client,
                        args.retry_safe_failures,
                        args.isolate_uncertain_failures,
                        resilient=args.resilient,
                    )
                    result = runner.run(
                        args.limit, progress=progress, through=args.through
                    )
        else:
            root = private_dir(args.root)
            with run_lock(root):
                result = (
                    overview(root)
                    if args.command == "status"
                    else str(render(root))
                    if args.command == "report"
                    else str(export_validation(root))
                    if args.command == "export-humaneval-validation"
                    else str(release(root, args.human_review))
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command in ("run", "repair") and result["paused"]:
            return 2
        return 0
    except CallFailure as error:
        print(
            json.dumps({"error": error.category, "http_status": error.status}),
            file=sys.stderr,
        )
        return 2
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        # Exception text can contain external data or credentials. Never echo it.
        print(
            "Operation failed: check configuration, credential availability, permissions and immutable artifacts.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
