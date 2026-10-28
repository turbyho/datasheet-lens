from datasheet_lens import __version__
from datasheet_lens.cli import main
from datasheet_lens.config import store_home


def test_info_shows_version_and_store(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path))
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert str(tmp_path) in out


def test_store_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("DATASHEET_LENS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert store_home() == tmp_path / ".datasheet-lens"
