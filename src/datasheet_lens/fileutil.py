"""Atomic file writes (store files, and the files of a user project)."""

import os
import stat
import tempfile
from pathlib import Path


def write_atomic(target: Path, data: bytes) -> None:
    """Write data to a unique temporary file in the target directory, then replace
    the target. Two processes that write the same file do not mix their data.
    A symbolic link stays: the file that it points to gets the data. An existing
    file keeps its permissions; a new file gets 0644 (os.umask cannot be read
    without a change, and the MCP server runs more threads)."""
    target = Path(os.path.realpath(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def temp_path(target: Path) -> Path:
    """A unique temporary path in the target directory (for a tool that writes the
    file itself). The caller replaces the target with it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".part")
    os.close(fd)
    return Path(tmp)
