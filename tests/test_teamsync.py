"""The client of the team server (teamsync), against a real server: the API in a
thread (uvicorn), PostgreSQL and Garage. These tests run only with the
environment of test_team_server; else pytest skips them."""

import asyncio
import json
import os
import socket
import threading
import time
import uuid

import pytest

TEST_ENV = ("DSLENS_TEST_PG", "DSLENS_TEST_S3", "DSLENS_TEST_S3_KEY_ID", "DSLENS_TEST_S3_SECRET")
pytestmark = pytest.mark.skipif(not all(os.environ.get(v) for v in TEST_ENV),
                                reason="no test database and object store (see test_team_server)")

if all(os.environ.get(v) for v in TEST_ENV):
    import asyncpg
    import uvicorn

    from datasheet_lens import knowledge, teamsync
    from datasheet_lens.store import Store
    from datasheet_lens.team.app import create_app
    from datasheet_lens.team.config import Settings
    from datasheet_lens.team.db import Database


@pytest.fixture(scope="module")
def server():
    """A team server on a free port, with a new database and the accounts alice
    (contributor), rita (reviewer) and rudi (reader). Gives the URL and the tokens."""
    base = os.environ["DSLENS_TEST_PG"].rstrip("/")
    name = f"dslens_client_{uuid.uuid4().hex[:8]}"

    async def admin_sql(sql):
        conn = await asyncpg.connect(f"{base}/postgres")
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    async def accounts(url):
        db = Database(url)
        await db.connect()
        await db.init_schema()
        tokens = {}
        for account, role in (("alice", "contributor"), ("rita", "reviewer"), ("rudi", "reader")):
            await db.create_account(account, role)
            tokens[account] = (await db.create_token(account))[1]
        await db.close()
        return tokens

    asyncio.run(admin_sql(f"CREATE DATABASE {name}"))
    endpoint = os.environ["DSLENS_TEST_S3"]
    settings = Settings(db_url=f"{base}/{name}", s3_endpoint=endpoint, s3_public_url=endpoint,
                        s3_key_id=os.environ["DSLENS_TEST_S3_KEY_ID"], s3_secret=os.environ["DSLENS_TEST_S3_SECRET"],
                        s3_bucket=os.environ.get("DSLENS_TEST_S3_BUCKET", "dslens-test"))
    tokens = asyncio.run(accounts(settings.db_url))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(create_app(settings, cleanup=False), host="127.0.0.1", port=port,
                                       log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "tokens": tokens, "settings": settings}
    uv.should_exit = True
    thread.join(10)
    asyncio.run(admin_sql(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture
def client(server, tmp_path, monkeypatch):
    """open(name, account): a store of its own, connected as account."""
    def open_store(name, account):
        monkeypatch.setenv("DATASHEET_LENS_TEAM_TOKEN", server["tokens"][account])
        return Store(tmp_path / name)
    return open_store


def _as(server, monkeypatch, account):
    monkeypatch.setenv("DATASHEET_LENS_TEAM_TOKEN", server["tokens"][account])


def _parts(store):
    return {r["mpn"]: dict(r) for r in store.db.execute("SELECT * FROM parts")}


def test_two_clients(server, client, make_pdf, monkeypatch):
    """A store with data connects: its rows and files go to the server. A new
    store connects and gets them; a file comes when a tool needs it."""
    a = client("a", "alice")
    doc = a.add_document(make_pdf("tlv755p.pdf", pages=3))
    base = a.add_part(f"TLV755P-{uuid.uuid4().hex[:4]}", "TI")
    a.link(base, doc.id)
    knowledge.save_fact(a, doc.id, 2, "Vout", "3.3", unit="V", part=a.part(base)["mpn"])
    a.maker_relation("alias", "TXN", "Texas Instruments")
    lines = teamsync.connect(a, server["url"])
    assert any("local rows go to the server" in line for line in lines)
    assert teamsync.state(a)["waiting"] == 0

    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    mpn = a.part(base)["mpn"]
    assert mpn in _parts(b) and _parts(b)[mpn]["gid"] == a.part(base)["gid"]
    got = b.document(doc.sha256)
    assert got is not None and not got.path.exists()  # only the row
    assert b.local_file(got).read_bytes() == doc.path.read_bytes()
    assert b.db.execute("SELECT value FROM knowledge").fetchone()[0] == "3.3"
    assert b.db.execute("SELECT count(*) FROM maker_relations WHERE name = 'TXN'").fetchone()[0] == 1

    # A change of b goes to a: the status of the fact (b is a reviewer).
    fact = b.db.execute("SELECT id FROM knowledge").fetchone()[0]
    knowledge.set_status(b, fact, "checked")
    assert teamsync.sync(b).pushed == 1
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    assert a.db.execute("SELECT status, version FROM knowledge").fetchone()[:] == ("checked", 2)
    # No echo: a pull does not put the rows of the server into the outbox.
    assert teamsync.state(a)["waiting"] == 0 and teamsync.state(b)["waiting"] == 0


def test_conflict(server, client, monkeypatch):
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    part = a.add_part(f"OPA{uuid.uuid4().hex[:6]}", "TI")
    teamsync.sync(a)
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    mpn = a.part(part)["mpn"]
    b.set_category(b.find_part(mpn), "OpAmp")
    _as(server, monkeypatch, "alice")
    a.set_category(part, "Comparator")
    teamsync.sync(a)
    _as(server, monkeypatch, "rita")
    report = teamsync.sync(b)
    assert any("conflict" in n for n in report.notes)
    assert _parts(b)[mpn]["family"] == "Comparator"  # the version of the server is kept
    assert any("conflict" in line for line in teamsync.model_notes(b))
    assert teamsync.model_notes(b) == []  # a note goes to the model one time


def test_same_new_part_on_two_clients(server, client, make_pdf, monkeypatch):
    """Two persons add the same part offline: the server keeps the older one.
    The newer client gets its gid, with its links."""
    mpn = f"TPS{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    _as(server, monkeypatch, "alice")
    older = a.add_part(mpn, "TI")
    time.sleep(1.1)  # created has seconds
    _as(server, monkeypatch, "rita")
    doc = b.add_document(make_pdf("tps.pdf"))
    newer = b.add_part(mpn, "Texas Instruments")
    b.link(newer, doc.id)
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    _as(server, monkeypatch, "rita")
    report = teamsync.sync(b)
    assert any("is the part" in n for n in report.notes)
    rows = [r for r in b.db.execute("SELECT gid FROM parts WHERE mpn = ?", (mpn,))]
    assert [r[0] for r in rows] == [a.part(older)["gid"]]
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    assert [r["sha256"] for r in a.part_documents(mpn)] == [doc.sha256]


@pytest.mark.parametrize("maker, first", [
    ("TI", True),                   # the server part comes first; the same name: the unique MPN and manufacturer
    ("Texas Instruments", True),    # the server part comes first; an other name: merged_into
    ("Texas Instruments", False),   # the tombstone comes first
])
def test_older_part_of_an_other_client_takes_the_part(server, client, make_pdf, monkeypatch, maker, first):
    """a has a part on the server. b pushes an older part with the same MPN and
    manufacturer: the server keeps the older part and merges the part of a into
    it. a then has one part, with the gid of the older part and its link, in
    each order of the rows of the pull (by gid)."""
    mpn = f"OLD{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    doc = a.add_document(make_pdf("old.pdf"))
    newer = a.add_part(mpn, "TI")
    a.link(newer, doc.id)
    teamsync.sync(a)
    b = client("b", "rita")
    gid = ("00000000-0000-7000-8000-" if first else "ffffffff-ffff-7fff-bfff-") + uuid.uuid4().hex[:12]
    older = b.db.execute("INSERT INTO parts (mpn, manufacturer, created, gid) VALUES (?, ?,"
                         " '2000-01-01T00:00:00+00:00', ?)", (mpn, maker, gid)).lastrowid
    b.db.commit()
    _as(server, monkeypatch, "rita")
    teamsync.connect(b, server["url"])
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    rows = a.db.execute("SELECT gid FROM parts WHERE mpn = ?", (mpn,)).fetchall()
    assert [r[0] for r in rows] == [b.part(older)["gid"]]
    assert [r["sha256"] for r in a.part_documents(mpn)] == [doc.sha256]
    assert teamsync.state(a)["waiting"] == 0


def test_reader_gets_the_part_of_the_server(server, client, monkeypatch):
    """A reader makes a part that it cannot push; the server has the same part:
    the local row takes the gid of the server (no second part, no error)."""
    mpn = f"LM{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    a.add_part(mpn, "TI")
    teamsync.sync(a)
    r = client("r", "rudi")
    local = r.add_part(mpn, "Texas Instruments")
    report_lines = teamsync.connect(r, server["url"])
    assert report_lines
    assert [row["gid"] for row in r.db.execute("SELECT gid FROM parts WHERE mpn = ?", (mpn,))] == [
        _parts(a)[mpn]["gid"]]
    assert r.part(local)["gid"] == _parts(a)[mpn]["gid"]
    assert any("refused by the server" in n for n in teamsync.notes(r))  # the role reader


def test_relation_merges_on_the_other_client(server, client, monkeypatch):
    """Two names that the registry does not know: two parts on the server. A
    relation of one person merges them on the server and on each store, into
    the older part."""
    mpn = f"X{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    _as(server, monkeypatch, "alice")
    older = a.add_part(mpn, "Acme Corp")
    teamsync.sync(a)
    time.sleep(1.1)
    _as(server, monkeypatch, "rita")
    b.add_part(mpn + "-OTHER", "")  # a different part: not changed by the merge
    newer = b.db.execute("INSERT INTO parts (mpn, manufacturer, created) VALUES (?, 'ACME Electronics', ?)",
                         (mpn, "2099-01-01T00:00:00+00:00")).lastrowid
    b.db.commit()
    teamsync.sync(b)  # the registry of the server does not know the names: a second part
    assert sorted(r[0] for r in b.db.execute("SELECT manufacturer FROM parts WHERE mpn = ?", (mpn,))) == [
        "ACME Electronics", "Acme Corp"]
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    assert len(a.match_parts(mpn)) == 2
    assert a.maker_relation("alias", "ACME Electronics", "Acme Corp") == [f"{mpn}: ACME Electronics into Acme Corp"]
    teamsync.sync(a)
    _as(server, monkeypatch, "rita")
    teamsync.sync(b)
    for store in (a, b):
        rows = store.db.execute("SELECT gid FROM parts WHERE mpn = ?", (mpn,)).fetchall()
        assert [r[0] for r in rows] == [a.part(older)["gid"]], store.home
        assert teamsync.state(store)["waiting"] == 0
    assert b.part(newer) is None


def test_offline_and_resume_of_a_multipart_upload(server, client, make_pdf, monkeypatch):
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    path = make_pdf("big.pdf", pages=2)
    with open(path, "ab") as f:  # a valid PDF with more bytes (a PDF reader ignores data after %%EOF)
        f.write(os.urandom(20 << 20))
    doc = a.add_document(path)
    monkeypatch.setattr(teamsync, "SINGLE_PUT_MAX", 1 << 20)
    original = teamsync.Remote.transfer
    calls = []

    def flaky(method, url, headers, body=None, length=None, timeout=600):
        calls.append(method)
        if method == "PUT" and len([c for c in calls if c == "PUT"]) == 2:
            raise teamsync.Offline("no connection (test)")
        return original(method, url, headers, body, length, timeout)

    monkeypatch.setattr(teamsync.Remote, "transfer", staticmethod(flaky))
    with pytest.raises(teamsync.Offline):
        teamsync.sync(a)
    assert teamsync.state(a)["last_error"].startswith("no connection")
    assert any("last sync failed" in line for line in teamsync.model_notes(a))
    assert a.db.execute("SELECT count(*) FROM sync_uploads").fetchone()[0] == 1
    teamsync.sync(a)  # continues: part 1 is on the server
    assert calls.count("PUT") == 3  # part 1, the failed part 2, part 2 again
    assert teamsync.state(a)["waiting"] == 0 and teamsync.state(a)["last_error"] is None
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    assert b.local_file(b.document(doc.sha256)).read_bytes() == doc.path.read_bytes()


def test_no_server(server, tmp_path, monkeypatch):
    monkeypatch.setenv("DATASHEET_LENS_TEAM_TOKEN", server["tokens"]["alice"])
    store = Store(tmp_path / "x")
    teamsync.connect(store, server["url"])
    store.db.execute("UPDATE sync_state SET server = 'http://127.0.0.1:9'")
    store.db.commit()
    lines = teamsync.sync_before_answer(store)
    assert lines and "not asked" in lines[0]
    assert teamsync.disconnect(store) == 0 and not teamsync.connected(store)
    assert teamsync.model_notes(store) == []


def test_cli(server, tmp_path, monkeypatch, capsys):
    from datasheet_lens.cli import main

    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path / "cli"))
    monkeypatch.setenv("DATASHEET_LENS_TEAM_TOKEN", server["tokens"]["alice"])
    assert main(["server"]) == 0 and "not connected" in capsys.readouterr().out
    assert main(["server", "connect", server["url"]]) == 0
    assert "account alice" in capsys.readouterr().out
    assert main(["server", "sync"]) == 0 and "pushed 0 changes" in capsys.readouterr().out
    assert main(["server", "status"]) == 0 and "last complete sync" in capsys.readouterr().out
    assert main(["server", "disconnect"]) == 0


