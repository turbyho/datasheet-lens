"""Atomic file writes into the store."""

import os
import tempfile
from pathlib import Path


def write_atomic(target: Path, data: bytes) -> None:
    """Write data to a unique temporary file in the target directory, then replace
    the target. Two processes that write the same file do not mix their data."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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
