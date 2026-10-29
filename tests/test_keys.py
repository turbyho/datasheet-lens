import os
import stat
import sys

import pytest

from datasheet_lens import keys
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError


class FakeRing:
    """A key store in memory, as the module keyring gives it."""

    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


def test_key_in_the_file_when_there_is_no_key_store(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("abc-123\n"))
    assert main(["key", "mouser"]) == 0
    path = keys._file()
    assert f"saved in {path}" in capsys.readouterr().out
    assert keys.get_key("mouser") == "abc-123"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # The environment variable comes first.
    monkeypatch.setenv(keys.KEYS["mouser"], "from-env")
    assert keys.get_key("mouser") == "from-env"
    assert main(["key"]) == 0
    assert "mouser: environment variable DATASHEET_LENS_MOUSER_KEY" in capsys.readouterr().out
    monkeypatch.delenv(keys.KEYS["mouser"])
    assert main(["key", "mouser", "--delete"]) == 0
    assert keys.get_key("mouser") == ""


def test_key_in_the_key_store(monkeypatch, capsys):
    ring = FakeRing()
    monkeypatch.setattr(keys, "_keyring", lambda: ring)
    keys._write_file({"mouser": "old"})  # a key from a time with no key store
    assert keys.set_key("mouser", " k1 ") == "the key store of the system"
    assert ring.data == {("datasheet-lens", "mouser"): "k1"} and keys._read_file() == {}
    assert keys.get_key("mouser") == "k1"
    assert keys.key_sources() == {"mouser": "the key store of the system"}
    keys.delete_key("mouser")
    assert keys.get_key("mouser") == "" and keys.key_sources() == {"mouser": "not set"}


def test_key_errors():
    with pytest.raises(LensError, match="unknown key"):
        keys.set_key("digikey", "x")
    with pytest.raises(LensError, match="empty"):
        keys.set_key("mouser", "  ")
    with pytest.raises(LensError, match="character"):
        keys.set_key("mouser", 'a"b')


def test_key_file_problems(capsys, monkeypatch):
    """A control character from a paste is not a key. A file that cannot be read
    gives no key, and the listing names it. --delete needs a name and tells about
    the environment variable."""
    with pytest.raises(LensError, match="character"):
        keys.set_key("mouser", "\x1b[200~abc")
    keys._file().parent.mkdir(parents=True, exist_ok=True)
    keys._file().write_text("mouser = \n", encoding="utf-8")
    assert keys.get_key("mouser") == ""
    assert "cannot read the keys" in keys.key_sources()["mouser"]
    with pytest.raises(LensError, match="correct or remove the file"):
        keys.set_key("mouser", "abc")
    keys._file().unlink()
    assert main(["key", "--delete"]) == 2
    monkeypatch.setenv(keys.KEYS["mouser"], "from-env")
    assert main(["key", "mouser", "--delete"]) == 0
    assert "a key is still in environment variable DATASHEET_LENS_MOUSER_KEY" in capsys.readouterr().out