def test_two_local_changes_of_one_row(server, client, make_pdf, monkeypatch):
    """A new fact and a change of its status before a sync: the second change
    waits for the version of the first one (no conflict, no lost change)."""
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    doc = b.add_document(make_pdf("two.pdf"))
    fact = knowledge.save_fact(b, doc.id, 1, "Vref", "1.2", unit="V")
    knowledge.set_status(b, fact, "checked")
    part = b.add_part(f"TWO{uuid.uuid4().hex[:6]}", "TI")
    b.set_category(part, "LDO")
    b.set_category(part, "Regulator")
    report = teamsync.sync(b)
    assert not [n for n in report.notes if "conflict" in n], report.notes
    assert b.db.execute("SELECT status, version FROM knowledge WHERE id = ?", (fact,)).fetchone()[:] == (
        "checked", 2)
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    gid = b.db.execute("SELECT gid FROM knowledge WHERE id = ?", (fact,)).fetchone()[0]
    assert a.db.execute("SELECT status FROM knowledge WHERE gid = ?", (gid,)).fetchone()[0] == "checked"
    assert _parts(a)[b.part(part)["mpn"]]["family"] == "Regulator"


def test_waiting_row_one_attempt_in_each_sync(server, client, monkeypatch):
    """A change that refers to a row that the server does not have waits: one
    attempt in each sync, and the later changes of its row wait with it."""
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    gid = "00000000-0000-7000-8000-" + uuid.uuid4().hex[:12]
    data = {"mpn": "W1", "manufacturer": "", "family": "", "created": "2026-11-01", "base": "missing-part"}
    a.db.execute("INSERT INTO outbox (tbl, op, key, data) VALUES ('parts', 'insert', ?, ?)", (gid, json.dumps(data)))
    a.db.execute("INSERT INTO outbox (tbl, op, key, base_version, data) VALUES ('parts', 'update', ?, 0, ?)",
                 (gid, json.dumps({**data, "family": "x"})))
    a.db.commit()
    monkeypatch.setattr(teamsync, "PUSH_BATCH", 1)  # many batches in one sync
    teamsync.sync(a)
    rows = a.db.execute("SELECT op, attempts FROM outbox ORDER BY seq").fetchall()
    assert [tuple(r) for r in rows] == [("insert", 1), ("update", 0)]
    for _ in range(teamsync.RETRY_LIMIT):
        teamsync.sync(a)
    assert a.db.execute("SELECT count(*) FROM outbox WHERE op = 'insert'").fetchone()[0] == 0
    assert any("refused by the server" in n for n in teamsync.notes(a))


