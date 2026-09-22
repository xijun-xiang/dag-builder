"""Launch a prepared pilot locally from a private, immutable code snapshot.

No credentials or benchmark text are printed. The API key remains in its original
private file. This is not an infinite retry service: Pipeline enforces the budget
and transient retry policy; semantic/integrity failures are preserved, not repaired.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def pipeline_type(root, config):
    """Select the explicit protocol; a prepared recovery must never run fresh."""
    from dag_builder.humaneval_repair import recovery_pipeline_type
    from dag_builder.pipeline import Pipeline
    from dag_builder.repair import RepairPipeline
    from dag_builder.repair_loop import RevisionPipeline

    if config.task_type == "livecodebench":
        from dag_builder.livecodebench_reference import LiveCodeBenchReferencePipeline
        if (root / "recovery_manifest.json").exists():
            raise ValueError("LiveCodeBench cannot consume a HumanEval recovery manifest")
        return LiveCodeBenchReferencePipeline
    if (root / "recovery_manifest.json").exists():
        if config.task_type != "humaneval" or config.prompt_version not in (
            "humaneval-reference-v4", "humaneval-reference-v5"
        ):
            raise ValueError("recovery manifest requires HumanEval v4 protocol or v5 protocol")
        return recovery_pipeline_type(root)
    if config.prompt_version == "gpqa-revision-v1":
        return RevisionPipeline
    if config.prompt_version == "gpqa-repair-v1":
        return RepairPipeline
    return Pipeline


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    root = args.root.absolute()
    if args.worker:
        sys.path.insert(0, str(root / "controller" / "code"))

    from dag_builder.client import APIClient, load_key
    from dag_builder.config import Config
    from dag_builder.pipeline import implementation, now
    from dag_builder.report import overview, render
    from dag_builder.storage import (
        digest,
        private_dir,
        read_json,
        run_lock,
        write_bytes_once,
        write_once,
    )

    private_dir(root)
    controller = private_dir(root / "controller")
    if not args.worker:
        # A running/frozen experiment must not accidentally be relaunched.
        with run_lock(root / "launcher"):
            if (root / "launch.json").exists():
                raise RuntimeError(
                    "pilot already launched; inspect before explicitly resuming"
                )
            config = Config.load(args.config)
            pipeline_type(root, config)
            load_key(config.key_env, args.key_file)
            items = read_json(root / "items.json")
            assert items and all(
                item.get("task_type") == config.task_type for item in items
            )
            code = implementation()
            import dag_builder

            package = Path(dag_builder.__file__).parent
            for name in code["source_files"]:
                write_bytes_once(
                    controller / "code" / "dag_builder" / name,
                    (package / name).read_bytes(),
                )
            write_once(controller / "code" / "snapshot_origin.json", code)
            write_bytes_once(controller / "runner.py", Path(__file__).read_bytes())
            write_once(root / "config.json", config.to_dict())
            write_once(root / "code_origin.json", code)
            render(root)
            command = [
                sys.executable,
                str(controller / "runner.py"),
                "--worker",
                "--root",
                str(root),
                "--config",
                str(root / "config.json"),
                "--key-file",
                str(args.key_file.absolute()),
            ]
            with (
                (root / "worker.stdout.log").open("x") as out,
                (root / "worker.stderr.log").open("x") as err,
            ):
                child = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            write_once(
                root / "launch.json",
                {
                    "launched_at": now(),
                    "pid": child.pid,
                    "command": command,
                    "host": "local",
                    "code_sha256": code["code_sha256"],
                },
            )
            # Keep the Mac awake only while this worker is alive, not indefinitely.
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(child.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            print(
                json.dumps({"pid": child.pid, "root": str(root), "status": "launched"})
            )
        return

    try:
        with run_lock(controller):
            original = read_json(root / "code_origin.json")
            assert implementation()["code_sha256"] == original["code_sha256"]
            write_once(
                root / "worker-start.json", {"pid": os.getpid(), "started_at": now()}
            )
            config = Config.load(args.config)
            client = APIClient(config, load_key(config.key_env, args.key_file))

            def progress(row):
                print(json.dumps(dict(row, recorded_at=now())), flush=True)
                report = render(root)
                summary = overview(root)
                snapshot = {
                    "recorded_at": now(),
                    "summary": summary,
                    "report": str(report),
                }
                write_once(root / "progress" / (digest(snapshot) + ".json"), snapshot)

            pipeline_class = pipeline_type(root, config)
            result = pipeline_class(root, config, client, resilient=True).run(
                progress=progress
            )
            report = render(root)
            write_once(
                root / "completion.json",
                {
                    "ended_at": now(),
                    "status": "paused" if result["paused"] else "processed",
                    "global_stop": result["global_stop"],
                    "summary": overview(root),
                    "report": str(report),
                    "human_released": 0,
                },
            )
            print(
                json.dumps(
                    {
                        "status": "paused" if result["paused"] else "processed",
                        "report": str(report),
                    }
                ),
                flush=True,
            )
    except Exception as error:  # noqa: BLE001 -- redact at the detached-process boundary
        # Never print raw provider errors, request headers or source content.
        write_once(
            root / "stops" / (str(os.getpid()) + ".json"),
            {
                "stopped_at": now(),
                "exception_type": type(error).__name__,
                "note": "Local/integrity error; no blind restart. Inspect preserved records.",
            },
        )
        print("Worker stopped safely; inspect private stop record.", flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
