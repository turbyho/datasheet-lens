"""Atomic file writes (store files, and the files of a user project)."""

import errno
import os
import secrets
import stat
import tempfile
from pathlib import Path

# The flags of a new temporary file: as tempfile.mkstemp (O_BINARY: no CRLF on Windows).
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def new_temp(target: Path, mode: int = 0o666) -> tuple[int, Path]:
    """Make a new empty file with a unique name in the folder of the target. Give its
    open file descriptor and its path.

    The system gives the file the mode with no bit of the umask (also a default ACL of
    the folder). With the default 0o666, that is the mode of each new file of the
    user. tempfile.mkstemp gives 0o600, and os.umask cannot be read without a change
    (the MCP server runs more threads).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(tempfile.TMP_MAX):
        tmp = target.parent / f"{target.name}.{secrets.token_hex(8)}.part"
        try:
            return os.open(tmp, _TEMP_FLAGS, mode), tmp
        except FileExistsError:
            continue
        except PermissionError:
            # The rule of tempfile.mkstemp: on Windows, a file that waits for its delete
            # gives PermissionError. Try the next name when the folder is writable.
            if os.name == "nt" and target.parent.is_dir() and os.access(target.parent, os.W_OK):
                continue
            raise
    raise FileExistsError(errno.EEXIST, "no unique name for a temporary file", str(target.parent))


def write_atomic(target: Path, data: bytes) -> None:
    """Write data to a unique temporary file in the target directory, then replace
    the target. Two processes that write the same file do not mix their data.
    A symbolic link stays: the file that it points to gets the data. An existing
    file keeps its mode bits, but not its owner, its group or an ACL entry that the
    folder does not give (the target is a new file). A new file gets the mode of
    each new file of the user (see new_temp)."""
    target = Path(os.path.realpath(target))
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None
    fd, tmp = new_temp(target)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def temp_path(target: Path) -> Path:
    """A unique temporary path in the target directory (for a tool that writes the
    file itself). The caller replaces the target with it. The file has the mode of
    each new file of the user (see new_temp)."""
    fd, tmp = new_temp(target)
    os.close(fd)
    return tmp
