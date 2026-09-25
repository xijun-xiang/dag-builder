"""Convert a frozen accepted cohort to the common PALS DAG JSONL format."""

import argparse
import json

from dag_builder.unified import convert_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--benchmark", choices=("gpqa_diamond", "humaneval", "livecodebench_v6", "mmlu"), required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output-dir", help="omit to validate without writing")
    args = parser.parse_args()
    result = convert_file(args.source, args.benchmark, args.expected_sha256, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
