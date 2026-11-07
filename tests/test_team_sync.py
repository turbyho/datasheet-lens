"""The synchronization of the shared tables on the team server (pull, push,
conflicts, merges), against a real PostgreSQL. These tests run only with
DSLENS_TEST_PG (see test_team_server); else pytest skips them."""

import asyncio
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DSLENS_TEST_PG"), reason="no test database (DSLENS_TEST_PG)")

if os.environ.get("DSLENS_TEST_PG"):
    import asyncpg

    from datasheet_lens.team import sync
    from datasheet_lens.team.db import Database

DOC = "a" * 64
DOC2 = "b" * 64


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db():
    """A new database for each test, with the accounts reader, alice
    (contributor), rita (reviewer), adam (admin) and two files."""
    base = os.environ["DSLENS_TEST_PG"].rstrip("/")
    name = f"dslens_sync_{uuid.uuid4().hex[:8]}"

    async def admin_sql(sql):
        conn = await asyncpg.connect(f"{base}/postgres")
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    async def prepare():
        await admin_sql(f"CREATE DATABASE {name}")
        database = Database(f"{base}/{name}")
        await database.connect()
        try:
            await database.init_schema()
            accounts = {}
            for account, role in (("reader", "reader"), ("alice", "contributor"), ("rita", "reviewer"),
                                  ("adam", "admin")):
                accounts[account] = {"id": await database.create_account(account, role), "name": account,
                                     "role": role}
            for sha in (DOC, DOC2):
                await database.add_blob(sha, 1000, "application/pdf", accounts["adam"]["id"])
            return accounts
        finally:
            await database.close()

    accounts = run(prepare())
    # The drop has no FORCE: a connection that a test leaves open is an error of the test.
    yield {"url": f"{base}/{name}", **accounts}
    run(admin_sql(f"DROP DATABASE {name}"))


def with_database(db, func, *args):
    """Run func(database, *args) with a new connected Database: a new pool and
    registry cache, one event loop for each call."""

    async def go():
        database = Database(db["url"], max_size=4)
        await database.connect()
        try:
            return await func(database, *args)
        finally:
            await database.close()

    return run(go())


def call(db, func, *args):
    """Run func(pool, *args), a function of sync, with a new pool."""
    return with_database(db, lambda database, *rest: func(database.pool, *rest), *args)


def push(db, account, *changes):
    return with_database(db, sync.push, db[account], [{"seq": i, **c} for i, c in enumerate(changes)])


def pull_all(db, cursor=None):
    """All pages of a pull: (rows, cursor)."""
    rows, after = [], None
    while True:
        page = call(db, sync.pull, cursor, after)
        rows += page["rows"]
        if not page["more"]:
            return rows, page["cursor"]
        after = page["after"]


def part(gid, mpn, maker="", created="2026-11-01T10:00:00+00:00", base=None, family=""):
    return {"tbl": "parts", "op": "insert", "key": gid,
            "data": {"mpn": mpn, "manufacturer": maker, "family": family, "created": created, "base": base}}


def document(sha=DOC, size=1000):
    return {"tbl": "documents", "op": "insert", "key": sha,
            "data": {"filename": "x.pdf", "media_type": "application/pdf", "size": size, "pages": 3, "title": "",
                     "manufacturer": "", "doc_number": "", "revision": "", "revision_date": "", "source_url": "",
                     "added": "2026-11-01T10:00:00+00:00", "supersedes": None}}


def link(gid, sha=DOC, op="insert"):
    return {"tbl": "part_documents", "op": op, "key": json.dumps([gid, sha]), "data": {"role": "datasheet"}}


def fact(gid, part_gid, status="unchecked", sha=DOC):
    return {"tbl": "knowledge", "op": "insert", "key": gid,
            "data": {"part": part_gid, "document": sha, "page": 1, "element_id": "", "bbox": "", "subject": "Vout",
                     "value": "3.3", "unit": "V", "conditions": "", "column_name": "", "note": "",
                     "source_kind": "model", "model": "m", "prompt_version": "", "author": "",
                     "created": "2026-11-01T10:00:00+00:00", "status": status}}


