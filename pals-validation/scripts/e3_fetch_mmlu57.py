"""Download only the 57 pinned public MMLU test shards, once per file."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from urllib.request import urlopen

from pals_validation.e3.greedy_data import MMLU_REVISION, SUBJECTS, adapt_subject
from pals_validation.io import read, save, sha256


def main():
    import pyarrow.parquet as pq
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    def fetch(subject):
        folder = args.output / subject
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / "test-00000-of-00001.parquet"
        receipt = folder / "download.json"
        url = f"https://huggingface.co/datasets/cais/mmlu/resolve/{MMLU_REVISION}/{subject}/test-00000-of-00001.parquet"
        if path.exists():
            old = read(receipt)
            if old["url"] != url or old["sha256"] != sha256(path):
                raise ValueError("Unverified existing download: " + subject)
            return subject, old
        with urlopen(url, timeout=60) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024 or not payload.startswith(b"PAR1"):
            raise ValueError("Invalid/oversized source: " + subject)
        with path.open("xb") as stream:
            stream.write(payload)
        rows = pq.read_table(path).to_pylist()
        adapt_subject(rows, subject, MMLU_REVISION)
        record = {"path": str(path.resolve()), "sha256": sha256(path), "count": len(rows), "url": url}
        save(receipt, record)
        print(subject, len(rows), flush=True)
        return subject, record
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = dict(pool.map(fetch, SUBJECTS))
    save(args.output / "manifest.json", {"dataset": "cais/mmlu", "revision": MMLU_REVISION,
         "split": "test", "subjects": records})


if __name__ == "__main__":
    main()
