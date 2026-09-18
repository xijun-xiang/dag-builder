"""Canonical hashes and exclusive, atomic output files."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def read_jsonl(path):
    # splitlines() also splits U+2028 inside a JSON string: iterate real lines.
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def save(path, value):
    """Publish a complete file without replacing any existing result."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)  # exclusive publication, even with concurrent writers
    finally:
        os.unlink(name)


def verify(folder, manifest):
    for name, expected in manifest.items():
        path = Path(folder) / name
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            raise ValueError(f"Integrity check failed: {name}")


def rank(seed, *keys):
    # Preserve the historical fixed-prefix selection hash exactly.
    return hashlib.sha256(json.dumps([seed, *keys], ensure_ascii=False).encode()).hexdigest()