def results(answer):
    return [r["result"] for r in answer]


def by_key(rows, table):
    return {r["key"]: r for r in rows if r["tbl"] == table}


def test_push_and_pull(db):
    answer = push(db, "alice", document(), part("p-base", "TLV755P", "TI"),
                  part("p-conc", "TLV75533PDBVR", "TI", base="p-base"), link("p-base"), fact("f1", "p-base"))
    assert results(answer) == ["applied"] * 5
    rows, cursor = pull_all(db)
    assert [r["tbl"] for r in rows] == ["documents", "parts", "parts", "part_documents", "knowledge"]
    assert [r["key"] for r in rows if r["tbl"] == "parts"] == ["p-base", "p-conc"]  # the base first
    assert by_key(rows, "parts")["p-conc"]["data"]["base"] == "p-base"
    assert by_key(rows, "knowledge")["f1"]["data"]["part"] == "p-base"
    assert pull_all(db, cursor)[0] == []
    # The same insert again (a retry after a network error): applied, no change.
    assert results(push(db, "alice", part("p-base", "TLV755P", "TI"))) == ["applied"]
    assert pull_all(db, cursor)[0] == []


def test_pages_with_one_upper_bound(db, monkeypatch):
    """A row that changes between two pages of the first pull comes in the next
    pull (the pages use the upper bound of the first page)."""
    push(db, "alice", *[part(f"p{n}", f"MPN{n}") for n in range(5)])
    monkeypatch.setattr(sync, "PAGE", 2)
    first = call(db, sync.pull, None, None)
    assert first["more"] and len(first["rows"]) == 2
    sent = first["rows"][0]
    push(db, "alice", {"tbl": "parts", "op": "update", "key": sent["key"], "base_version": 1,
                       "data": {**sent["data"], "family": "LDO"}})
    rows, after = list(first["rows"]), first["after"]
    while True:
        page = call(db, sync.pull, None, after)
        rows += page["rows"]
        if not page["more"]:
            break
        after = page["after"]
    assert sorted(r["key"] for r in rows) == [f"p{n}" for n in range(5)]  # each row one time
    later, _ = pull_all(db, page["cursor"])
    assert [(r["key"], r["data"]["family"]) for r in later] == [(sent["key"], "LDO")]


def test_conflict_on_an_old_version(db):
    push(db, "alice", part("p1", "LM358", "TI"))
    row = part("p1", "LM358", "TI")["data"]  # an update has the full row, as the outbox of a client
    change = {"tbl": "parts", "op": "update", "key": "p1", "data": {**row, "family": "OpAmp"}}
    assert results(push(db, "alice", {**change, "base_version": 1})) == ["applied"]
    answer = push(db, "alice", {**change, "base_version": 1, "data": {**row, "family": "Comparator"}})
    assert answer[0]["result"] == "conflict" and answer[0]["row"]["data"]["family"] == "OpAmp"
    assert answer[0]["row"]["version"] == 2


def test_roles(db):
    assert push(db, "reader", part("p1", "LM358"))[0]["result"] == "rejected"
    push(db, "alice", document(), part("p1", "LM358"), fact("f1", "p1"))
    row = fact("f1", "p1")["data"]
    status = {"tbl": "knowledge", "op": "update", "key": "f1", "base_version": 1, "data": {**row, "status": "checked"}}
    assert "reviewer" in push(db, "alice", status)[0]["reason"]
    assert results(push(db, "rita", status)) == ["applied"]
    # A checked fact: a contributor cannot change its value, also with the status unchanged.
    value = {"tbl": "knowledge", "op": "update", "key": "f1", "base_version": 2,
             "data": {**row, "status": "checked", "value": "5.5"}}
    assert "reviewer" in push(db, "alice", value)[0]["reason"]
    # An unchecked fact: a contributor can change it.
    push(db, "alice", fact("f2", "p1"))
    assert results(push(db, "alice", {"tbl": "knowledge", "op": "update", "key": "f2", "base_version": 1,
                                      "data": {**fact("f2", "p1")["data"], "note": "see table 5"}})) == ["applied"]
    assert "admin" in push(db, "alice", {"tbl": "parts", "op": "delete", "key": "p1", "base_version": 1}
                           )[0]["reason"]


