"""The files that the setup wizard of the team server makes (no server is necessary)."""

import pytest

from datasheet_lens.errors import LensError
from datasheet_lens.team import setup
from datasheet_lens.team.config import parse_env


def test_domain_check():
    assert setup.check_domain(" DSLens.Example.com ") == "dslens.example.com"
    for wrong in ("localhost", "a b.com", "x.com;rm", "x.com/y", "-x.com", ""):
        with pytest.raises(LensError):
            setup.check_domain(wrong)


def test_env_file_round_trip():
    values = {"DSLENS_DB_URL": "postgresql://dslens:a-b_c@localhost:5432/dslens", "DSLENS_S3_SECRET": "x/y+z="}
    assert parse_env(setup.env_text(values)) == values


def test_garage_configuration():
    text = setup.garage_toml("ab" * 32, "token")
    assert 'rpc_secret = "' + "ab" * 32 + '"' in text and 'admin_token = "token"' in text
    for line in ("metadata_fsync = true", "data_fsync = true", 'db_engine = "sqlite"', "replication_factor = 1",
                 'api_bind_addr = "127.0.0.1:3900"', 'rpc_bind_addr = "127.0.0.1:3901"'):
        assert line in text
    assert "0.0.0.0" not in text and "[::]" not in text  # only the loopback


def test_units():
    server = setup.server_unit(8100)
    assert "EnvironmentFile=/var/lib/dslens-server/server.env" in server
    assert "ExecStart=/opt/dslens-server/venv/bin/dslens-server run --port 8100" in server
    assert "User=dslens" in server and "ProtectSystem=strict" in server
    garage = setup.garage_unit()
    assert "ReadWritePaths=/var/lib/garage" in garage and "User=garage" in garage


def test_nginx_site():
    text = setup.nginx_site("dslens.example.com", "s3.dslens.example.com", 8100)
    assert "server_name dslens.example.com s3.dslens.example.com;" in text  # port 80, one ACME root
    assert text.count("ssl_certificate     /etc/letsencrypt/live/dslens.example.com/fullchain.pem;") == 2
    s3 = text[text.index("server_name s3.dslens.example.com;"):]
    # The Host header is part of the signature of a presigned URL; the files stream.
    for line in ("proxy_set_header Host $host;", "client_max_body_size 0;", "proxy_request_buffering off;"):
        assert line in s3
    assert "server 127.0.0.1:8100;" in text and "server 127.0.0.1:3900;" in text
