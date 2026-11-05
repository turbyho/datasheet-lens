"""The team server: accounts, tokens and files, against a real PostgreSQL and a
real S3 object store (Garage). These tests run only with the environment
variables of TEST_ENV; else pytest skips them.

DSLENS_TEST_PG          postgresql://user@host:port (no database: each test run makes one)
DSLENS_TEST_S3          the endpoint of the object store (http://127.0.0.1:3900)
DSLENS_TEST_S3_KEY_ID   an access key with read, write and owner on the bucket
DSLENS_TEST_S3_SECRET
DSLENS_TEST_S3_BUCKET   default dslens-test
"""

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

TEST_ENV = ("DSLENS_TEST_PG", "DSLENS_TEST_S3", "DSLENS_TEST_S3_KEY_ID", "DSLENS_TEST_S3_SECRET")
pytestmark = pytest.mark.skipif(not all(os.environ.get(v) for v in TEST_ENV),
                                reason="no test database and object store (see the module docstring)")

if all(os.environ.get(v) for v in TEST_ENV):
    import asyncpg
    import httpx
    from fastapi.testclient import TestClient

    from datasheet_lens.team import admin, auth
    from datasheet_lens.team.app import create_app
    from datasheet_lens.team.blobs import BlobStore, blob_key, staging_key
    from datasheet_lens.team.config import Settings
    from datasheet_lens.team.db import Database

MiB = 1 << 20


