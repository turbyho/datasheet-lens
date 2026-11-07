"""The lock of a project folder between processes and threads (lock.py)."""

import errno
import subprocess
import sys
import threading
import time

import pytest

from datasheet_lens import lock
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError

HOLD = """
import sys, time
from pathlib import Path
from datasheet_lens.lock import folder_lock
with folder_lock(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_a_thread_takes_its_lock_again(store_home, tmp_path):
    """A thread that holds the lock can take it again."""
    project = prj.init_project(tmp_path / "board")
    with lock.folder_lock(project.root), prj.open_project(project.root) as loaded:
        assert loaded.root == project.root


def test_the_project_file_is_read_in_the_lock(store_home, tmp_path):
    """open_project reads the project file after the holder is done: a change
    of the holder is in the project that the block gets."""
    project = prj.init_project(tmp_path / "board")
    taken, got = threading.Event(), []

    def hold():
        with prj.open_project(project.root) as held:
            taken.set()
            time.sleep(0.3)
            prj.set_distributors(held, ["mouser"])

    holder = threading.Thread(target=hold)
    holder.start()
    taken.wait(5)
    with prj.open_project(project.root) as loaded:  # waits for the holder
        got.append(loaded.distributors)
    holder.join()
    assert got == [["mouser"]]


def test_a_different_holder_makes_the_command_wait(store_home, tmp_path, monkeypatch, capsys):
    """A different holder: the command waits until it ends, and after TIMEOUT
    it gives an error that tells the reason."""
    project = prj.init_project(tmp_path / "board")
    taken, release = threading.Event(), threading.Event()

    def hold():
        with lock.folder_lock(project.root):
            taken.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    taken.wait(5)
    monkeypatch.setattr(lock, "TIMEOUT", 0.2)
    assert main(["ls", "-p", str(project.root)]) == 2
    assert "an other datasheet-lens command (a different process, or a different call of the MCP server)" in \
        capsys.readouterr().err
    # The CLI waits with no limit, and tells it one time.
    monkeypatch.setattr(lock, "TIMEOUT", None)
    monkeypatch.setattr(lock, "WAIT_NOTE", 0.05)
    threading.Timer(0.3, release.set).start()
    start = time.monotonic()
    assert main(["sync", "-p", str(project.root)]) == 0  # waits for the holder
    assert time.monotonic() - start >= 0.2
    assert capsys.readouterr().err.count("works on the project") == 1
    holder.join()


def test_a_different_process_makes_the_command_wait(store_home, tmp_path):
    """The CLI and the MCP server are two processes: a sync waits for the lock
    of the other process."""
    project = prj.init_project(tmp_path / "board")
    other = subprocess.Popen([sys.executable, "-c", HOLD, str(project.root), "0.6"], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        start = time.monotonic()
        assert main(["sync", "-p", str(project.root)]) == 0
        assert time.monotonic() - start >= 0.3
    finally:
        other.wait(10)
    assert lock.lock_path(project.root).parent == store_home / "locks"


def test_a_lock_error_is_not_a_busy_lock(store_home, tmp_path, monkeypatch):
    """Only the errors of a busy lock make the call wait. An other error (no
    lock support on the file system) is an error at once."""
    fcntl = pytest.importorskip("fcntl")  # POSIX

    def unsupported(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    with pytest.raises(LensError, match="the lock of the project folder does not work"):
        with lock.folder_lock(tmp_path):
            pass


def test_one_index_build_at_a_time(store_home, text_pdf, monkeypatch):
    """Two threads that build the index of one document (project_sync and
    get_index of the MCP server): one builds it, the other finds it."""
    from datasheet_lens import indexer
    from datasheet_lens.store import Store

    with Store() as store:
        doc = store.add_document(text_pdf("a.pdf", [[(50, 60, 12, "1 Features")]]))
    calls, read = [], indexer._read_pages

    def slow(path):
        calls.append(path)
        time.sleep(0.2)
        return read(path)

    monkeypatch.setattr(indexer, "_read_pages", slow)
    results = []

    def build():
        with Store() as store:
            results.append(indexer.build_index(store, doc.id)["skipped"])

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1 and sorted(results) == [False, True]
