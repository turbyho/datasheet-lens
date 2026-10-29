"""Tell the user about a newer release, and about a server that does not run the
installed code (the same method as fw-context-mcp).

- The server does not run the installed code. The MCP server reads __version__
  one time, when it starts. When the user installs a different version while
  the server runs, the server keeps the old code. Only a restart of the LLM
  client helps: the MCP server is a child process of the client. This check
  reads only the local package metadata.
- A newer release is on PyPI. One background thread for each server process
  gets the JSON document of the package from PyPI, and writes the newest
  version into a state file in the store folder. After a request that
  succeeded, no request goes out for CHECK_INTERVAL. A request that failed
  writes nothing, thus a machine with no network access tries again at the
  next start. The tools read only the state file, never the network: no
  answer waits for PyPI.

The network check is on by default. The key "update_check": false in the
global configuration, or a value in DISABLE_ENV, stops it. An editable install
(PEP 610 direct_url.json) gets no check: its metadata does not follow the
source tree, and a comparison with PyPI can report a release that the source
tree has already.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.metadata
import json
import logging
import os
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import __version__
from .config import store_home
from .fileutil import write_atomic

log = logging.getLogger(__name__)

DISTRIBUTION = "datasheet-lens"
PYPI_URL = "https://pypi.org/pypi/datasheet-lens/json"
# A value other than "" and "0" stops the network check (for CI and tests).
DISABLE_ENV = "DATASHEET_LENS_NO_UPDATE_CHECK"
CONFIG_KEY = "update_check"
# One request a day is enough for releases that come after days or weeks.
CHECK_INTERVAL = timedelta(hours=24)
TIMEOUT = 5.0
STATE_FILE = "update_check.json"


def installed_version() -> str | None:
    """The version in the package metadata on disk now (it follows an upgrade
    while the process runs), or None when the package is not installed."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def _same_version(a: str, b: str) -> bool:
    """True for one version in two spellings (0.2.0-rc1 and 0.2.0rc1)."""
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def restart_reason(running: str = __version__) -> str | None:
    """The text that tells the user to restart the LLM client, or None: the
    installed version is not the version that this process loaded. Not for an
    editable install: its metadata does not follow the source tree."""
    installed = installed_version()
    if installed is None or _same_version(installed, running) or is_editable_install() is not False:
        return None
    return (f"The installed datasheet-lens is {installed}, but this MCP server runs {running}, the version "
            "of the start of the LLM client. Tell the user to restart the LLM client (Claude Code or the "
            "client in use): the MCP server is a child process of the client.")


@functools.lru_cache(maxsize=1)
def is_editable_install() -> bool | None:
    """True for an editable install (PEP 610), None when direct_url.json is
    not JSON (the install type is not known: no check)."""
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return False
    if text is None:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def check_enabled() -> bool:
    """True when the user lets datasheet-lens ask PyPI, and the install is not
    editable."""
    from .setup import load_config

    if os.environ.get(DISABLE_ENV, "") not in ("", "0"):
        return False
    if load_config().get(CONFIG_KEY, True) is False:
        return False
    return is_editable_install() is False


def state_path() -> Path:
    return store_home() / STATE_FILE


def read_state() -> tuple[datetime, str] | None:
    """(time of the request, newest version) of the last request that
    succeeded, or None. A file that cannot be read gives None."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["checked_at"])
        latest = str(data["latest"])
        Version(latest)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (checked, latest) if checked.tzinfo is not None else None


def refresh_latest_version(now: datetime | None = None, force: bool = False) -> None:
    """Ask PyPI for the newest version and write it into the state file. No
    request when the stored result is younger than CHECK_INTERVAL, unless force
    is true (the CLI command info). The errors go to the caller."""
    current = now or datetime.now(UTC)
    state = read_state()
    # A time in the future (a clock that moved back) does not stop the check.
    if not force and state is not None and timedelta(0) <= current - state[0] < CHECK_INTERVAL:
        return
    request = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        latest = str(json.loads(response.read().decode("utf-8"))["info"]["version"])
    Version(latest)
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps({"checked_at": current.isoformat(), "latest": latest}).encode("utf-8"))


def _refresh_in_background(force: bool = False) -> None:
    """A failed check is a fact about the network: it goes to the debug log,
    never into a tool answer."""
    try:
        refresh_latest_version(force=force)
    except Exception:  # noqa: BLE001 - no network, HTTP error, bad document, store not writable
        log.debug("update check failed", exc_info=True)


def start_update_check(force: bool = False) -> threading.Thread | None:
    """Start the background request to PyPI when the check applies. The thread
    never keeps the process alive (daemon)."""
    with contextlib.suppress(Exception):  # a configuration that cannot be read: no check
        if not check_enabled():
            return None
        thread = threading.Thread(target=_refresh_in_background, args=(force,), daemon=True,
                                  name="datasheet-lens-update-check")
        thread.start()
        return thread
    return None


def newer_release() -> tuple[str, str] | None:
    """(newest, installed) when PyPI has a newer release, from the state file
    only."""
    with contextlib.suppress(Exception):
        if not check_enabled():
            return None
        installed, state = installed_version(), read_state()
        if installed is None or state is None:
            return None
        try:
            if Version(state[1]) <= Version(installed):
                return None
        except InvalidVersion:
            return None
        return state[1], installed
    return None


_told: set[str] = set()  # the notes that this MCP server process gave already


def version_notes() -> list[str]:
    """The lines for the LLM: a restart of the client, or a newer release. Each
    note one time for each server process: the agent tells the user one time."""
    notes = []
    with contextlib.suppress(Exception):
        reason = restart_reason()
        if reason:
            notes.append(reason)
    found = newer_release()
    if found:
        notes.append(f"datasheet-lens {found[0]} is available on PyPI, and this machine has {found[1]}. Tell "
                     "the user one time in this session. Do not upgrade it yourself: the user upgrades it with "
                     "the tool that installed it (for example pipx upgrade datasheet-lens), then restarts the "
                     "LLM client.")
    new = [n for n in notes if n not in _told]
    _told.update(new)
    return new


def user_notes() -> list[str]:
    """The lines for a person at the CLI: a newer release, and how to upgrade."""
    found = newer_release()
    if not found:
        return []
    return [f"datasheet-lens {found[0]} is available on PyPI (this machine has {found[1]}). Upgrade it with the "
            "tool that installed it, for example: pipx upgrade datasheet-lens. Then restart the LLM client."]
