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
tree has already. A process with no package metadata (a source tree in
PYTHONPATH) gets no check: it has no installed version to compare.
"""

from __future__ import annotations

import http.client
import importlib.metadata
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from packaging.version import InvalidVersion, Version

from . import __version__
from .config import store_home
from .errors import LensError
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

T = TypeVar("T")


class RequestError(Exception):
    """The request to PyPI failed: no network, an HTTP error, a connection that
    stopped in the answer, or an answer with no valid version. A fact about the
    network, not a defect: it goes to the debug log only."""


def installed_version() -> str | None:
    """The version in the package metadata on disk now (it follows an upgrade
    while the process runs), or None when the package is not installed."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def runs_installed_code() -> bool:
    """True when the metadata on sys.path belongs to the code that runs: its
    package folder is the folder of this module. A source tree in PYTHONPATH
    can run in front of an installed package: then the metadata tells about a
    different version, and the check does not apply."""
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return False
    try:
        return os.path.samefile(str(distribution.locate_file(__package__)), Path(__file__).parent)
    # The folder of the metadata does not exist (an editable install), the code is in
    # a zip file, or the distribution has no files on disk (NotImplementedError).
    except (OSError, NotImplementedError):
        return False


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
    if (installed is None or _same_version(installed, running) or is_editable_install() is not False
            or not runs_installed_code()):
        return None
    return (f"The installed datasheet-lens is {installed}, but this MCP server runs {running}, the version "
            "of the start of the LLM client. Tell the user to restart the LLM client (Claude Code or the "
            "client in use): the MCP server is a child process of the client.")


def is_editable_install() -> bool | None:
    """True for an editable install (PEP 610). None when the install type is not
    known (no check): the package has no metadata, or direct_url.json is not
    UTF-8 or not JSON. The function reads the metadata at each call: the user
    can change the install while the server runs."""
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except (importlib.metadata.PackageNotFoundError, UnicodeDecodeError):
        return None
    if text is None:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def check_enabled() -> bool:
    """True when the user lets datasheet-lens ask PyPI, and the install is from
    an index (not editable, with metadata, and its code runs)."""
    from .config import load_config

    if os.environ.get(DISABLE_ENV, "") not in ("", "0"):
        return False
    if load_config().get(CONFIG_KEY, True) is False:
        return False
    return is_editable_install() is False and runs_installed_code()


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
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
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
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as e:  # it holds the open answer
        e.close()
        raise RequestError(f"PyPI gave HTTP {e.code}") from e
    # No network, a stop in the answer, a proxy variable that is not a URL (ValueError).
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise RequestError(f"no answer from PyPI: {e}") from e
    try:
        latest = str(json.loads(body.decode("utf-8"))["info"]["version"])
        Version(latest)
    # Not UTF-8 or JSON, too deep for the parser, no "info", InvalidVersion.
    except (ValueError, RecursionError, KeyError, TypeError) as e:
        raise RequestError(f"PyPI gave no valid version: {e}") from e
    # An error of the write is a defect of this machine, not of the network.
    write_atomic(state_path(), json.dumps({"checked_at": current.isoformat(), "latest": latest}).encode("utf-8"))


def _refresh_in_background(force: bool = False) -> None:
    """A failed request is a fact about the network: it goes to the debug log,
    never into a tool answer. A different error is a defect: the log and a note
    tell it."""
    try:
        refresh_latest_version(force=force)
    except RequestError:
        log.debug("update check failed", exc_info=True)
    except Exception as error:  # noqa: BLE001 - a defect: the log and a note tell it
        _record("request", error)


def _start(force: bool) -> threading.Thread | None:
    if not check_enabled():
        return None
    thread = threading.Thread(target=_refresh_in_background, args=(force,), daemon=True,
                              name="datasheet-lens-update-check")
    thread.start()
    return thread


def start_update_check(force: bool = False) -> threading.Thread | None:
    """Start the background request to PyPI when the check applies. The thread
    never keeps the process alive (daemon). load_config gives {} for a
    configuration that cannot be read: then the check applies. A defect does not
    stop the server or the CLI command: the log and a note tell it."""
    return _checked("start", _start, force)


