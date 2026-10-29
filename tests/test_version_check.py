import io
import json
from datetime import UTC, datetime, timedelta

from datasheet_lens import version_check as vc
from datasheet_lens.cli import main
from datasheet_lens.setup import save_config


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def enable(monkeypatch, installed="0.1.0"):
    """The check applies: no stop in the environment, an install from an index."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)
    monkeypatch.setattr(vc, "is_editable_install", lambda: False)
    monkeypatch.setattr(vc, "installed_version", lambda: installed)
    monkeypatch.setattr(vc, "_told", set())


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
