"""Fetch and audit pinned public t2ance LCB candidates; never execute code."""

import argparse
import json
import os
from pathlib import Path

from dag_builder.t2ance_source import FILES, fetch_file, prepare
from verify_livecodebench_reference import static_check


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cache", "source-file", "calibri-selection", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    if args.download:
        for name in FILES:
            fetch_file(args.cache, name)
    result = prepare(args.cache, args.source_file, args.calibri_selection,
                     args.output, static_check)
    print(json.dumps({"selected": result["text_bearing_candidates"],
                      "excluded": len(result["excluded"]),
                      "matched_rows": result["matched_upstream_rows"]}, ensure_ascii=False))
