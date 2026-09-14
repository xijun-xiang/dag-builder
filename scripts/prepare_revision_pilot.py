"""Freeze a user-reviewed entry list; never select samples by new-model results."""

import argparse
import json
import os

from dag_builder.revision_source import prepare_revision
from dag_builder.storage import read_json


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--selection-spec", required=True)
    args = parser.parse_args()
    specification = read_json(args.selection_spec)
    selection = prepare_revision(
        args.root, specification["entries"], specification["selection_note"]
    )
    print(json.dumps({"selected": selection["selected_count"], "root": args.root}))


if __name__ == "__main__":
    main()