@pytest.fixture(scope="module")
def settings():
    base = os.environ["DSLENS_TEST_PG"].rstrip("/")
    name = f"dslens_test_{uuid.uuid4().hex[:8]}"

    async def admin_sql(sql):
        conn = await asyncpg.connect(f"{base}/postgres")
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin_sql(f"CREATE DATABASE {name}"))
    endpoint = os.environ["DSLENS_TEST_S3"]
    yield Settings(db_url=f"{base}/{name}", s3_endpoint=endpoint, s3_public_url=endpoint,
                   s3_key_id=os.environ["DSLENS_TEST_S3_KEY_ID"], s3_secret=os.environ["DSLENS_TEST_S3_SECRET"],
                   s3_bucket=os.environ.get("DSLENS_TEST_S3_BUCKET", "dslens-test"), max_blob_size=100 * MiB)
    asyncio.run(admin_sql(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture
def client(settings):
    auth._failures.clear()
    # An error of the server is an answer 500, as for a real client.
    with TestClient(create_app(settings, cleanup=False), raise_server_exceptions=False) as c:
        yield c


def _admin(settings, monkeypatch, capsys, *argv) -> str:
    """Run dslens-admin with the settings of the test. Give its output."""
    monkeypatch.setattr(admin, "load_for_admin", lambda: settings)
    capsys.readouterr()
    assert admin.main(list(argv)) == 0
    return capsys.readouterr().out


def _account(settings, monkeypatch, capsys, role="contributor") -> dict:
    name = f"user-{uuid.uuid4().hex[:6]}"
    out = _admin(settings, monkeypatch, capsys, "account", "add", name, "-r", role)
    token = re.search(r"dsl_[0-9a-f]{64}", out).group(0)
    return {"name": name, "headers": {"Authorization": f"Bearer {token}"}, "token": token}


def _file(size: int) -> tuple[bytes, dict]:
    data = os.urandom(size)
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "size": size, "media_type": "application/pdf"}


def test_authentication(client, settings, monkeypatch, capsys):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/v1/account").status_code == 401
    assert client.get("/v1/account", headers={"Authorization": "Bearer dsl_wrong"}).status_code == 401
    user = _account(settings, monkeypatch, capsys, role="reviewer")
    assert client.get("/v1/account", headers=user["headers"]).json()["role"] == "reviewer"


def test_rate_limit_of_failed_tokens(client):
    for _ in range(auth.MAX_FAILURES):
        assert client.get("/v1/account", headers={"Authorization": "Bearer dsl_wrong"}).status_code == 401
    assert client.get("/v1/account", headers={"Authorization": "Bearer dsl_wrong"}).status_code == 429


def test_revoked_expired_and_disabled(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    out = _admin(settings, monkeypatch, capsys, "token", "ls", user["name"])
    token_id = out.split()[0]
    _admin(settings, monkeypatch, capsys, "token", "rm", token_id)
    assert client.get("/v1/account", headers=user["headers"]).status_code == 401

    user = _account(settings, monkeypatch, capsys)
    _admin(settings, monkeypatch, capsys, "account", "set", user["name"], "--active", "off")
    assert client.get("/v1/account", headers=user["headers"]).status_code == 401
    _admin(settings, monkeypatch, capsys, "account", "set", user["name"], "--active", "on")
    assert client.get("/v1/account", headers=user["headers"]).status_code == 200

    out = _admin(settings, monkeypatch, capsys, "token", "add", user["name"], "--days", "0")
    expired = re.search(r"dsl_[0-9a-f]{64}", out).group(0)
    assert client.get("/v1/account", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_reader_cannot_upload(client, settings, monkeypatch, capsys):
    reader = _account(settings, monkeypatch, capsys, role="reader")
    _, info = _file(1000)
    response = client.post("/v1/blobs", json=info, headers=reader["headers"])
    assert response.status_code == 403 and "contributor" in response.json()["detail"]


def test_upload_and_download(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(300_000)
    answer = client.post("/v1/blobs", json=info, headers=user["headers"]).json()
    assert answer["exists"] is False
    # Other bytes under the key: the object store rejects them (signed checksum),
    # also with a different size (signed size).
    assert httpx.put(answer["url"], content=os.urandom(len(data)), headers=answer["headers"]).status_code == 400
    assert httpx.put(answer["url"], content=os.urandom(1000), headers=answer["headers"]).status_code == 403
    assert client.post(f"/v1/blobs/{info['sha256']}/done", json=info, headers=user["headers"]).status_code == 404
    assert httpx.put(answer["url"], content=data, headers=answer["headers"]).status_code == 200
    assert client.post(f"/v1/blobs/{info['sha256']}/done", json=info, headers=user["headers"]).json() == {
        "stored": True}
    assert client.post("/v1/blobs", json=info, headers=user["headers"]).json() == {"exists": True}

    reader = _account(settings, monkeypatch, capsys, role="reader")
    download = client.get(f"/v1/blobs/{info['sha256']}", headers=reader["headers"]).json()
    assert download["size"] == info["size"] and httpx.get(download["url"]).content == data
    assert client.get(f"/v1/blobs/{'0' * 64}", headers=reader["headers"]).status_code == 404

    _admin(settings, monkeypatch, capsys, "blob", "rm", info["sha256"])
    assert client.get(f"/v1/blobs/{info['sha256']}", headers=reader["headers"]).status_code == 404
    assert BlobStore(settings).size(blob_key(info["sha256"])) is None


def test_file_that_the_store_has_with_no_row(client, settings, monkeypatch, capsys):
    """A client that did not tell done: the next request finds the file."""
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(2000)
    BlobStore(settings).s3.put_object(Bucket=settings.s3_bucket, Key=blob_key(info["sha256"]), Body=data)
    assert client.post("/v1/blobs", json=info, headers=user["headers"]).json() == {"exists": True}


def test_wrong_requests(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    _, info = _file(1000)
    assert client.post("/v1/blobs", json={**info, "sha256": "x" * 64}, headers=user["headers"]).status_code == 422
    assert client.post("/v1/blobs", json={**info, "media_type": "text/html"},
                       headers=user["headers"]).status_code == 422
    too_large = client.post("/v1/blobs", json={**info, "size": settings.max_blob_size + 1}, headers=user["headers"])
    assert too_large.status_code == 422 and "at most" in too_large.json()["detail"]
    body = b"{" + b" " * (1 << 20) + b"}"
    assert client.post("/v1/blobs", content=body, headers={**user["headers"], "content-type": "application/json"}
                       ).status_code == 413


def test_done_with_a_wrong_size_removes_the_file(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(5000)
    answer = client.post("/v1/blobs", json=info, headers=user["headers"]).json()
    assert httpx.put(answer["url"], content=data, headers=answer["headers"]).status_code == 200
    wrong = client.post(f"/v1/blobs/{info['sha256']}/done", json={**info, "size": 4999}, headers=user["headers"])
    assert wrong.status_code == 422
    assert BlobStore(settings).size(blob_key(info["sha256"])) is None


def _send_parts(client, upload, data, numbers, headers):
    part_size = upload["part_size"]
    chunks = {n: data[(n - 1) * part_size:n * part_size] for n in numbers}
    urls = client.post(f"/v1/uploads/{upload['upload_id']}/urls", headers=headers, json={
        "parts": [{"number": n, "sha256": hashlib.sha256(c).hexdigest()} for n, c in chunks.items()]}).json()
    for part in urls["parts"]:
        assert httpx.put(part["url"], content=chunks[part["number"]], headers=part["headers"],
                         timeout=60).status_code == 200


def test_multipart_upload_continues_after_a_stop(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(40 * MiB)
    upload = client.post("/v1/uploads", json=info, headers=user["headers"]).json()
    assert upload["parts"] == 3 and upload["part_size"] == 16 * MiB
    _send_parts(client, upload, data, [1, 2], user["headers"])
    state = client.get(f"/v1/uploads/{upload['upload_id']}", headers=user["headers"]).json()
    assert [p["number"] for p in state["uploaded"]] == [1, 2]
    missing = client.post(f"/v1/uploads/{upload['upload_id']}/complete", headers=user["headers"])
    assert missing.status_code == 422 and "2 of 3 parts" in missing.json()["detail"]

    other = _account(settings, monkeypatch, capsys)
    assert client.get(f"/v1/uploads/{upload['upload_id']}", headers=other["headers"]).status_code == 403

    _send_parts(client, upload, data, [3], user["headers"])
    assert client.post(f"/v1/uploads/{upload['upload_id']}/complete", headers=user["headers"]).json() == {
        "stored": True}
    download = client.get(f"/v1/blobs/{info['sha256']}", headers=user["headers"]).json()
    assert hashlib.sha256(httpx.get(download["url"], timeout=60).content).hexdigest() == info["sha256"]
    assert client.get(f"/v1/uploads/{upload['upload_id']}", headers=user["headers"]).status_code == 404
    assert BlobStore(settings).size(staging_key(upload["upload_id"])) is None


def test_multipart_upload_with_wrong_content(client, settings, monkeypatch, capsys):
    """Correct parts of a different file: the server finds it at the end."""
    user = _account(settings, monkeypatch, capsys)
    _, info = _file(6 * MiB)
    other, _ = _file(6 * MiB)
    upload = client.post("/v1/uploads", json=info, headers=user["headers"]).json()
    _send_parts(client, upload, other, [1], user["headers"])
    wrong = client.post(f"/v1/uploads/{upload['upload_id']}/complete", headers=user["headers"])
    assert wrong.status_code == 422 and "SHA-256" in wrong.json()["detail"]
    assert client.get(f"/v1/blobs/{info['sha256']}", headers=user["headers"]).status_code == 404
    assert client.get(f"/v1/uploads/{upload['upload_id']}", headers=user["headers"]).status_code == 404
    assert BlobStore(settings).size(staging_key(upload["upload_id"])) is None


def test_cleanup_of_uploads(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    _, info = _file(1000)
    stale = client.post("/v1/uploads", json=info, headers=user["headers"]).json()["upload_id"]
    store = BlobStore(settings)
    orphan = uuid.uuid4().hex
    orphan_s3 = store.start(orphan)

    async def age():
        conn = await asyncpg.connect(settings.db_url)
        await conn.execute("UPDATE uploads SET started = now() - interval '2 days' WHERE id = $1", stale)
        await conn.close()

    asyncio.run(age())
    _admin(settings, monkeypatch, capsys, "cleanup")
    assert client.get(f"/v1/uploads/{stale}", headers=user["headers"]).status_code == 404
    # An upload with no row that is new (the API starts it now): it stays.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert orphan in {k for k, _ in store.orphans(set(), later)}
    assert orphan not in {k for k, _ in store.orphans(set(), datetime.now(timezone.utc) - timedelta(hours=1))}
    store.abort(orphan, orphan_s3)
    assert orphan not in {k for k, _ in store.orphans(set(), later)}
    store.abort(orphan, orphan_s3)  # a second time: no error


def test_admin_lists(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys, role="admin")
    assert user["name"] in _admin(settings, monkeypatch, capsys, "account", "ls")
    assert "files" in _admin(settings, monkeypatch, capsys, "stats")
    monkeypatch.setattr(admin, "load_for_admin", lambda: settings)
    assert admin.main(["account", "add", user["name"]]) == 2  # exists
    assert admin.main(["token", "rm", "999999"]) == 2


def test_schema_is_idempotent(settings):
    async def twice():
        db = Database(settings.db_url)
        await db.connect()
        await db.init_schema()
        await db.init_schema()
        await db.close()

    asyncio.run(twice())


def test_size_is_signed(client, settings, monkeypatch, capsys):
    """The presigned URL signs the size: a file with the same SHA-256 and a
    different size cannot go into the store (no way past the size limit)."""
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(10_000)
    answer = client.post("/v1/blobs", json={**info, "size": 100}, headers=user["headers"]).json()
    assert httpx.put(answer["url"], content=data, headers=answer["headers"]).status_code == 403
    assert BlobStore(settings).size(blob_key(info["sha256"])) is None


def test_part_size_is_signed(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(20 * MiB)
    upload = client.post("/v1/uploads", json=info, headers=user["headers"]).json()
    last = data[16 * MiB:]
    urls = client.post(f"/v1/uploads/{upload['upload_id']}/urls", headers=user["headers"], json={
        "parts": [{"number": 2, "sha256": hashlib.sha256(last + b"x").hexdigest()}]}).json()
    part = urls["parts"][0]
    # The last part has 4 MiB: one more byte is a different signed size.
    assert httpx.put(part["url"], content=last + b"x", headers=part["headers"], timeout=60).status_code == 403
    client.delete(f"/v1/uploads/{upload['upload_id']}", headers=user["headers"])


def test_complete_again_after_a_failure(client, settings, monkeypatch, capsys):
    """The parts are joined, then the copy fails (network): the upload stays,
    its state tells completed, and a second complete stores the file."""
    user = _account(settings, monkeypatch, capsys)
    data, info = _file(6 * MiB)
    upload = client.post("/v1/uploads", json=info, headers=user["headers"]).json()
    _send_parts(client, upload, data, [1], user["headers"])
    store = client.app.state.blobs
    original = store.s3.copy_object

    def broken(**kwargs):
        raise ConnectionError("network")

    monkeypatch.setattr(store.s3, "copy_object", broken)
    assert client.post(f"/v1/uploads/{upload['upload_id']}/complete", headers=user["headers"]).status_code == 500
    state = client.get(f"/v1/uploads/{upload['upload_id']}", headers=user["headers"]).json()
    assert state["completed"] is True and state["uploaded"] == []
    monkeypatch.setattr(store.s3, "copy_object", original)
    assert client.post(f"/v1/uploads/{upload['upload_id']}/complete", headers=user["headers"]).json() == {
        "stored": True}
    assert BlobStore(settings).size(staging_key(upload["upload_id"])) is None


def test_chunked_body_needs_a_length(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    _, info = _file(1000)

    def chunks():
        yield json.dumps(info).encode()

    response = client.post("/v1/blobs", content=chunks(), headers={**user["headers"],
                                                                   "content-type": "application/json"})
    assert response.status_code == 411


def test_rate_limit_counts_only_the_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    auth._failures.clear()
    for _ in range(auth.MAX_FAILURES - 1):
        auth._failed("10.0.0.9")
    now[0] += auth.WINDOW - 1
    auth._failed("10.0.0.9")
    assert auth._limited("10.0.0.9")
    now[0] += 2  # only the last failure is in the window
    assert not auth._limited("10.0.0.9")


def test_sync_endpoints(client, settings, monkeypatch, capsys):
    user = _account(settings, monkeypatch, capsys)
    reader = _account(settings, monkeypatch, capsys, role="reader")
    gid = uuid.uuid4().hex
    change = {"seq": 7, "tbl": "parts", "op": "insert", "key": gid,
              "data": {"mpn": f"EP{gid[:6]}", "manufacturer": "", "family": "", "created": "2026-11-01", "base": None}}
    assert client.post("/v1/sync/push", json={"changes": [change]}, headers=user["headers"]).json() == {
        "results": [{"seq": 7, "result": "applied", "version": 1}]}
    assert client.post("/v1/sync/push", json={"changes": [change]}, headers=reader["headers"]
                       ).json()["results"][0]["result"] == "rejected"
    page = client.get("/v1/sync/pull", headers=reader["headers"]).json()
    assert gid in {r["key"] for r in page["rows"]} and isinstance(page["cursor"], int)
    later = client.get("/v1/sync/pull", params={"cursor": page["cursor"]}, headers=reader["headers"]).json()
    assert later["rows"] == []
    assert client.get("/v1/sync/pull", params={"cursor": "x"}, headers=reader["headers"]).status_code == 422
    assert client.post("/v1/sync/push", json={"changes": []}, headers=user["headers"]).status_code == 422
    assert "makers_version" in client.get("/v1/account", headers=user["headers"]).json()
