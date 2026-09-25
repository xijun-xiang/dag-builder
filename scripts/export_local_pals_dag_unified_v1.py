"""Export accepted local GSM8K/MMLU run roots to PALS unified v1."""

import argparse
import json

from dag_builder.unified import export_roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=("gsm8k", "mmlu"), required=True)
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = export_roots(args.root, args.benchmark, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
