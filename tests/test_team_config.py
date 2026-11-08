"""The settings of the team server (no server is necessary)."""

import sys

import pytest

from datasheet_lens.errors import LensError
from datasheet_lens.team import config


# The team server runs only on Linux with systemd: the setup wizard refuses an other
# system. On Windows, Path gives Windows paths, and chmod sets only the read-only bit.
LINUX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the team server runs only on Linux")

ENV = {"DSLENS_DB_URL": "postgresql://dslens:pw@localhost:5432/dslens", "DSLENS_S3_ENDPOINT": "http://127.0.0.1:3900",
       "DSLENS_S3_PUBLIC_URL": "https://s3.example.com", "DSLENS_S3_KEY_ID": "GK1", "DSLENS_S3_SECRET": "secret"}


def test_load():
    settings = config.load(ENV)
    assert settings.s3_bucket == "dslens" and settings.max_blob_size == 2 << 30 and settings.url_expiry == 3600
    assert config.load({**ENV, "DSLENS_URL_EXPIRY": "60"}).url_expiry == 60


def test_missing_and_wrong_values():
    with pytest.raises(LensError, match="DSLENS_S3_SECRET"):
        config.load({k: v for k, v in ENV.items() if k != "DSLENS_S3_SECRET"})
    with pytest.raises(LensError, match="DSLENS_MAX_BLOB_SIZE must be an integer"):
        config.load({**ENV, "DSLENS_MAX_BLOB_SIZE": "2G"})


def test_env_file(tmp_path, monkeypatch):
    path = tmp_path / "server.env"
    path.write_text("# settings\n" + "\n".join(f'{k}="{v}"' for k, v in ENV.items()) + "\nDSLENS_S3_BUCKET=files\n")
    assert config.read_env_file(path)["DSLENS_S3_BUCKET"] == "files"
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "ENV_FILE", path)
    assert config.load_for_admin().s3_bucket == "files"


@LINUX_ONLY
def test_env_file_that_cannot_be_read(tmp_path, monkeypatch):
    path = tmp_path / "server.env"
    path.write_text("\n".join(f'{k}="{v}"' for k, v in ENV.items()) + "\n", encoding="utf-8")
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "ENV_FILE", path)
    path.chmod(0)
    try:
        with pytest.raises(LensError, match="sudo"):
            config.load_for_admin()
    finally:
        path.chmod(0o600)
