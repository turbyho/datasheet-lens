import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from datasheet_lens import version_check as vc
from datasheet_lens.cli import main
from datasheet_lens.setup import save_config


REAL_RUNS = vc.runs_installed_code


@pytest.fixture(autouse=True)
def no_notes(monkeypatch):
    """Each test starts with no notes and no defects of an earlier test."""
    monkeypatch.setattr(vc, "_told", set())
    monkeypatch.setattr(vc, "_defects", {})


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def enable(monkeypatch, installed="0.1.0"):
    """The check applies: no stop in the environment, an install from an index."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "runs_installed_code", lambda: True)
    monkeypatch.setattr(vc, "installed_version", lambda: installed)
    monkeypatch.setattr(vc, "_told", set())
    monkeypatch.setattr(vc, "_defects", {})


def pypi(monkeypatch, version="0.2.0"):
    """A PyPI that gives version. The list gets the URL of each request."""
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        return Answer(json.dumps({"info": {"version": version}}).encode())

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    return calls


def test_newer_release_from_pypi(store_home, monkeypatch):
    enable(monkeypatch)
    calls = pypi(monkeypatch)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    vc.refresh_latest_version(now)
    vc.refresh_latest_version(now + timedelta(hours=1))  # younger than the interval: no request
    assert calls == [vc.PYPI_URL]
    assert vc.newer_release() == ("0.2.0", "0.1.0")
    notes = vc.version_notes()
    assert any("datasheet-lens 0.2.0 is available on PyPI" in n and "Do not upgrade it yourself" in n
               for n in notes)
    assert vc.version_notes() == []  # one time for each server process
    state = vc.state_path().read_text()
    vc.state_path().write_text(state.replace("0.2.0", "0.3.0"))
    assert any("datasheet-lens 0.3.0 is available" in n for n in vc.version_notes())  # a new text
    vc.state_path().write_text(state)
    vc.refresh_latest_version(now + timedelta(hours=25))
    assert len(calls) == 2


def test_no_check_when_stopped(store_home, monkeypatch):
    enable(monkeypatch)
    vc.state_path().parent.mkdir(parents=True, exist_ok=True)
    vc.state_path().write_text(json.dumps({"checked_at": datetime.now(UTC).isoformat(), "latest": "9.0"}))
    assert vc.newer_release() == ("9.0", "0.1.0")
    monkeypatch.setenv(vc.DISABLE_ENV, "1")
    assert vc.newer_release() is None and vc.start_update_check() is None
    monkeypatch.delenv(vc.DISABLE_ENV)
    save_config({vc.CONFIG_KEY: False})
    assert vc.newer_release() is None
    save_config({})
    monkeypatch.setattr(vc, "is_editable_install", lambda: True)  # an editable install: no check
    assert vc.newer_release() is None
    vc.state_path().write_text("{broken")
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    assert vc.newer_release() is None


def test_no_check_with_no_metadata(store_home, monkeypatch):
    """A source tree in PYTHONPATH: no installed version, no request."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "DISTRIBUTION", "datasheet-lens-not-installed")
    calls = pypi(monkeypatch)
    assert vc.is_editable_install() is None
    assert not vc.check_enabled() and vc.start_update_check() is None
    assert calls == []


def test_install_type_read_at_each_call(monkeypatch):
    """The user can change the install while the server runs: no cache. A
    direct_url.json that is not UTF-8 gives an unknown install type."""
    answers = iter(['{"dir_info": {"editable": true}}', None, UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])

    class Distribution:
        def read_text(self, name):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert vc.is_editable_install() is True
    assert vc.is_editable_install() is False
    assert vc.is_editable_install() is None


def test_a_defect_in_the_thread_is_not_hidden(store_home, monkeypatch, caplog):
    """Only the errors of a request go to the debug log. A defect gives a note,
    and one log entry."""
    enable(monkeypatch, installed=vc.__version__)

    def refresh(force=False):
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "refresh_latest_version", refresh)
    vc._refresh_in_background()
    vc._refresh_in_background()
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert vc.version_notes() == [
        "The version check of datasheet-lens failed (RuntimeError: defect). The result of the tool is "
        "complete. Tell the user one time in this session."
    ]


@pytest.mark.parametrize("body", [
    json.dumps({"info": {"version": "not a version"}}).encode(),
    json.dumps({"info": {"version": None}}).encode(),
    b"\xff", b"<html>", b"[]", b'{"info": []}', b"{}", b"[" * 100000,
])
def test_an_answer_with_no_version_is_a_request_error(store_home, monkeypatch, caplog, body):
    enable(monkeypatch)
    monkeypatch.setattr(vc.urllib.request, "urlopen", lambda request, timeout: Answer(body))
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()
    vc._refresh_in_background()  # the debug log only
    assert not vc.state_path().exists()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_an_http_error_is_closed(store_home, monkeypatch):
    enable(monkeypatch)
    closed = []

    class Error(vc.urllib.error.HTTPError):
        def close(self):
            closed.append(True)
            super().close()

    def urlopen(request, timeout):
        raise Error(vc.PYPI_URL, 503, "unavailable", {}, None)

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()
    assert closed


