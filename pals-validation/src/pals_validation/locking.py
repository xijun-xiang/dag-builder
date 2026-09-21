"""Process-lifetime locks. The inode is persistent; ownership is not.

Requires POSIX flock on the shared filesystem. Never unlink an active/old lock:
unlinking allows two processes to lock different inodes for the same path.
"""
import fcntl
import json
import os
import socket
from contextlib import contextmanager


@contextmanager
def exclusive_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Active owner holds lock: {path}") from error
        owner = {"pid": os.getpid(), "host": socket.gethostname(),
                 "slurm_job_id": os.environ.get("SLURM_JOB_ID")}
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(owner) + "\n").encode())
        os.fsync(fd)
        yield
    finally:
        # close also releases ownership after SIGTERM/SIGKILL; file stays put.
        os.close(fd)