def test_pull_of_a_renamed_part_into_a_local_part(server, client, monkeypatch):
    """A part of the server gets the MPN and manufacturer of a different local
    part (the server merged that part into it): no unique error, one part."""
    mpn = f"REN{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    mine = a.add_part(mpn, "TI")
    teamsync.sync(a)
    _as(server, monkeypatch, "rita")
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    gid = "00000000-0000-7000-8000-" + uuid.uuid4().hex[:12]  # comes first in a pull
    other = {"mpn": "OTHER" + mpn, "manufacturer": "TI", "family": "", "created": "2000-01-01T00:00:00+00:00",
             "base": None}
    client_b = teamsync.remote(b)
    client_b.call("POST", "/v1/sync/push", {"changes": [{"seq": 1, "tbl": "parts", "op": "insert", "key": gid,
                                                         "data": other}]})
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    _as(server, monkeypatch, "rita")
    answer = client_b.call("POST", "/v1/sync/push", {"changes": [
        {"seq": 2, "tbl": "parts", "op": "update", "key": gid, "base_version": 1, "data": {**other, "mpn": mpn}}]})
    assert answer["results"][0]["result"] == "applied"
    _as(server, monkeypatch, "alice")
    teamsync.sync(a)
    rows = a.db.execute("SELECT gid FROM parts WHERE mpn = ?", (mpn,)).fetchall()
    assert [r[0] for r in rows] == [gid] and a.part(mine) is None


