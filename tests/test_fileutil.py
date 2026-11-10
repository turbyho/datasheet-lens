import os
import stat
import sys

import pytest

from datasheet_lens.fileutil import temp_path, write_atomic


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no umask and no mode bits for the group")
@pytest.mark.parametrize("umask", [0o022, 0o002, 0o077])
def test_new_files_get_the_mode_of_the_umask(tmp_path, umask):
    """A new file gets 0o666 with no bit of the umask, as each new file of the user.
    An existing file keeps its mode."""
    old = os.umask(umask)
    try:
        write_atomic(tmp_path / "new.toml", b"a")
        tmp = temp_path(tmp_path / "copy.pdf")  # for example the copy of a document in a project
        existing = tmp_path / "existing.md"
        existing.write_bytes(b"a")
        os.chmod(existing, 0o640)
        write_atomic(existing, b"b")
    finally:
        os.umask(old)
    wanted = 0o666 & ~umask
    assert stat.S_IMODE((tmp_path / "new.toml").stat().st_mode) == wanted
    assert stat.S_IMODE(tmp.stat().st_mode) == wanted
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640 and existing.read_bytes() == b"b"
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".part") == [tmp.name]


def test_write_atomic_keeps_the_bytes(tmp_path):
    """The data is written as bytes: no CRLF on Windows."""
    write_atomic(tmp_path / "a.md", b"x\ny\n")
    assert (tmp_path / "a.md").read_bytes() == b"x\ny\n"
