"""A lock of the folder of a user project between processes and threads.

The CLI and the MCP server can work on one project at the same time. A sync
moves, copies and deletes the files of the folder, and a dry run of sync reads
and hashes them: without a lock, a dry run can read a file that a sync of a
different process moved (FileNotFoundError), or give counts of a state between
two changes.

The lock is a file in the store folder (store_home()/locks/), not in the
project: the folder of the user gets no file of its own. Its name is the hash
of the path of the project folder. The project file itself is not the lock:
save writes a new file in place of the old one (write_atomic), and a lock of
the old file then stops to work.

The lock is exclusive: fcntl.flock on POSIX, msvcrt.locking on Windows. Each
thread opens its own file: the threads of the MCP server wait for each other
as two processes do. A thread that holds the lock can take it again (a removal
does a dry run of sync).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import LensError

# The time that a call waits for a different process: a sync that builds the
# index of many new documents holds the lock for a long time.
TIMEOUT = 600.0
INTERVAL = 0.05
_held = threading.local()  # {lock path: [file descriptor, count]} of this thread


def lock_path(root: Path) -> Path:
    """The lock file of a project folder."""
    from .config import store_home

    key = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:24]
    return store_home() / "locks" / f"{key}.lock"


def _try_lock(fd: int) -> bool:
    """Take the lock of an open file with no wait. False when a different
    holder has it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)  # msvcrt.locking locks from the position of the file
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # EWOULDBLOCK, EACCES: a different holder has the lock
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def folder_lock(root: Path) -> Iterator[None]:
    """Hold the lock of a project folder. A different process or thread that
    holds it makes the call wait, up to TIMEOUT seconds; then a LensError."""
    path = lock_path(root)
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = {}
    if path in held:  # this thread holds it: take it again
        held[path][1] += 1
        try:
            yield
        finally:
            held[path][1] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + TIMEOUT
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LensError(f"a different datasheet-lens process works on the project {root} for more than "
                                f"{TIMEOUT:.0f} s (lock {path}): wait until it ends, then run the command again")
            time.sleep(INTERVAL)
        held[path] = [fd, 1]
        try:
            yield
        finally:
            del held[path]
            _unlock(fd)
    finally:
        os.close(fd)


def locked(function):
    """Run a function of a project (the first argument is the Project) with
    the lock of its folder."""
    import functools

    @functools.wraps(function)
    def run(project, *args, **kwargs):
        with folder_lock(project.root):
            return function(project, *args, **kwargs)

    return run
