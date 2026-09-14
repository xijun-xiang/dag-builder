"""Run offline tests and save a reproducible acceptance record (not experiment results)."""

import argparse
import hashlib
import io
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dag_builder.pipeline import implementation
from dag_builder.storage import digest, private_dir, write_bytes_once, write_once


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    record = {
        "kind": "offline_engineering_tests_not_model_results",
        "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "command": [
            sys.executable,
            str(Path(__file__).resolve()),
            "--output",
            str(args.output),
        ],
        "implementation": implementation(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful(),
        "test_files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "tests").glob("test_*.py"))
        },
    }
    target = private_dir(args.output) / digest(record)[:16]
    write_once(target / "acceptance.json", record)
    write_bytes_once(target / "tests.txt", stream.getvalue().encode())
    print(
        f"tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} skipped={len(result.skipped)}"
    )
    print(target)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
