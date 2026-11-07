"""A lock of the folder of a user project between processes and threads.

The CLI and the MCP server can work on one project at the same time. A sync
moves, copies and deletes the files of the folder and writes the project file,
and a dry run of sync reads and hashes the files: without a lock, a dry run can
read a file that a sync of a different process moved (FileNotFoundError), or
compare an old project file with a new folder.

A command takes the lock before it reads the project file (project.open_project)
and holds it until the folder and the project file are done. The order of the
locks, always the same, so that no two calls wait for each other:

1. this lock (the folder of the project),
2. the lock of the MCP server (_add_lock),
3. the store (a connection to its database).

A call does not take the lock when it holds 2, or a write transaction of 3: a
different process can then wait for the store while this call waits for the
folder. An open connection with no write is no problem.

The commands take the lock first (open_project). The functions that write the
project file or change the folder also take it (locked): thus no write is
without the lock, also from a caller that forgets it. For such a function the
lock is held already, and it takes it again.

The lock is a file in the store folder (store_home()/locks/), not in the
project: the folder of the user gets no file of its own. Its name is the hash
of the path of the project folder. Thus the processes of one store (one person
on one machine: the CLI and the MCP server) exclude each other. The project
file itself is not the lock: save writes a new file in place of the old one
(write_atomic), and a lock of the old file then stops to work.

The lock is exclusive: fcntl.flock on POSIX, msvcrt.locking on Windows. Each
thread opens its own file: the threads of the MCP server wait for each other
as two processes do. A thread that holds the lock can take it again.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import LensError

# The time that a call waits for the lock, or None: no limit. A holder can work
# for a long time (a first sync with many copies, an inbox with many files that
# sync must read to find their parts). The CLI waits with no limit (the user
# can stop it), and tells it one time (on_wait). The MCP server sets a limit:
# a tool call that waits longer than its client would do its change when nobody
# reads the result.
TIMEOUT: float | None = None
INTERVAL = 0.05
WAIT_NOTE = 1.0  # seconds before on_wait
on_wait = None  # a function (root) that tells the user that the call waits
# The errors of a lock that a different holder has: EWOULDBLOCK/EAGAIN of flock,
# EACCES/EDEADLK of msvcrt.locking. Each other error (for example no lock support
# on a network file system) is an error of the call.
BUSY = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EDEADLK}
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
    except OSError as e:
        if e.errno in BUSY:
            return False
        raise LensError(f"the lock of the project folder does not work ({e}): the store folder must be on a "
                        "file system with file locks") from None
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
    """Hold the lock of a project folder. A different process, or a different
    call of the MCP server, that holds it makes the call wait (TIMEOUT: then a
    LensError). See the order of the locks above."""
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
        start, told = time.monotonic(), False
        while not _try_lock(fd):
            waited = time.monotonic() - start
            if TIMEOUT is not None and waited >= TIMEOUT:
                raise LensError(f"an other datasheet-lens command (a different process, or a different call of the "
                                f"MCP server) works on the project {root} for more than {TIMEOUT:.0f} s (lock "
                                f"{path}): call the tool again later")
            if not told and on_wait is not None and waited >= WAIT_NOTE:
                on_wait(root)
                told = True
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
    """Run a function of a project (the first argument is the Project) with the
    lock of its folder: a function that writes the project file or reads or
    changes the folder. A command holds the lock already (open_project)."""
    import functools

    @functools.wraps(function)
    def run(project, *args, **kwargs):
        with folder_lock(project.root):
            return function(project, *args, **kwargs)

    return run