def newer_release() -> tuple[str, str] | None:
    """(newest, installed) when PyPI has a newer release, from the state file
    only."""
    if not check_enabled():
        return None
    installed, state = installed_version(), read_state()
    if installed is None or state is None:
        return None
    try:
        newer = Version(state[1]) > Version(installed)
    except InvalidVersion:  # installed metadata with no valid version
        return None
    return (state[1], installed) if newer else None


# The texts of the notes that this MCP server process gave already. A new text,
# for example of a newer release, gives a new note.
_told: set[str] = set()
# Check name -> the text of its first defect. A later defect of the check gives no
# note: a text that changes at each call gives one note only.
_defects: dict[str, str] = {}
# The MCP server runs the tools in more threads, and the request in one more.
_lock = threading.Lock()
# The subject of the defect note of each check.
_SUBJECTS = {"data": "The check of the data folder of datasheet-lens"}


def _record(check: str, error: Exception) -> None:
    """Keep the first defect of a check for its note and log it as a warning. A
    later defect of the check goes to the debug log: a text that changes at each
    call does not fill the log."""
    with _lock:
        first = check not in _defects
        if first:
            _defects[check] = f"{type(error).__name__}: {error}"
    if first:
        log.warning("version check %s failed", check, exc_info=error)
    else:
        log.debug("version check %s failed again", check, exc_info=error)


def _checked(check: str, function: Callable[..., T], *args: object) -> T | None:
    """The result of function(*args), or None after a defect (an exception that is
    not an expected condition of the check). The log and the note of
    _defect_notes tell the defect. Each check has its own guard: a defect of one
    check does not stop the other checks."""
    try:
        return function(*args)
    except Exception as error:  # noqa: BLE001 - a defect: the log and a note tell it
        _record(check, error)
        return None


def _defect_notes(result: str, tail: str = "") -> list[str]:
    """One note for each different first defect of the checks. Two checks with the
    same defect (for example of check_enabled) give one note."""
    with _lock:
        defects = list(_defects.items())
    notes = {}
    for check, text in defects:
        subject = _SUBJECTS.get(check, "The version check of datasheet-lens")
        notes.setdefault(text, f"{subject} failed ({text}). The result of the {result} is complete.{tail}")
    return list(notes.values())


def _data_note() -> str | None:
    from .config import data_note

    try:
        return data_note()
    except LensError:  # an error of config.json: the tool itself gives it
        return None


def version_notes() -> list[str]:
    """The lines for the LLM: a restart of the client, a newer release, or a
    database in the store directory that the data folder of config.json hides.
    Each note one time for each server process: the agent tells the user one
    time. A defect of a check gives one note, after the log entry: the result of
    the tool is complete."""
    notes = []
    reason = _checked("restart", restart_reason)
    if reason:
        notes.append(reason)
    note = _checked("data", _data_note)
    if note:
        notes.append(f"{note} Tell the user one time in this session.")
    found = _checked("release", newer_release)
    if found:
        notes.append(f"datasheet-lens {found[0]} is available on PyPI, and this machine has {found[1]}. "
                     "Tell the user one time in this session. Do not upgrade it yourself: the user "
                     "upgrades it with the tool that installed it (for example pipx upgrade "
                     "datasheet-lens), then restarts the LLM client.")
    notes += _defect_notes("tool", " Tell the user one time in this session.")
    with _lock:
        new = [n for n in notes if n not in _told]
        _told.update(new)
    return new


def user_notes() -> list[str]:
    """The lines for a person at the CLI (stdout): a newer release, and how to
    upgrade."""
    found = _checked("release", newer_release)
    if not found:
        return []
    return [f"datasheet-lens {found[0]} is available on PyPI (this machine has {found[1]}). Upgrade it with the "
            "tool that installed it, for example: pipx upgrade datasheet-lens. Then restart the LLM client."]


def user_defects() -> list[str]:
    """The lines for a person at the CLI (stderr, after user_notes): each defect of
    the check. The command is complete."""
    return [f"datasheet-lens: warning: {text}" for text in _defect_notes("command")]