def test_references_and_files(db):
    answer = push(db, "alice", link("missing"), document(sha="c" * 64), document(size=999), fact("f1", None))
    assert [r["result"] for r in answer] == ["rejected"] * 4
    assert "not on the server" in answer[0]["reason"] and "upload it first" in answer[1]["reason"]
    assert "size" in answer[2]["reason"] and "document" in answer[3]["reason"]


def test_newer_duplicate_is_merged(db):
    """Two persons make the same part: the newer one is merged into the older
    one. A later row with the old gid refers to the kept part."""
    push(db, "alice", document(), part("p-old", "TPS7A02", "TI", created="2026-11-01T09:00:00+00:00"))
    answer = push(db, "rita", part("p-new", "TPS7A02", "Texas Instruments", created="2026-11-02T09:00:00+00:00"),
                  link("p-new"))
    assert answer[0] == {"seq": 0, "result": "merged", "gid": "p-old"}
    assert answer[1]["result"] == "applied"
    rows, _ = pull_all(db)
    assert list(by_key(rows, "parts")) == ["p-old"]
    assert list(by_key(rows, "part_documents")) == [json.dumps(["p-old", DOC], separators=(",", ":"))]
    assert push(db, "rita", part("p-new", "TPS7A02", "Texas Instruments"))[0] == {
        "seq": 0, "result": "merged", "gid": "p-old"}


def test_older_duplicate_takes_the_part(db):
    """The pushed part is older: the part of the server goes into it, with its
    links and facts, and gets merged_into."""
    push(db, "alice", document(), part("p-srv", "TPS7A02", "TI", created="2026-11-02T09:00:00+00:00"),
         link("p-srv"), fact("f1", "p-srv"))
    _, cursor = pull_all(db)
    assert results(push(db, "rita", part("p-old", "TPS7A02", "TI", created="2026-11-01T09:00:00+00:00"))) == [
        "applied"]
    rows, _ = pull_all(db, cursor)
    parts = by_key(rows, "parts")
    assert parts["p-srv"]["deleted"] and parts["p-srv"]["merged_into"] == "p-old"
    assert not parts["p-old"]["deleted"]
    assert by_key(rows, "knowledge")["f1"]["data"]["part"] == "p-old"
    links = by_key(rows, "part_documents")
    assert not links[json.dumps(["p-old", DOC], separators=(",", ":"))]["deleted"]
    assert links[json.dumps(["p-srv", DOC], separators=(",", ":"))]["deleted"]


def test_relation_merges_parts(db):
    push(db, "alice", part("p1", "X100", "Proto Semi", created="2026-11-01T09:00:00+00:00"),
         {"tbl": "maker_relations", "op": "insert", "key": "r0",
          "data": {"relation": "distinct", "name": "Proto Semi", "other": "Proto Semiconductor",
                   "created": "2026-11-01T09:00:00+00:00"}},
         part("p2", "X100", "Proto Semiconductor", created="2026-11-01T10:00:00+00:00"))
    rows, cursor = pull_all(db)
    assert len(by_key(rows, "parts")) == 2  # two manufacturers: a second source
    # The opposite relation: the registry of the server refuses it.
    alias = {"tbl": "maker_relations", "op": "insert", "key": "r1",
             "data": {"relation": "alias", "name": "Proto Semiconductor", "other": "Proto Semi",
                      "created": "2026-11-02T09:00:00+00:00"}}
    answer = push(db, "alice", alias, part("p3", "X200", "Proto Semi"))
    assert answer[0]["result"] == "rejected" and "refuses" in answer[0]["reason"]
    assert answer[1]["result"] == "applied"  # the registry stays correct after the refused relation
    # Without the distinct pair, the alias merges the two parts into the older one.
    async def drop(pool):
        async with pool.acquire() as conn:
            await conn.execute("UPDATE maker_relations SET deleted = true WHERE gid = 'r0'")
    call(db, drop)
    assert results(push(db, "alice", alias)) == ["applied"]
    parts = by_key(pull_all(db, cursor)[0], "parts")
    assert parts["p2"]["deleted"] and parts["p2"]["merged_into"] == "p1"


