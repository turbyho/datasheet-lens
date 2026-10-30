from pathlib import Path

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
    # Path.home() reads HOME on Linux and macOS, USERPROFILE on Windows.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert store_home() == tmp_path / ".datasheet-lens"


def test_data_folder(store_home, tmp_path, make_pdf, capsys):
    """The key "data" of config.json moves db/, blobs/ and tmp/ to a different
    folder. config.json stays in the store folder."""
    import json

    import pytest

    from datasheet_lens import version_check
    from datasheet_lens.config import config_path, data_home, data_lines
    from datasheet_lens.errors import LensError
    from datasheet_lens.setup import save_config
    from datasheet_lens.store import Store

    assert data_home() == store_home
    with Store() as store:  # a database in the store folder: the old location
        store.add_document(make_pdf())
    data = tmp_path / "raid" / "lens"
    save_config({"element_backend": "text", "data": str(data)})
    # A disk that is not mounted: no new empty store on the wrong disk.
    with pytest.raises(LensError, match="does not exist: mount its disk"):
        Store()
    assert main(["info"]) == 2 and not data.exists()
    data.mkdir(parents=True)
    assert data_home() == data
    with Store() as store:
        doc = store.add_document(make_pdf("other.pdf", pages=3))
        assert doc.path.is_relative_to(data / "blobs") and len(store.documents()) == 1
    assert (data / "db" / "lens.db").is_file() and config_path().parent == store_home
    capsys.readouterr()
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert f"data: {data}" in out and "not the database in" in out
    # The MCP server tells the agent one time.
    assert any("not the database in" in n for n in version_check.version_notes())
    assert not any("not the database in" in n for n in version_check.version_notes())
    # The same folder by a different path is not a different folder.
    save_config({"data": str(store_home / ".." / store_home.name)})
    assert data_lines() == []
    for value in ("relative/path", "", 5):
        config_path().write_text(json.dumps({"data": value}), encoding="utf-8")
        with pytest.raises(LensError, match='"data" must be'):
            data_home()
    config_path().write_text("{broken", encoding="utf-8")
    with pytest.raises(LensError, match="cannot read the configuration"):
        Store()