def test_project_check_pulls_only(server, client, make_pdf, monkeypatch):
    a = client("a", "alice")
    teamsync.connect(a, server["url"])
    a.add_part(f"PO{uuid.uuid4().hex[:6]}", "TI")
    assert teamsync.state(a)["waiting"] == 1
    assert teamsync.sync_before_answer(a) == []
    assert teamsync.state(a)["waiting"] == 1  # no push in a tool call


def test_connect_sends_the_full_state(server, client, monkeypatch):
    """An outbox that has only a part of the store before the first connect:
    connect sends all rows."""
    mpn = f"FULL{uuid.uuid4().hex[:6]}"
    a = client("a", "alice")
    a.add_part(mpn, "TI")
    a.db.execute("UPDATE sync_state SET recording = 1")
    a.db.commit()
    a.add_part(mpn + "B", "TI")  # only this part is in the outbox
    teamsync.connect(a, server["url"])
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    assert {mpn, mpn + "B"} <= set(_parts(b))


BUNDLE_PAGES = [[(72, 80, 16, "1 Features"), (72, 120, 10, "Output voltage 3.3 V"), (72, 140, 10, "Table 1. Ratings")],
                [(72, 80, 16, "2 Electrical Characteristics"), (72, 120, 10, "Quiescent current 25 nA")]]