def test_a_write_error_is_a_defect(store_home, monkeypatch):
    """The state file cannot be written: a defect of this machine, not of the network."""
    enable(monkeypatch, installed=vc.__version__)
    pypi(monkeypatch)

    def write(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(vc, "write_atomic", write)
    vc._refresh_in_background()
    (note,) = vc.version_notes()
    assert "failed (OSError: [Errno 28] No space left on device)" in note


def test_later_defects_go_to_the_debug_log(monkeypatch, caplog):
    """One note and one warning for each check. A later defect of the check, also
    with a different text, goes to the debug log."""
    enable(monkeypatch)
    caplog.set_level("DEBUG", logger=vc.log.name)
    for text in ("a", "b", "a"):
        vc._record("release", RuntimeError(text))
    assert [r.levelname for r in caplog.records] == ["WARNING", "DEBUG", "DEBUG"]
    assert vc._defects == {"release": "RuntimeError: a"}


def test_a_defect_of_the_data_check_names_the_data_folder(store_home, monkeypatch):
    enable(monkeypatch, installed=vc.__version__)

    def note():
        raise RuntimeError("defect")

    monkeypatch.setattr(vc, "_data_note", note)
    (text,) = vc.version_notes()
    assert text.startswith("The check of the data folder of datasheet-lens failed (RuntimeError: defect)")


def test_a_deep_state_file_cannot_be_read(store_home, monkeypatch):
    enable(monkeypatch)
    vc.state_path().parent.mkdir(parents=True, exist_ok=True)
    vc.state_path().write_text("[" * 100000)
    assert vc.read_state() is None


def test_a_proxy_that_is_not_a_url_is_a_request_error(store_home, monkeypatch):
    enable(monkeypatch)

    def urlopen(request, timeout):
        raise ValueError("proxy URL with no authority: 'http://'")

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    with pytest.raises(vc.RequestError):
        vc.refresh_latest_version()


def test_a_source_tree_in_front_of_an_install(monkeypatch):
    """The metadata of an installed package that does not run: no check."""
    enable(monkeypatch)
    monkeypatch.setattr(vc, "runs_installed_code", REAL_RUNS)
    site = "/elsewhere/site-packages"

    class Distribution:
        def locate_file(self, path):
            return f"{site}/{path}"

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert not vc.runs_installed_code()
    assert not vc.check_enabled()
    assert vc.restart_reason("0.0.1") is None
    site = str(Path(vc.__file__).resolve().parent.parent)  # the metadata of the code that runs
    assert vc.runs_installed_code()


def test_a_stopped_response_is_a_request_error(store_home, monkeypatch):
    """IncompleteRead is not an OSError: it is an error of the network too."""
    enable(monkeypatch)

    def urlopen(request, timeout):
        raise vc.http.client.IncompleteRead(b"{")

    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    vc._refresh_in_background()  # the debug log only
    assert not vc.state_path().exists()


def test_a_defect_of_one_check_keeps_the_other_notes(store_home, monkeypatch):
    """Each check has its own guard. A defect gives one note, also when its text
    is different at each call."""
    enable(monkeypatch, installed=vc.__version__)  # no restart note
    count = iter(range(100))

    def reason():
        raise RuntimeError(f"defect {next(count)}")

    monkeypatch.setattr(vc, "restart_reason", reason)
    monkeypatch.setattr(vc, "newer_release", lambda: ("9.0", "0.1.0"))
    notes = vc.version_notes()
    assert len(notes) == 2
    assert "datasheet-lens 9.0 is available on PyPI" in notes[0]
    assert "The version check of datasheet-lens failed (RuntimeError: defect 0)" in notes[1]
    assert vc.version_notes() == []  # one time for each server process


def test_a_defect_does_not_stop_the_start(store_home, tmp_path, monkeypatch, capsys):
    """The server and the CLI start also when the check has a defect."""
    enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["project", "init"]) == 0

    def enabled():
        raise RuntimeError("no home directory")

    monkeypatch.setattr(vc, "check_enabled", enabled)
    assert vc.start_update_check() is None
    assert vc.user_notes() == []
    assert vc.user_defects() == [
        "datasheet-lens: warning: The version check of datasheet-lens failed (RuntimeError: no home "
        "directory). The result of the command is complete."
    ]
    capsys.readouterr()
    assert main(["ls"]) == 0  # the command runs to its end; the defect goes to stderr
    out, err = capsys.readouterr()
    assert "version check" not in out
    assert err.startswith("datasheet-lens: warning: The version check of datasheet-lens failed")


def test_restart_note(monkeypatch):
    enable(monkeypatch, installed="0.2.0rc1")
    assert "restart the LLM client" in vc.restart_reason("0.1.0")
    assert vc.restart_reason("0.2.0-rc1") is None  # the same version in a different spelling
    monkeypatch.setattr(vc, "is_editable_install", lambda: True)  # the metadata of an editable install is old
    assert vc.restart_reason("0.1.0") is None
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "installed_version", lambda: None)
    assert vc.restart_reason("0.1.0") is None


def test_info_asks_pypi_each_time(store_home, monkeypatch, capsys):
    """info ignores the interval, and gives the note for a person: the upgrade command."""
    enable(monkeypatch)
    calls = pypi(monkeypatch, "0.3.0")
    vc.state_path().parent.mkdir(parents=True, exist_ok=True)
    vc.state_path().write_text(json.dumps({"checked_at": datetime.now(UTC).isoformat(), "latest": "0.1.0"}))
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert calls == [vc.PYPI_URL]
    assert "0.3.0 is available on PyPI" in out and "pipx upgrade datasheet-lens" in out
    assert "Tell the user" not in out


def test_versions_agree():
    """The version is the same in each file that has it (the release checks it)."""
    import tomllib
    from pathlib import Path

    from datasheet_lens import __version__

    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == version
    for name in ("server.json", "glama.json"):
        path = root / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["version"] == version, name
            for package in data.get("packages", []):
                assert package["version"] == version, name


def test_a_distribution_with_no_files_on_disk(monkeypatch):
    class Distribution:
        def locate_file(self, path):
            raise NotImplementedError

    monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: Distribution())
    assert not vc.runs_installed_code()