def test_registry_cache_follows_the_relations(db, monkeypatch):
    """One Database (one pool and one registry cache) for all pushes, as in the
    app: the cache gets each new relation, keeps no refused relation, loads the
    registry again after a change of a relation in the database, and loads
    nothing for a push with no change of a relation."""
    distinct = {"tbl": "maker_relations", "op": "insert", "key": "r0",
                "data": {"relation": "distinct", "name": "Proto Semi", "other": "Proto Semiconductor",
                         "created": "2026-11-01T09:00:00+00:00"}}
    alias = {"tbl": "maker_relations", "op": "insert", "key": "r1",
             "data": {"relation": "alias", "name": "Proto Semiconductor", "other": "Proto Semi",
                      "created": "2026-11-02T09:00:00+00:00"}}
    loads = []
    load = sync.Registry.load.__func__

    async def counted(cls, conn):
        loads.append(1)
        return await load(cls, conn)

    monkeypatch.setattr(sync.Registry, "load", classmethod(counted))

    async def go(database):
        async def send(*changes):
            before = len(loads)
            answer = results(await sync.push(database, db["alice"],
                                             [{"seq": i, **c} for i, c in enumerate(changes)]))
            return answer, len(loads) - before

        answers = [await send(distinct),
                   await send(part("p1", "X100", "Proto Semi")),
                   await send(part("p2", "X100", "Proto Semiconductor")),  # distinct: a second part
                   await send(alias, part("p3", "X200", "Proto Semi")),  # the alias is refused
                   await send(part("p4", "X200", "Proto Semiconductor"))]  # no alias in the cache: a second part
        async with database.pool.acquire() as conn:
            await conn.execute("UPDATE maker_relations SET deleted = true WHERE gid = 'r0'")
        answers += [await send(alias),  # the cache loads again: no distinct pair, the alias is applied
                    await send(part("p5", "X300", "Proto Semi")),
                    await send(part("p6", "X300", "Proto Semiconductor"))]  # the cached alias: a duplicate
        return answers

    answers = with_database(db, go)
    assert [a for a, _ in answers] == [["applied"], ["applied"], ["applied"], ["rejected", "applied"], ["applied"],
                                       ["applied"], ["applied"], ["merged"]]
    # The pushes of parts only: one load after a change of the relations (p1, p5), else none.
    assert [answers[i][1] for i in (1, 2, 4, 6, 7)] == [1, 0, 0, 1, 0]


def test_a_failed_or_deleted_relation_is_not_in_the_rest_of_the_push(db):
    """The registry of a push holds only the relations of its transaction: a
    relation that the registry takes but the database refuses (a NUL character),
    and a relation that the push deletes, do not merge the next parts."""
    # The key of a name ignores the NUL: the registry takes the alias, the database refuses it.
    bad = {"tbl": "maker_relations", "op": "insert", "key": "r0",
           "data": {"relation": "alias", "name": "Proto Semiconductor", "other": "Proto Semi\x00",
                    "created": "2026-11-01T09:00:00+00:00"}}
    answer = push(db, "alice", bad, part("p1", "X1", "Proto Semi"), part("p2", "X1", "Proto Semiconductor"))
    assert results(answer) == ["rejected", "applied", "applied"]
    assert "database refuses" in answer[0]["reason"]
    alias = {"tbl": "maker_relations", "op": "insert", "key": "r1",
             "data": {"relation": "alias", "name": "Proto Semiconductor", "other": "Proto Semi",
                      "created": "2026-11-02T09:00:00+00:00"}}
    assert results(push(db, "alice", alias)) == ["applied"]  # it merges p2 into p1
    answer = push(db, "adam", {"tbl": "maker_relations", "op": "delete", "key": "r1", "base_version": None},
                  part("p3", "X2", "Proto Semi"), part("p4", "X2", "Proto Semiconductor"))
    assert results(answer) == ["applied", "applied", "applied"]


