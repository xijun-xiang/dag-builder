"""Request that a private detached pilot finish in-flight calls and stop dispatching."""

import argparse
import os
from pathlib import Path

from dag_builder.pipeline import now
from dag_builder.storage import private_dir, read_json, write_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = private_dir(args.root.resolve())
    if not (root / "launch.json").exists() or (root / "completion.json").exists():
        raise ValueError("operator stop requires an active launched run")
    launch = read_json(root / "launch.json")
    if launch["host"] != "local":
        raise ValueError("operator stop is local only")
    write_once(root / "operator-stop-request.json", {
        "requested_at": now(), "reason": args.reason,
        "policy": "stop new paid calls; preserve and drain in-flight calls",
    })
    print("Stop requested; wait for worker completion and inspect the final record.")


if __name__ == "__main__":
    main()
