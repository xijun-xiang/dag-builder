"""CLI never accepts a literal API key or executes a credential setup script."""

import argparse
import json
import os
import sys
from pathlib import Path

from .client import APIClient, CallFailure, load_key
from .config import Config
from .contract_probe import probe_contract
from .export import release
from .gpqa_source import prepare_gpqa
from .humaneval_source import prepare_humaneval
from .humaneval_export import export_validation
from .humaneval_recovery import audit_quality, prepare_recovery
from .humaneval_repair import prepare_diagnosed_repair, recovery_pipeline_type, PROTOCOL, PROTOCOL_VERSIONS
from .pipeline import Pipeline
from .repair import RepairPipeline
from .repair_loop import RevisionPipeline
from .repair_source import prepare_repair
from .report import overview, render
from .source import prepare
from .mmlu_campaign import prepare_all as prepare_all_mmlu, run_all as run_all_mmlu
from .mmlu_catalog import MMLU_SUBJECTS
from .mmlu_export import export_subject as export_mmlu_subject, export_campaign as export_mmlu_campaign
from .stages import THINKING_STAGES
from .storage import private_dir, read_json, run_lock


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit-humaneval-quality", help="Offline score-blind quality/recovery inventory")
    audit.add_argument("--source-root", required=True, type=Path)
    audit.add_argument("--output", required=True, type=Path)
    recheck = commands.add_parser("recheck-humaneval-contracts", help="Offline replay of two v1 contract failures; no promotion")
    recheck.add_argument("--source-root", required=True, type=Path)
    recheck.add_argument("--output", required=True, type=Path)
    continuation = commands.add_parser("prepare-humaneval-continuation", help="Offline continuation of all passing contract rechecks")
    continuation.add_argument("--source-root", required=True, type=Path)
    continuation.add_argument("--recheck-root", required=True, type=Path)
    continuation.add_argument("--root", required=True, type=Path)
    recovery = commands.add_parser("prepare-humaneval-recovery", help="Offline one-round snapshot; no API calls")
    recovery.add_argument("--source-root", required=True, type=Path)
    recovery.add_argument("--root", required=True, type=Path)
    recovery.add_argument("--include-semantic", action="store_true",
                          help="Also select diagnosed semantic/structural failures; default is lossless format only")
    diagnosed = commands.add_parser("prepare-humaneval-repair", help="Offline full unresolved cohort, one diagnosed repair")
    diagnosed.add_argument("--source-root", required=True, type=Path)
    diagnosed.add_argument("--format-root", type=Path, help="Completed direct lossless recovery only")
    diagnosed.add_argument("--root", required=True, type=Path)
    diagnosed.add_argument("--quarantines", type=Path, help="Evidence-bound source concerns, never acceptance overrides")
    diagnosed.add_argument("--protocol", choices=tuple(PROTOCOL_VERSIONS), default=PROTOCOL,
                           help="Use humaneval-diagnosed-repair-v2 with reference-v5 for the corrected contract")
    humaneval = commands.add_parser("prepare-humaneval", help="Offline pinned HumanEval reference-code import; no execution")
    humaneval.add_argument("--root", required=True, type=Path)
    humaneval.add_argument("--source-file", required=True, type=Path)
    humaneval.add_argument("--revision", required=True)
    humaneval.add_argument("--expected-sha256", required=True)
    humaneval.add_argument("--count", type=int, default=164)
    humaneval.add_argument("--seed", type=int, default=20260921)
    humaneval.add_argument("--exclusions", type=Path, help="Frozen JSON list of source-quality exclusions with evidence")
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
    source_count = source.add_mutually_exclusive_group()
    source_count.add_argument("--count", type=int, default=30)
    source_count.add_argument("--all-eligible", action="store_true",
                              help="Select every mechanically eligible item")
    source.add_argument("--seed", type=int, default=20260909)
    mmlu_all = commands.add_parser("prepare-mmlu-all", help="Prepare all 57 pinned MMLU subjects, no API calls")
    mmlu_all.add_argument("--root", required=True, type=Path)
    mmlu_all.add_argument("--revision", required=True)
    mmlu_all.add_argument("--split", default="test", choices=("dev", "validation", "test"))
    mmlu_all.add_argument("--count-per-subject", type=int,
                          help="Omit to select all mechanically eligible items")
    mmlu_all.add_argument("--seed", type=int, default=20260909)
    mmlu_all.add_argument("--source-dir", type=Path,
                          help="Optional local Parquet tree: SUBJECT/SPLIT-00000-of-00001.parquet")
    mmlu_run = commands.add_parser("run-mmlu-all", help="Explicitly budgeted sequential MMLU campaign")
    mmlu_run.add_argument("--root", required=True, type=Path)
    mmlu_run.add_argument("--config", required=True, type=Path)
    mmlu_run.add_argument("--key-file", type=Path)
    selection = mmlu_run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all-subjects", action="store_true")
    selection.add_argument("--subjects", help="Comma-separated official subject IDs")
    mmlu_run.add_argument("--max-total-calls", required=True, type=int)
    mmlu_run.add_argument("--max-total-reserved-tokens", required=True, type=int)
    mmlu_run.add_argument("--limit-per-subject", type=int)
    mmlu_run.add_argument("--resilient", action="store_true")
    mmlu_export = commands.add_parser("export-mmlu-subject", help="Offline all-outcome and unified PALS export")
    mmlu_export.add_argument("--root", required=True, type=Path)
    mmlu_export.add_argument("--output-dir", required=True, type=Path)
    mmlu_export_all = commands.add_parser("export-mmlu-all", help="Require all 57 completed subjects, then combine PALS exports")
    mmlu_export_all.add_argument("--campaign-root", required=True, type=Path)
    mmlu_export_all.add_argument("--output-dir", required=True, type=Path)
    for name in ("probe", "probe-contract", "run", "repair", "recover-humaneval"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument(
            "--key-file",
            type=Path,
            help="Private credential file path, NEVER a literal key",
        )
        if name == "probe-contract":
            command.add_argument("--root", required=True, type=Path)
            command.add_argument("--configured-max-tokens", action="store_true",
                                 help="Use the exact configured output cap (up to 32768), not the small diagnostic cap; still at most two calls")
        if name in ("run", "repair", "recover-humaneval"):
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
        if args.command == "audit-humaneval-quality":
            result = audit_quality(args.source_root, args.output)
        elif args.command == "recheck-humaneval-contracts":
            from .humaneval_recheck import recheck_contracts
            result = recheck_contracts(args.source_root, args.output)
        elif args.command == "prepare-humaneval-continuation":
            from .humaneval_continuation import prepare_continuation
            with run_lock(args.root):
                result = prepare_continuation(args.root, args.source_root, args.recheck_root)
        elif args.command == "prepare-humaneval-recovery":
            with run_lock(args.root):
                result = prepare_recovery(args.root, args.source_root, args.include_semantic)
        elif args.command == "prepare-humaneval-repair":
            with run_lock(args.root):
                result = prepare_diagnosed_repair(args.root, args.source_root, args.format_root,
                                                 read_json(args.quarantines) if args.quarantines else None,
                                                 protocol=args.protocol)
        elif args.command == "prepare-humaneval":
            with run_lock(args.root):
                result = prepare_humaneval(args.root, args.source_file, args.revision,
                                          args.expected_sha256, args.count, args.seed,
                                          read_json(args.exclusions) if args.exclusions else None)
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
                    None if args.all_eligible else args.count,
                    args.seed,
                    args.dataset,
                    args.source_parquet,
                )
        elif args.command == "prepare-mmlu-all":
            if args.root.exists() and not args.root.is_dir():
                raise ValueError("campaign root must be a directory")
            with run_lock(args.root):
                result = prepare_all_mmlu(args.root, args.revision, args.split,
                                          args.count_per_subject, args.seed, args.source_dir)
        elif args.command == "run-mmlu-all":
            config = Config.load(args.config)
            subjects = list(MMLU_SUBJECTS) if args.all_subjects else args.subjects.split(",")
            if any(subject != subject.strip() for subject in subjects):
                raise ValueError("subject IDs must not contain whitespace")
            client = APIClient(config, load_key(config.key_env, args.key_file))
            result = run_all_mmlu(args.root, config, client, subjects,
                                  args.max_total_calls, args.max_total_reserved_tokens,
                                  args.limit_per_subject, args.resilient,
                                  progress=lambda row: print(json.dumps(row), flush=True))
        elif args.command == "export-mmlu-subject":
            result = export_mmlu_subject(args.root, args.output_dir)
        elif args.command == "export-mmlu-all":
            result = export_mmlu_campaign(args.campaign_root, args.output_dir)
        elif args.command in ("probe", "probe-contract", "run", "repair", "recover-humaneval"):
            config = Config.load(args.config)
            client = APIClient(config, load_key(config.key_env, args.key_file))
            if args.command == "probe":
                result = client.probe()
            elif args.command == "probe-contract":
                result = probe_contract(args.root, config, client,
                                        configured_max_tokens=args.configured_max_tokens)
            else:
                progress = lambda row: print(json.dumps(row), flush=True)
                if args.command == "recover-humaneval":
                    runner = recovery_pipeline_type(args.root)(args.root, config, client, resilient=args.resilient)
                    result = runner.run(args.limit, progress=progress)
                elif args.command == "repair":
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
        if args.command in ("run", "run-mmlu-all", "repair", "recover-humaneval") and result["paused"]:
            return 2
        if args.command == "probe-contract" and not result["passed"]:
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