def test_delete_with_cascade(db):
    push(db, "alice", document(), part("p1", "LM358", "TI"), part("p2", "LM358DR", "TI", base="p1"), link("p1"),
         fact("f1", "p1"))
    _, cursor = pull_all(db)
    assert results(push(db, "adam", {"tbl": "parts", "op": "delete", "key": "p1", "base_version": 1})) == [
        "applied"]
    rows, _ = pull_all(db, cursor)
    assert by_key(rows, "parts")["p1"]["deleted"] and by_key(rows, "parts")["p2"]["data"]["base"] is None
    assert all(r["deleted"] for r in rows if r["tbl"] == "part_documents")
    assert by_key(rows, "knowledge")["f1"]["data"]["part"] is None and not by_key(rows, "knowledge")["f1"]["deleted"]
    # A new link of a deleted part: rejected.
    assert push(db, "alice", link("p1"))[0]["result"] == "rejected"


def test_audit(db):
    push(db, "alice", part("p1", "LM358", "TI"))

    async def audit(pool):
        async with pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch("SELECT account_id, tbl, key, op FROM audit")]

    assert call(db, audit) == [{"account_id": db["alice"]["id"], "tbl": "parts", "key": "p1", "op": "insert"}]


def test_same_part_from_two_clients_at_the_same_time(db):
    """The lock on the MPN: two parallel pushes of the same new part give one
    part, not two."""
    for attempt in range(5):
        mpn = f"RACE{attempt}"

        async def both(database):
            a = sync.push(database, db["alice"],
                          [{"seq": 0, **part(f"a{attempt}", mpn, "TI", created="2026-11-01T09:00:00+00:00")}])
            b = sync.push(database, db["rita"],
                          [{"seq": 0, **part(f"b{attempt}", mpn, "TI", created="2026-11-01T10:00:00+00:00")}])
            return await asyncio.gather(a, b)

        with_database(db, both)
        live = [r for r in pull_all(db)[0] if r["tbl"] == "parts" and r["data"]["mpn"] == mpn]
        assert [r["key"] for r in live] == [f"a{attempt}"]


def test_wrong_changes_are_rejected_one_by_one(db):
    """A wrong change is rejected; the other changes of the push are applied."""
    good = part("p1", "LM358", "TI")
    changes = [{**part("p2", "X"), "data": {**part("p2", "X")["data"], "created": None}},   # wrong type
               {"tbl": "knowledge", "op": "insert", "key": "f9",
                "data": {**fact("f9", None)["data"], "source_kind": "ai"}},                 # not a choice
               {"tbl": "part_documents", "op": "insert", "key": "not json", "data": {"role": "datasheet"}},
               {"tbl": "parts", "op": "insert", "key": 5, "data": good["data"]},            # key not a text
               "not a change",                                                              # (the API refuses it too)
               {**part("p3", "Y"), "data": {**part("p3", "Y")["data"], "mpn": "x\x00"}},   # NUL: the database refuses
               good]
    answer = with_database(db, sync.push, db["alice"], changes)
    assert [r["result"] for r in answer] == ["rejected"] * 6 + ["applied"]
    assert "database refuses" in answer[5]["reason"]


def test_link_role_update_has_no_version(db):
    """The local store has no version for a link: an update keeps the last change."""
    push(db, "alice", document(), part("p1", "LM358"), link("p1"))
    change = {"tbl": "part_documents", "op": "update", "key": json.dumps(["p1", DOC]), "base_version": None,
              "data": {"role": "errata"}}
    assert results(push(db, "alice", change)) == ["applied"]
    assert by_key(pull_all(db)[0], "part_documents")[json.dumps(["p1", DOC], separators=(",", ":"))][
        "data"]["role"] == "errata"


def test_document_file_values_do_not_change(db):
    push(db, "alice", document())
    change = {"tbl": "documents", "op": "update", "key": DOC, "base_version": 1,
              "data": {**document()["data"], "size": 1}}
    assert "do not change" in push(db, "alice", change)[0]["reason"]
    change["data"] = {**document()["data"], "title": "OPA2134"}
    assert results(push(db, "alice", change)) == ["applied"]


