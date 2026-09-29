"""Recompute the completed fixed first batch in each E3 benchmark."""

import argparse
import json

from pals_validation.e3.audit import audit_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(audit_run(args.run, "canary", args.out), sort_keys=True))


if __name__ == "__main__":
    main()
