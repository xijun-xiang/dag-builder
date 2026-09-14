"""Launch a prepared full-coverage campaign from an immutable local snapshot."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--worker", action="store_true")
    args = p.parse_args()
    root = args.root.absolute()
    if args.worker:
        sys.path.insert(0, str(root / "controller/code"))
    from dag_builder.campaign import run_campaign
    from dag_builder.campaign_export import export_campaign
    from dag_builder.client import load_key
    from dag_builder.pipeline import implementation, now
    from dag_builder.storage import read_json, run_lock, write_bytes_once, write_once

    if args.worker:
        try:
            if (
                implementation()["code_sha256"]
                != read_json(root / "controller/snapshot.json")["code_sha256"]
            ):
                raise ValueError("code snapshot mismatch")
            write_once(
                root / "worker-start.json", {"pid": os.getpid(), "started_at": now()}
            )
            print(json.dumps(run_campaign(root, args.key_file)), flush=True)
        except Exception as error:  # noqa: BLE001 — outer worker boundary redacts sensitive errors
            # Do not print exception messages that can include source or secrets.
            write_once(
                root / "worker-failure.json",
                {"exception_type": type(error).__name__, "time": now()},
            )
            export_campaign(root, "interrupted")
            raise SystemExit(1)
        return
    with run_lock(root / "launcher"):
        if (root / "launch.json").exists():
            raise RuntimeError("already launched; inspect before resuming")
        load_key("JUDGE_API_KEY", args.key_file)
        import dag_builder

        package = Path(dag_builder.__file__).parent
        meta = implementation()
        for name in meta["source_files"]:
            write_bytes_once(
                root / "controller/code/dag_builder" / name,
                (package / name).read_bytes(),
            )
        write_once(root / "controller/snapshot.json", meta)
        write_bytes_once(root / "controller/runner.py", Path(__file__).read_bytes())
        command = [
            sys.executable,
            str(root / "controller/runner.py"),
            "--worker",
            "--root",
            str(root),
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
                "pid": child.pid,
                "started_at": now(),
                "command": command,
                "code_sha256": meta["code_sha256"],
            },
        )
        if sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(child.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(json.dumps({"pid": child.pid, "root": str(root), "status": "launched"}))


if __name__ == "__main__":
    main()