def test_page_full_at_the_end_of_a_table(db, monkeypatch):
    """A page that is full at the last row of a table: the next page starts with
    the first row of the next table (no row is lost)."""
    push(db, "alice", document(), part("p1", "A1"), part("p2", "A2"))
    monkeypatch.setattr(sync, "PAGE", 1)
    rows, _ = pull_all(db)
    assert [(r["tbl"], r["key"]) for r in rows] == [("documents", DOC), ("parts", "p1"), ("parts", "p2")]


def test_wrong_page_position(db):
    with pytest.raises(sync.BadPosition):
        sync.check_after([9, [], 1])
    with pytest.raises(sync.BadPosition):
        sync.check_after([sync.TABLES.index("parts"), ["x"], 1])  # parts: two order values
    with pytest.raises(sync.BadPosition):
        call(db, sync.pull, None, [0, ["a"], 10 ** 15])  # after the current state


def source(gid, number="C47616", op="insert", url="", library="Extended", found="2026-11-06"):
    return {"tbl": "part_sources", "op": op, "key": json.dumps([gid, "jlcpcb", number]),
            "data": None if op == "delete" else {"url": url, "library": library, "found": found}}


def source_key(gid, number="C47616"):
    return json.dumps([gid, "jlcpcb", number], separators=(",", ":"))


def test_part_sources(db):
    """A purchase source: insert, update of its values (no version), delete by a
    contributor, and an insert of the same key again with new values."""
    push(db, "alice", part("p1", "CSD16340Q3", "TI"))
    _, cursor = pull_all(db)
    assert results(push(db, "alice", source("p1"), source("p1", op="update", url="https://jlc/1"))) == [
        "applied"] * 2
    rows, cursor = pull_all(db, cursor)
    assert [(r["key"], r["version"], r["data"]) for r in rows] == [
        (source_key("p1"), 2, {"url": "https://jlc/1", "library": "Extended", "found": "2026-11-06"})]
    # The same insert again: no change. An insert with new values: an update.
    assert push(db, "rita", source("p1", url="https://jlc/1"))[0] == {"seq": 0, "result": "applied", "version": 2}
    assert pull_all(db, cursor)[0] == []
    assert push(db, "rita", source("p1", library="Basic"))[0] == {"seq": 0, "result": "applied", "version": 3}
    rows, cursor = pull_all(db, cursor)
    assert [r["data"] for r in rows] == [{"url": "", "library": "Basic", "found": "2026-11-06"}]
    # A delete needs the role contributor; an insert after it makes the row live again, with its values.
    assert results(push(db, "alice", source("p1", op="delete"))) == ["applied"]
    rows, cursor = pull_all(db, cursor)
    assert [(r["key"], r["deleted"]) for r in rows] == [(source_key("p1"), True)]
    assert results(push(db, "alice", source("p1", library="Preferred"))) == ["applied"]
    rows, cursor = pull_all(db, cursor)
    assert [(r["deleted"], r["data"]["library"]) for r in rows] == [(False, "Preferred")]
    # Wrong values and references.
    answer = push(db, "alice", source("p1", library="Standard"), source("missing"),
                  {**source("p1"), "data": {"url": "", "library": "Basic"}})
    assert results(answer) == ["rejected"] * 3
    assert "library" in answer[0]["reason"] and "not on the server" in answer[1]["reason"]
    assert "found" in answer[2]["reason"]


