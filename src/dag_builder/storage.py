"""Private, immutable artifacts. One coordinator holds the run lock."""

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path


def encoded(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def private_dir(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError("symlinked artifact path is not allowed")
    if not path.exists():
        missing, cursor = [], path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(
            "artifact directory must be private and owned by this user"
        )
    return path


def write_bytes_once(path, payload):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("symlinked artifact file is not allowed")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError("refusing to overwrite an existing artifact")
        return
    fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise
    finally:
        Path(temporary).unlink()


def write_once(path, value):
    write_bytes_once(path, encoded(value))


@contextlib.contextmanager
def run_lock(root):
    root = private_dir(root)
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
