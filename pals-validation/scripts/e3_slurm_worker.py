"""One Slurm task owns one E3 shard and only its allocated GPU."""

import json
import os

from pals_validation.e3.run import worker


def main():
    root = os.environ["PALS_RUN"]
    stage = os.environ["PALS_STAGE"]
    shard = int(os.environ["PALS_SHARD"])
    canary = os.environ.get("PALS_CANARY") == "1"
    result = worker(root, stage, shard, os.environ.get("PALS_SOURCE_RUN"), canary)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