def test_part_sources_follow_the_part(db):
    """The delete of a part deletes its sources. A merge of two parts moves the
    sources of the newer part to the older part; a source that the older part has
    keeps its values."""
    push(db, "alice", part("p-old", "TPS7A02", "TI", created="2026-11-01T09:00:00+00:00"),
         part("p-new", "TPS7A02", "Proto Semi", created="2026-11-02T09:00:00+00:00"),
         source("p-old", "C1", url="https://jlc/old"), source("p-new", "C1", url="https://jlc/new"),
         source("p-new", "C2"), part("p-del", "LM358", "TI"), source("p-del", "C3"))
    _, cursor = pull_all(db)
    assert results(push(db, "adam", {"tbl": "parts", "op": "delete", "key": "p-del", "base_version": 1})) == [
        "applied"]
    rows, cursor = pull_all(db, cursor)
    assert by_key(rows, "part_sources")[source_key("p-del", "C3")]["deleted"]

    async def alias(pool):  # the two names are one manufacturer: the parts merge
        async with pool.acquire() as conn, conn.transaction():
            registry = sync.Registry()
            registry.check("alias", "Proto Semi", "TI")
            return await sync.merge_duplicates(conn, registry, None)

    assert call(db, alias) == 1
    sources = by_key(pull_all(db, cursor)[0], "part_sources")
    assert sources[source_key("p-new", "C1")]["deleted"] and sources[source_key("p-new", "C2")]["deleted"]
    assert source_key("p-old", "C1") not in sources  # not changed: it keeps its URL
    assert not sources[source_key("p-old", "C2")]["deleted"]
    live = {r["key"]: r["data"]["url"] for r in pull_all(db)[0] if r["tbl"] == "part_sources"}
    assert live == {source_key("p-old", "C1"): "https://jlc/old", source_key("p-old", "C2"): ""}


def test_server_schema_of_version_3_gets_the_part_sources(db):
    """A server database of version 3 (no part_sources) gets the table at the
    next start, and keeps its rows."""
    push(db, "alice", part("p1", "LM358", "TI"))

    async def downgrade(pool):
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE part_sources")
            await conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")

    async def start():
        database = Database(db["url"])
        await database.connect()
        try:
            await database.init_schema()
            async with database.pool.acquire() as conn:
                return await conn.fetchval("SELECT value FROM meta WHERE key = 'schema_version'")
        finally:
            await database.close()

    call(db, downgrade)
    assert run(start()) == "4"
    assert results(push(db, "alice", source("p1"))) == ["applied"]
    assert [r["tbl"] for r in pull_all(db)[0]] == ["parts", "part_sources"]


class _Client:
    """A client of the team server for teamsync.push and teamsync.pull: the
    functions of sync, with no HTTP."""

    def __init__(self, db, account):
        self.db, self.account = db, account

    def call(self, method, path, body=None, params=None, timeout=0):
        if path == "/v1/sync/pull":
            params = params or {}
            after = json.loads(params["after"]) if "after" in params else None
            return call(self.db, sync.pull, params.get("cursor"), after)
        assert (method, path) == ("POST", "/v1/sync/push")
        return {"results": with_database(self.db, sync.push, self.db[self.account], body["changes"])}


def test_upgraded_client_gets_the_skipped_sources_and_the_deletes(db, tmp_path):
    """An older version of a client ignored the purchase sources in its pulls. After
    the upgrade (migration 6), the next sync gets the deletes after the cursor of
    the store (incremental pull), then the sources (full pull of the live rows).
    The cursor of the store stays the cursor of the incremental pull."""
    import sqlite3

    from datasheet_lens import teamsync
    from datasheet_lens.store import FULL_PULL, Store

    client = _Client(db, "rita")

    def pull(store):
        lease = teamsync.Lease(store)
        assert lease.take()
        try:
            teamsync.pull(store, client, teamsync.Report(), lease)
        finally:
            lease.release()
        return store.db.execute("SELECT cursor FROM sync_state").fetchone()[0]

    push(db, "alice", part("p1", "CSD16340Q3", "TI"), part("p2", "LM358", "TI"))
    store = Store(tmp_path / "client")
    store.db.execute("UPDATE sync_state SET server = 'https://team.example.com', recording = 1")
    store.db.commit()
    pull(store)
    # A newer client adds a source; this client pulls it (the older version ignores it).
    push(db, "alice", source("p1"))
    cursor = pull(store)
    home = store.home
    store.close()
    old = sqlite3.connect(home / "db" / "lens.db")  # the store of the older version: schema 5
    old.execute("DROP TABLE part_sources")
    old.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    old.commit()
    old.close()
    # A delete on the server after the last pull of the older version.
    assert results(push(db, "adam", {"tbl": "parts", "op": "delete", "key": "p2", "base_version": 1})) == [
        "applied"]

    store = Store(home)
    assert store.db.execute("SELECT cursor FROM sync_state").fetchone()[0] == cursor
    assert store.db.execute("SELECT 1 FROM meta WHERE key = ?", (FULL_PULL,)).fetchone()
    new_cursor = pull(store)
    assert [r["mpn"] for r in store.parts()] == ["CSD16340Q3"]  # the delete came
    assert store.part_sources(store.find_part("CSD16340Q3")) == [
        {"source": "jlcpcb", "number": "C47616", "url": "", "library": "Extended", "found": "2026-11-06"}]
    assert new_cursor >= cursor and store.db.execute("SELECT 1 FROM meta WHERE key = ?", (FULL_PULL,)).fetchone() is None
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    # The next pulls are incremental again.
    push(db, "alice", part("p3", "TPS7A02", "TI"))
    assert pull(store) >= new_cursor and len(store.parts()) == 2