def _bundle_doc(store, text_pdf):
    """A document with an index, a text element map, and a fact on an element."""
    from datasheet_lens.elements import build_elements
    from datasheet_lens.indexer import build_index

    doc = store.add_document(text_pdf(f"b{uuid.uuid4().hex[:6]}.pdf",
                                      BUNDLE_PAGES + [[(72, 80, 10, uuid.uuid4().hex)]]))
    build_index(store, doc.id)
    build_elements(store, doc.id, "text")
    element = store.db.execute("SELECT element_id FROM elements WHERE document_id = ? AND page = 1"
                               " ORDER BY page_order LIMIT 1", (doc.id,)).fetchone()[0]
    fact = knowledge.save_fact(store, doc.id, 1, "title", "Features", element_id=element)
    return doc, fact


def test_bundles_go_through_the_server(server, client, text_pdf, monkeypatch):
    """The first store uploads its index and element map; a second store gets them
    from the server, with no PDF file and no build, and the same element IDs."""
    from datasheet_lens.elements import build_elements
    from datasheet_lens.indexer import build_index

    a = client("a", "alice")
    doc, fact = _bundle_doc(a, text_pdf)
    report = teamsync.connect(a, server["url"])
    assert sum("canonical bundle" in line for line in teamsync.sync(a).bundles) == 0  # done in connect
    assert all(a.build(doc.id, kind)["bundle"] for kind in ("index", "elements")), report
    b = client("b", "rita")
    teamsync.connect(b, server["url"])
    copy = b.document(doc.sha256)
    assert build_index(b, copy.id)["bundle"] == a.build(doc.id, "index")["bundle"]
    assert build_elements(b, copy.id, "text")["bundle"] == a.build(doc.id, "elements")["bundle"]
    assert not copy.path.exists()  # no PDF: the bundles are enough
    rows = lambda s, d: s.db.execute("SELECT element_id, page, x0, y0, x1, y1, text FROM elements"  # noqa: E731
                                     " WHERE document_id = ? ORDER BY element_id", (d,)).fetchall()
    assert [tuple(r) for r in rows(b, copy.id)] == [tuple(r) for r in rows(a, doc.id)]
    gid = a.db.execute("SELECT gid FROM knowledge WHERE id = ?", (fact,)).fetchone()[0]
    assert b.db.execute("SELECT element_id FROM knowledge WHERE gid = ?", (gid,)).fetchone()[0] == \
        a.db.execute("SELECT element_id FROM knowledge WHERE id = ?", (fact,)).fetchone()[0]


