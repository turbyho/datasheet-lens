"""The lock of a project folder between processes and threads (lock.py)."""

import subprocess
import sys
import threading
import time

import pytest

from datasheet_lens import lock
from datasheet_lens import project as prj
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store

HOLD = """
import sys, time
from pathlib import Path
from datasheet_lens.lock import folder_lock
with folder_lock(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_a_thread_takes_its_lock_again(store_home, tmp_path):
    """A removal does a dry run of sync: the thread that holds the lock takes
    it again."""
    with lock.folder_lock(tmp_path), lock.folder_lock(tmp_path):
        pass
    project = prj.init_project(tmp_path / "board")
    with Store() as store, lock.folder_lock(project.root):
        prj.dry_run(project, store)  # dry_run takes the lock again


def test_a_different_holder_makes_the_call_wait(store_home, tmp_path, monkeypatch):
    """A different thread holds the lock: the call waits until it ends, and
    after TIMEOUT it gives an error that tells the reason."""
    project = prj.init_project(tmp_path / "board")
    taken, release, order = threading.Event(), threading.Event(), []

    def hold():
        with lock.folder_lock(project.root):
            taken.set()
            release.wait(5)
            order.append("holder")

    holder = threading.Thread(target=hold)
    holder.start()
    taken.wait(5)
    monkeypatch.setattr(lock, "TIMEOUT", 0.2)
    with Store() as store, pytest.raises(LensError, match="a different datasheet-lens process works on the project"):
        prj.dry_run(project, store)
    monkeypatch.setattr(lock, "TIMEOUT", 5.0)
    threading.Timer(0.2, release.set).start()
    with Store() as store:
        prj.sync(project, store)  # waits for the holder
    order.append("sync")
    holder.join()
    assert order == ["holder", "sync"]


def test_a_different_process_makes_the_call_wait(store_home, tmp_path):
    """The CLI and the MCP server are two processes: a sync waits for the lock
    of the other process."""
    project = prj.init_project(tmp_path / "board")
    other = subprocess.Popen([sys.executable, "-c", HOLD, str(project.root), "0.6"], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        start = time.monotonic()
        with Store() as store:
            prj.sync(project, store)
        assert time.monotonic() - start >= 0.3
    finally:
        other.wait(10)
    assert lock.lock_path(project.root).parent == store_home / "locks"