def test_account_gives_the_tables(db):
    """/v1/account gives the shared tables of the server: a client keeps the
    changes of a different table in its outbox."""
    from fastapi.testclient import TestClient

    from datasheet_lens.team.app import create_app
    from datasheet_lens.team.config import Settings

    async def token():
        database = Database(db["url"])
        await database.connect()
        try:
            return (await database.create_token("alice"))[1]
        finally:
            await database.close()

    bearer = run(token())
    settings = Settings(db_url=db["url"], s3_endpoint="http://127.0.0.1:9", s3_public_url="http://127.0.0.1:9",
                        s3_key_id="none", s3_secret="none")
    with TestClient(create_app(settings, blobs=object(), cleanup=False)) as c:  # no object store: not used here
        answer = c.get("/v1/account", headers={"Authorization": f"Bearer {bearer}"}).json()
    assert answer["tables"] == list(sync.TABLES) and "part_sources" in answer["tables"]


def test_link_insert_sets_the_role(db):
    """An insert of a link that the server has, with a different role: the role of
    the insert is kept (no version: the last change). The same for a deleted link
    that the insert makes live again."""
    push(db, "alice", document(), part("p1", "LM358"), link("p1"))
    _, cursor = pull_all(db)
    key = json.dumps(["p1", DOC], separators=(",", ":"))
    errata = {**link("p1"), "data": {"role": "errata"}}
    assert push(db, "rita", errata)[0] == {"seq": 0, "result": "applied", "version": 2}
    rows, cursor = pull_all(db, cursor)
    assert [(r["key"], r["version"], r["data"]["role"]) for r in rows] == [(key, 2, "errata")]
    assert push(db, "rita", errata)[0]["version"] == 2  # the same insert again: no change
    assert pull_all(db, cursor)[0] == []
    assert results(push(db, "alice", link("p1", op="delete"))) == ["applied"]
    _, cursor = pull_all(db, cursor)
    assert push(db, "alice", {**link("p1"), "data": {"role": "manual"}})[0]["version"] == 4
    rows, _ = pull_all(db, cursor)
    assert [(r["deleted"], r["data"]["role"]) for r in rows] == [(False, "manual")]


def test_merge_revives_a_link_with_the_role_of_the_duplicate(db):
    """A merge moves a link of the newer part to the older part. When the older
    part had that link and it was deleted, the link is live again with the role
    of the newer part: as the merge of a store, which has no deleted link."""
    push(db, "alice", document(), part("p-old", "TPS7A02", "TI", created="2026-11-01T09:00:00+00:00"),
         part("p-new", "TPS7A02", "Proto Semi", created="2026-11-02T09:00:00+00:00"),
         link("p-old"), {**link("p-new"), "data": {"role": "manual"}})
    assert results(push(db, "alice", link("p-old", op="delete"))) == ["applied"]

    async def alias(pool):  # the two names are one manufacturer: the parts merge
        async with pool.acquire() as conn, conn.transaction():
            registry = sync.Registry()
            registry.check("alias", "Proto Semi", "TI")
            return await sync.merge_duplicates(conn, registry, None)

    assert call(db, alias) == 1
    links = by_key(pull_all(db)[0], "part_documents")
    assert list(links) == [json.dumps(["p-old", DOC], separators=(",", ":"))]
    assert links[json.dumps(["p-old", DOC], separators=(",", ":"))]["data"]["role"] == "manual"