def test_better_bundle_replaces_the_build(server, client, text_pdf, monkeypatch):
    """A docling bundle replaces the text bundle in each store; the facts follow
    their region to the new element IDs."""
    from datasheet_lens import bundles

    a = client("a", "alice")
    doc, fact = _bundle_doc(a, text_pdf)
    teamsync.connect(a, server["url"])
    _as(server, monkeypatch, "rita")
    c = client("c", "rita")
    teamsync.connect(c, server["url"])
    copy = c.document(doc.sha256)
    from datasheet_lens.elements import build_elements
    build_elements(c, copy.id, "text")  # the text bundle of a
    # A local docling map of c (here: the same regions with other IDs). The sync of c
    # uploads it (better than the text bundle), it does not take the text bundle.
    with c.db:
        c.db.execute("UPDATE elements SET element_id = 'd' || element_id WHERE document_id = ?", (copy.id,))
        c.db.execute("UPDATE builds SET backend = 'docling', bundle = NULL WHERE document_id = ? AND kind = 'elements'",
                     (copy.id,))
    lines = teamsync.sync(c).bundles
    assert any("elements" in line and "uploaded as the canonical bundle" in line for line in lines), lines
    assert c.build(copy.id, "elements")["backend"] == "docling"
    assert bundles.canonical(c.db, doc.sha256, "elements", "1")["backend"] == "docling"
    _as(server, monkeypatch, "alice")
    report = teamsync.sync(a)
    assert any("(docling) replaces the local build" in line for line in report.bundles)
    assert a.build(doc.id, "elements")["backend"] == "docling"
    element = a.db.execute("SELECT element_id FROM knowledge WHERE id = ?", (fact,)).fetchone()[0]
    assert element.startswith("dp1.")
    assert teamsync.state(a)["waiting"] == 0  # the relink is not a shared change


def test_bundle_registration_rules(server, client, text_pdf, monkeypatch):
    a = client("a", "alice")
    doc, _ = _bundle_doc(a, text_pdf)
    teamsync.connect(a, server["url"])
    body = {"document_sha": doc.sha256, "kind": "elements", "format": "1", "backend": "text",
            "blob_sha": "0" * 64, "versions": {}}
    with pytest.raises(teamsync.LensError, match="upload it first"):
        teamsync.remote(a).call("POST", "/v1/bundles", body)
    with pytest.raises(teamsync.LensError, match="the backend pdfium"):
        teamsync.remote(a).call("POST", "/v1/bundles", {**body, "kind": "index", "backend": "docling"})
    _as(server, monkeypatch, "rudi")
    with pytest.raises(teamsync.LensError, match="403"):
        teamsync.Remote(server["url"], server["tokens"]["rudi"]).call("POST", "/v1/bundles", body)


def test_server_with_no_bundles(server, client, text_pdf, monkeypatch):
    """A server of an older version: no bundles, one note (not one for each build)."""
    a = client("a", "alice")
    _bundle_doc(a, text_pdf)
    original = teamsync.Remote.call

    def old_server(self, method, path, body=None, params=None, timeout=teamsync.TIMEOUT):
        answer = original(self, method, path, body, params, timeout)
        if path == "/v1/account":
            answer.pop("features", None)
        return answer

    monkeypatch.setattr(teamsync.Remote, "call", old_server)
    teamsync.connect(a, server["url"])
    teamsync.sync(a)
    rows = a.db.execute("SELECT text FROM sync_notes WHERE text LIKE '%no bundles%'").fetchall()
    assert len(rows) == 1
