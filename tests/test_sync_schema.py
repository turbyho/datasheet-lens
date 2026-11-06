"""The schema for the team server: the gids, the outbox and its triggers, the
shared relations of manufacturer names, and the migration of version 3."""

import json
import sqlite3

import pytest

from datasheet_lens import knowledge, makers
from datasheet_lens.store import FULL_PULL, SCHEMA_VERSION, Store, merge_duplicates, not_recorded


def _record(store: Store) -> None:
    """Connect the store to a server, for the test: the triggers write the outbox."""
    store.db.execute("UPDATE sync_state SET recording = 1")
    store.db.commit()


def _outbox(store: Store, after: int = 0) -> list[tuple]:
    return [(r["tbl"], r["op"], r["key"], r["base_version"], json.loads(r["data"]) if r["data"] else None)
            for r in store.db.execute("SELECT * FROM outbox WHERE seq > ? ORDER BY seq", (after,))]


def _last(store: Store) -> int:
    return store.db.execute("SELECT coalesce(max(seq), 0) FROM outbox").fetchone()[0]


def _gid(store: Store, table: str, row_id: int) -> str:
    return store.db.execute(f"SELECT gid FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_new_rows_get_gids_in_order(store_home):
    """Each shared row gets a gid (UUID version 7). The text order of the gids
    is the order of the creation, also in one millisecond."""
    with Store() as store:
        ids = [store.add_part(f"P{n:03d}") for n in range(300)]
        gids = [_gid(store, "parts", i) for i in ids]
        assert len(set(gids)) == 300 and gids == sorted(gids)
        assert all(len(g) == 36 and g[14] == "7" and g[19] in "89ab" for g in gids)


def test_no_outbox_with_no_server(store_home, make_pdf):
    """A store that is not connected to a server writes no outbox rows."""
    with Store() as store:
        doc = store.add_document(make_pdf())
        store.link(store.add_part("LM358", "TI"), doc.id)
        store.maker_relation("alias", "TXN", "TI")
        assert _last(store) == 0
        assert store.db.execute("SELECT gid FROM maker_relations").fetchone()[0]


def test_outbox_of_local_changes(store_home, make_pdf):
    """Each local change of a shared table goes into the outbox, with global
    keys in place of the local IDs."""
    with Store() as store:
        _record(store)
        doc = store.add_document(make_pdf(pages=3), source_url="")
        base = store.add_part("TLV755P", "TI")
        concrete = store.add_part("TLV75533PDBVR", "TI")
        store.link(base, doc.id)
        store.set_base(concrete, base)
        fact = knowledge.save_fact(store, doc.id, 2, "Vout", "3.3", unit="V", part="TLV755P")
        knowledge.set_status(store, fact, "checked")
        base_gid, concrete_gid = _gid(store, "parts", base), _gid(store, "parts", concrete)
        rows = _outbox(store)
        assert [(t, op) for t, op, *_ in rows] == [
            ("documents", "insert"), ("parts", "insert"), ("parts", "insert"), ("part_documents", "insert"),
            ("parts", "update"), ("knowledge", "insert"), ("knowledge", "update")]
        assert rows[0][2] == doc.sha256 and rows[0][4]["pages"] == 3
        assert rows[1][2] == base_gid and rows[1][4]["mpn"] == "TLV755P"
        assert json.loads(rows[3][2]) == [base_gid, doc.sha256]
        assert rows[4][2] == concrete_gid and rows[4][4]["base"] == base_gid and rows[4][3] == 0
        assert rows[5][4]["part"] == base_gid and rows[5][4]["document"] == doc.sha256
        assert rows[6][4]["status"] == "checked" and rows[6][3] == 0


def test_no_outbox_row_for_no_change(store_home):
    """An UPDATE that changes no value writes no outbox row (SQLite runs an
    AFTER UPDATE trigger also then)."""
    with Store() as store:
        _record(store)
        part = store.add_part("LM358", "TI")
        last = _last(store)
        store.db.execute("UPDATE parts SET family = family WHERE id = ?", (part,))
        store.db.execute("UPDATE parts SET version = 5 WHERE id = ?", (part,))  # not a shared value
        assert _last(store) == last


def test_cascade_writes_no_outbox_row(store_home, make_pdf):
    """The delete of a part writes one row: the server does the FK cascade of
    its links and aliases itself."""
    with Store() as store:
        _record(store)
        doc = store.add_document(make_pdf())
        part = store.add_part("LM358", "TI")
        store.link(part, doc.id)
        store.db.execute("INSERT INTO part_aliases VALUES (?, 'LM358-X')", (part,))
        gid, last = _gid(store, "parts", part), _last(store)
        store.db.execute("DELETE FROM parts WHERE id = ?", (part,))
        assert _outbox(store, last) == [("parts", "delete", gid, 0, None)]


def test_not_recorded(store_home):
    """Rows from the server keep their gid and write no outbox row. The state
    of the recording comes back after the block."""
    with Store() as store:
        _record(store)
        with not_recorded(store.db):
            store.db.execute("INSERT INTO parts (mpn, manufacturer, created, gid, version)"
                             " VALUES ('FROMSERVER', '', '2026-11-05', 'server-gid', 4)")
        assert _last(store) == 0
        assert store.db.execute("SELECT recording FROM sync_state").fetchone()[0] == 1
        assert store.db.execute("SELECT gid FROM parts WHERE mpn = 'FROMSERVER'").fetchone()[0] == "server-gid"


def test_relation_is_shared_and_merge_is_not(store_home, make_pdf):
    """A relation of manufacturer names is a shared decision. The merge of the
    parts that it causes is not in the outbox: each store and the server make
    it themselves."""
    with Store() as store:
        doc = store.add_document(make_pdf())
        keep = store.add_part("X100", "Proto Semi")
        store.maker_relation("distinct", "Proto Semi", "Proto Semiconductor")
        dup = store.add_part("X100", "Proto Semiconductor")
        store.link(dup, doc.id)
        store.db.execute("DELETE FROM manufacturer_distinct")
        store.db.commit()
        _record(store)
        assert store.maker_relation("alias", "Proto Semiconductor", "Proto Semi") == [
            "X100: Proto Semiconductor into Proto Semi"]
        rows = _outbox(store)
        assert [(t, op) for t, op, *_ in rows] == [("maker_relations", "insert")]
        assert rows[0][4] | {"created": ""} == {"relation": "alias", "name": "Proto Semiconductor",
                                                 "other": "Proto Semi", "created": ""}
        assert store.part(dup) is None and [r["id"] for r in store.part_documents("X100")] == [doc.id]
        assert store.part(keep) is not None


def test_merge_keeps_the_same_part_in_each_store(tmp_path):
    """Two stores with the same parts (same gids) in a different local order
    keep the same part: the order is created, then gid, not the local ID."""
    kept = []
    for name, order in (("a", ("gid-1", "gid-2")), ("b", ("gid-2", "gid-1"))):
        with Store(tmp_path / name) as store:
            makers_of = {"gid-1": "Proto Semi", "gid-2": "Proto Semiconductor"}
            for gid in order:
                store.db.execute("INSERT INTO parts (mpn, manufacturer, created, gid)"
                                 " VALUES ('X100', ?, '2026-11-05T10:00:00+00:00', ?)", (makers_of[gid], gid))
            makers.set_alias(store.db, "Proto Semiconductor", "Proto Semi")
            merge_duplicates(store.db)
            kept += [r[0] for r in store.db.execute("SELECT gid FROM parts WHERE mpn = 'X100'")]
    assert kept == ["gid-1", "gid-1"]


def test_plain_sqlite_connection_writes_through_the_triggers(store_home):
    """The triggers use only built-in SQL: a connection with no function of
    datasheet-lens (the sqlite3 shell) can write, and gets gids and outbox rows."""
    with Store() as store:
        _record(store)
        path = store.home / "db" / "lens.db"
    db = sqlite3.connect(path)
    db.execute("INSERT INTO parts (mpn, manufacturer, created) VALUES ('RAW', '', '2026-11-05')")
    db.commit()
    gid = db.execute("SELECT gid FROM parts WHERE mpn = 'RAW'").fetchone()[0]
    assert db.execute("SELECT tbl, op, key FROM outbox").fetchall() == [("parts", "insert", gid)]
    db.close()


def _version_2(home) -> None:
    """Make the store of home a store of version 2: no gids, no versions, no
    sync tables, no relations of manufacturer names (as the old version)."""
    db = sqlite3.connect(home / "db" / "lens.db")
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        db.execute(f"DROP TRIGGER {name}")
    for name in ("parts_gid", "knowledge_gid", "maker_relations_gid"):
        db.execute(f"DROP INDEX {name}")
    for table in ("sync_state", "outbox", "maker_relations"):
        db.execute(f"DROP TABLE {table}")
    for table, columns in (("parts", ("gid", "version")), ("knowledge", ("gid", "version")),
                           ("documents", ("version",))):
        for column in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    db.execute("DELETE FROM meta WHERE key = 'sync_triggers'")
    db.commit()
    db.close()


def _groups(db: sqlite3.Connection) -> set[frozenset[str]]:
    """The manufacturers of a registry, each as the set of the keys of its names."""
    groups: dict[int, set[str]] = {}
    for mid, key in db.execute("SELECT manufacturer_id, key FROM manufacturer_aliases"):
        groups.setdefault(mid, set()).add(key)
    return {frozenset(g) for g in groups.values()}


def _distinct(db: sqlite3.Connection) -> set[frozenset[str]]:
    names = dict(db.execute("SELECT id, name FROM manufacturers"))
    return {frozenset((makers.key(names[a]), makers.key(names[b])))
            for a, b in db.execute("SELECT a, b FROM manufacturer_distinct")}


def test_migration_from_version_2(store_home, make_pdf, tmp_path):
    """A store of version 2: each part and fact gets a gid in the order of its
    creation, and the registry gives the relations that make it again from
    the built-in names."""
    with Store() as store:
        doc = store.add_document(make_pdf())
        for mpn, created in (("B", "2026-10-02T08:00:00+00:00"), ("A", "2026-10-01T08:00:00+00:00"),
                             ("C", "2026-10-02T08:00:00+00:00")):
            store.db.execute("INSERT INTO parts (mpn, manufacturer, created) VALUES (?, '', ?)", (mpn, created))
        knowledge.save_fact(store, doc.id, 1, "Vout", "3.3", part="A")
        store.maker_relation("alias", "TXN", "Texas Instruments")       # a new name
        store.maker_relation("alias", "Fairchild", "onsemi")            # a new name, joins a built-in one
        store.maker_relation("alias", "Maxim Integrated", "Analog Devices")  # two built-in manufacturers
        store.maker_relation("distinct", "ROHM", "TI")
        store.maker_relation("distinct", "Acme", "Nexperia")            # a name only for distinct
        store.db.commit()
        groups, distinct = _groups(store.db), _distinct(store.db)
        home = store.home
    _version_2(home)

    with Store(home) as store:
        db = store.db
        assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        order = [r[0] for r in db.execute("SELECT mpn FROM parts ORDER BY gid")]
        assert order[:3] == ["A", "B", "C"]
        assert db.execute("SELECT count(*) FROM knowledge WHERE gid IS NULL").fetchone()[0] == 0
        relations = db.execute("SELECT relation, name, other FROM maker_relations ORDER BY gid").fetchall()
        assert all(r[0] == "alias" for r in relations[:3]) and len(relations) == 5
        assert _last(store) == 0
        # The triggers are there again: a new part continues the gids.
        new = _gid(store, "parts", store.add_part("NEW"))
        assert new > max(r[0] for r in db.execute("SELECT gid FROM parts WHERE mpn != 'NEW'"))

    # The relations make the same registry in a new store.
    with Store(tmp_path / "new") as fresh:
        for relation, name, other in relations:
            makers.relate(fresh.db, relation, name, other)
        assert _groups(fresh.db) == groups and _distinct(fresh.db) == distinct


def test_gids_continue_after_the_last_gid(store_home):
    """A new gid continues after the last gid of the store: also when the clock
    goes back, after 1000 rows in one millisecond, and with gids of a different
    store in the table."""
    with Store() as store:
        db = store.db
        future = 4102444800000  # 2100-01-01: a clock that went back from there
        db.execute("UPDATE sync_state SET gid_ms = ?, gid_counter = 998", (future,))
        with not_recorded(db):  # a row of a different store, with a counter in hexadecimal digits
            db.execute("INSERT INTO parts (mpn, created, gid) VALUES ('OTHER', '', ?)",
                       ("0" * 8 + "-0000-7fff-8000-000000000000",))
        gids = [_gid(store, "parts", store.add_part(f"P{n}")) for n in range(4)]
        assert gids == sorted(gids) and len(set(gids)) == 4
        assert gids[0][15:18] == "999" and gids[1][15:18] == "000" and gids[1][:13] > gids[0][:13]


def test_repeated_relation_is_one_decision(store_home):
    """A relation that the store has already is not a new shared decision."""
    with Store() as store:
        _record(store)
        store.maker_relation("alias", "TXN", "TI")
        store.maker_relation("alias", "TXN", "TI")
        store.maker_relation("alias", "ti", "txn")
        assert store.db.execute("SELECT count(*) FROM maker_relations").fetchone()[0] == 1
        assert [(t, op) for t, op, *_ in _outbox(store)] == [("maker_relations", "insert")]


def test_document_delete_writes_one_row(store_home, make_pdf):
    """The delete of a document writes one row: the server does the FK cascade
    of its links and facts itself."""
    with Store() as store:
        _record(store)
        doc = store.add_document(make_pdf())
        store.link(store.add_part("LM358", "TI"), doc.id)
        knowledge.save_fact(store, doc.id, 1, "Vos", "3", unit="mV")
        last = _last(store)
        store.db.execute("DELETE FROM documents WHERE id = ?", (doc.id,))
        assert _outbox(store, last) == [("documents", "delete", doc.sha256, 0, None)]


def test_part_delete_writes_no_fact_update(store_home, make_pdf):
    """A fact of a deleted part gets part_id NULL (ON DELETE SET NULL). The
    server does the same: no update row for the fact."""
    with Store() as store:
        _record(store)
        doc = store.add_document(make_pdf())
        part = store.add_part("LM358", "TI")
        knowledge.save_fact(store, doc.id, 1, "Vos", "3", unit="mV", part="LM358")
        gid, last = _gid(store, "parts", part), _last(store)
        store.db.execute("DELETE FROM parts WHERE id = ?", (part,))
        assert _outbox(store, last) == [("parts", "delete", gid, 0, None)]


def test_triggers_of_an_older_version_are_replaced(store_home):
    """A store with triggers of a different version gets the triggers of this
    version, and no trigger of the old version stays."""
    with Store() as store:
        store.db.execute("CREATE TRIGGER old_one AFTER INSERT ON parts BEGIN SELECT 1; END")
        store.db.execute("UPDATE meta SET value = 'old' WHERE key = 'sync_triggers'")
        store.db.commit()
    with Store() as store:
        names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert "old_one" not in names and "parts_ins" in names


def test_tombstone_with_merged_into_merges_locally(store_home, make_pdf):
    """A pulled tombstone of a part with merged_into, and the kept part is local:
    the local part merges into it (links and facts move), with no outbox row."""
    from datasheet_lens import teamsync

    with Store() as store:
        doc = store.add_document(make_pdf())
        store.maker_relation("distinct", "AA", "BB")
        keep = store.add_part("X100", "AA")
        dup = store.add_part("X100", "BB")
        store.link(dup, doc.id)
        store.db.execute("INSERT INTO knowledge (part_id, document_id, page, subject, value, source_kind, created)"
                         " VALUES (?, ?, 1, 'Vout', '3.3', 'model', 'x')", (dup, doc.id))
        store.db.commit()
        _record(store)
        keep_gid, dup_gid = _gid(store, "parts", keep), _gid(store, "parts", dup)
        with store.db, not_recorded(store.db):
            teamsync._apply(store.db, [{"tbl": "parts", "key": dup_gid, "version": 2, "deleted": True,
                                        "merged_into": keep_gid, "data": {}}], {})
        assert store.part(dup) is None
        assert [r["id"] for r in store.part_documents("X100")] == [doc.id]
        assert store.db.execute("SELECT part_id FROM knowledge").fetchone()[0] == keep
        assert _last(store) == 0


def test_migration_of_an_early_version_3(store_home):
    """A store of an early version 3 (the sync tables with fewer columns) gets
    the columns of version 4."""
    with Store() as store:
        home = store.home
    db = sqlite3.connect(home / "db" / "lens.db")
    for table, columns in (("sync_state", ("server", "cursor", "last_sync", "last_error", "lease_until",
                                           "lease_owner")), ("outbox", ("attempts",))):
        for column in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.execute("DROP TABLE sync_notes")
    db.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with Store(home) as store:
        state = dict(store.db.execute("SELECT * FROM sync_state").fetchone())
        assert {"server", "cursor", "lease_owner"} <= set(state) and state["lease_until"] == 0
        assert "attempts" in {r[1] for r in store.db.execute("PRAGMA table_info(outbox)")}
        assert "shown" in {r[1] for r in store.db.execute("PRAGMA table_info(sync_notes)")}


def test_lease(store_home):
    """One sync of a store at a time; a sync that lost its lease stops."""
    from datasheet_lens import teamsync
    from datasheet_lens.errors import LensError

    with Store() as store:
        first, second = teamsync.Lease(store), teamsync.Lease(store)
        assert first.take() and not second.take()
        store.db.execute("UPDATE sync_state SET lease_until = 0")  # the time of the lease ended
        store.db.commit()
        assert second.take()
        with pytest.raises(LensError, match="lost its lease"):
            first.renew()
        first.release()  # not the owner: no change
        assert store.db.execute("SELECT lease_owner FROM sync_state").fetchone()[0] == second.owner
        second.release()
        assert store.db.execute("SELECT lease_until FROM sync_state").fetchone()[0] == 0


def test_background_sync_continues_after_an_error(store_home, monkeypatch, capsys):
    import threading

    from datasheet_lens import teamsync

    stop, calls = threading.Event(), []

    def broken(store):
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        raise ValueError("not JSON")

    monkeypatch.setattr(teamsync, "connected", lambda store: True)
    monkeypatch.setattr(teamsync, "sync", broken)
    monkeypatch.setattr(teamsync, "SYNC_INTERVAL", 0)
    teamsync.background(stop, Store)
    assert len(calls) == 2 and "ValueError: not JSON" in capsys.readouterr().err


def test_outbox_of_part_sources(store_home):
    """A purchase source goes into the outbox with the gid of its part, the
    source and the number as its key. A new URL is an update; the same values
    again write no row. The delete of the part writes one row: the server does
    the FK cascade of its sources itself."""
    with Store() as store:
        _record(store)
        part = store.add_part("CSD16340Q3", "TI")
        gid, last = _gid(store, "parts", part), _last(store)
        store.set_part_source(part, "digikey", "296-25646-1-ND", url="https://dk/a", found="2026-11-06")
        store.set_part_source(part, "digikey", "296-25646-1-ND", url="https://dk/a", found="2026-11-06")
        store.set_part_source(part, "digikey", "296-25646-1-ND", url="https://dk/b", found="2026-11-06")
        store.set_part_source(part, "jlcpcb", "C47616", library="Extended", found="2026-11-06")
        assert store.delete_part_source(part, "jlcpcb", "C47616")
        assert not store.delete_part_source(part, "jlcpcb", "C47616")
        dk, jlc = json.dumps([gid, "digikey", "296-25646-1-ND"]), json.dumps([gid, "jlcpcb", "C47616"])
        rows = [(t, op, json.loads(k), v, d) for t, op, k, v, d in _outbox(store, last)]
        assert rows == [
            ("part_sources", "insert", json.loads(dk), None, {"url": "https://dk/a", "library": "", "found": "2026-11-06"}),
            ("part_sources", "update", json.loads(dk), None, {"url": "https://dk/b", "library": "", "found": "2026-11-06"}),
            ("part_sources", "insert", json.loads(jlc), None,
             {"url": "", "library": "Extended", "found": "2026-11-06"}),
            ("part_sources", "delete", json.loads(jlc), None, None)]
        last = _last(store)
        store.db.execute("DELETE FROM parts WHERE id = ?", (part,))
        assert _outbox(store, last) == [("parts", "delete", gid, 0, None)]
        assert store.db.execute("SELECT count(*) FROM part_sources").fetchone()[0] == 0


def test_record_all_sends_the_part_sources(store_home):
    """The first push of a store sends its purchase sources, after their parts."""
    from datasheet_lens.store import record_all

    with Store() as store:
        part = store.add_part("CSD16340Q3", "TI")
        store.set_part_source(part, "jlcpcb", "C47616", library="Extended", found="2026-11-06")
        assert record_all(store.db) == 2
        rows = _outbox(store)
        assert [(t, op) for t, op, *_ in rows] == [("parts", "insert"), ("part_sources", "insert")]
        assert json.loads(rows[1][2]) == [_gid(store, "parts", part), "jlcpcb", "C47616"]
        assert rows[1][4] == {"url": "", "library": "Extended", "found": "2026-11-06"}


def test_merge_moves_the_part_sources(store_home):
    """A merge of two parts moves the purchase sources of the duplicate part to
    the kept part, with no outbox row. A source that the kept part has already
    keeps its values."""
    with Store() as store:
        keep = store.add_part("X100", "Proto Semi")
        store.maker_relation("distinct", "Proto Semi", "Proto Semiconductor")
        dup = store.add_part("X100", "Proto Semiconductor")
        store.set_part_source(keep, "digikey", "X100-ND", url="https://dk/keep", found="2026-11-01")
        store.set_part_source(dup, "digikey", "X100-ND", url="https://dk/dup", found="2026-11-02")
        store.set_part_source(dup, "jlcpcb", "C100", library="Basic", found="2026-11-02")
        store.db.execute("DELETE FROM manufacturer_distinct")
        store.db.commit()
        _record(store)
        assert store.maker_relation("alias", "Proto Semiconductor", "Proto Semi") == [
            "X100: Proto Semiconductor into Proto Semi"]
        assert store.part(dup) is None
        assert store.part_sources(keep) == [
            {"source": "digikey", "number": "X100-ND", "url": "https://dk/keep", "library": "", "found": "2026-11-01"},
            {"source": "jlcpcb", "number": "C100", "url": "", "library": "Basic", "found": "2026-11-02"}]
        assert [(t, op) for t, op, *_ in _outbox(store)] == [("maker_relations", "insert")]


def test_pulled_part_sources(store_home):
    """The purchase sources of the server: an insert, an update of the values
    and a delete, with no outbox row. A source of a part that the store does not
    have is ignored."""
    from datasheet_lens import teamsync

    with Store() as store:
        part = store.add_part("CSD16340Q3", "TI")
        _record(store)
        gid, last = _gid(store, "parts", part), _last(store)

        def apply(number, deleted=False, url="", library="", other_gid=None):
            row = {"tbl": "part_sources", "key": json.dumps([other_gid or gid, "jlcpcb", number]), "version": 1,
                   "deleted": deleted, "data": {"url": url, "library": library, "found": "2026-11-06"}}
            with store.db, not_recorded(store.db):
                teamsync._apply(store.db, [row], {})

        apply("C47616", library="Extended")
        apply("C47616", url="https://jlc/C47616", library="Basic")
        apply("C1", library="Basic")
        apply("C2", other_gid="no-such-part")
        assert store.part_sources(part) == [
            {"source": "jlcpcb", "number": "C1", "url": "", "library": "Basic", "found": "2026-11-06"},
            {"source": "jlcpcb", "number": "C47616", "url": "https://jlc/C47616", "library": "Basic",
             "found": "2026-11-06"}]
        apply("C1", deleted=True)
        assert [s["number"] for s in store.part_sources(part)] == ["C47616"]
        assert _last(store) == last


def test_migration_from_version_5(store_home, make_pdf):
    """A store of version 5 (no part_sources) gets the table and keeps its data.
    The triggers of the table come with the migration. A connected store keeps its
    cursor and gets the mark FULL_PULL: the older version ignored the purchase
    sources of the server (see test_team_sync)."""
    with Store() as store:
        doc = store.add_document(make_pdf())
        part = store.add_part("LM358", "TI")
        store.link(part, doc.id)
        knowledge.save_fact(store, doc.id, 1, "Vos", "3", unit="mV", part="LM358")
        home = store.home
    db = sqlite3.connect(home / "db" / "lens.db")
    db.execute("DROP TABLE part_sources")  # also its triggers
    db.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    db.execute("UPDATE meta SET value = 'version-5' WHERE key = 'sync_triggers'")
    db.execute("UPDATE sync_state SET cursor = 42")
    db.commit()
    db.close()
    with Store(home) as store:
        db = store.db
        assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert SCHEMA_VERSION == 6
        assert [r["mpn"] for r in store.parts()] == ["LM358"]
        assert [r["id"] for r in store.part_documents("LM358")] == [doc.id]
        assert db.execute("SELECT value FROM knowledge").fetchone()[0] == "3"
        assert store.part_sources(part) == []
        assert db.execute("SELECT cursor FROM sync_state").fetchone()[0] == 42
        assert db.execute("SELECT value FROM meta WHERE key = ?", (FULL_PULL,)).fetchone()[0] == "1"
        _record(store)
        store.set_part_source(part, "mouser", "595-LM358", found="2026-11-06")
        assert [(t, op) for t, op, *_ in _outbox(store)] == [("part_sources", "insert")]


def test_migration_from_version_5_with_no_server(store_home):
    """A store with no cursor (not connected) gets no mark FULL_PULL: its first
    pull is a full pull."""
    with Store() as store:
        home = store.home
    db = sqlite3.connect(home / "db" / "lens.db")
    db.execute("DROP TABLE part_sources")
    db.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with Store(home) as store:
        assert store.db.execute("SELECT 1 FROM meta WHERE key = ?", (FULL_PULL,)).fetchone() is None


class _FakeServer:
    """A fake team server. tables None: an older server, which does not give its
    tables in /v1/account. It refuses the parts of refuse (a reader), has no rows
    to pull, and records each change that it gets."""

    def __init__(self, tables=None, refuse=()):
        self.tables, self.refuse, self.sent = tables, refuse, []

    def call(self, method, path, body=None, params=None, timeout=0):
        if path == "/v1/account":
            return {"name": "alice", "role": "contributor", "features": ["sync"]} | (
                {"tables": list(self.tables)} if self.tables is not None else {})
        if path == "/v1/sync/pull":
            return {"rows": [], "more": False, "after": None, "cursor": 1}
        assert (method, path) == ("POST", "/v1/sync/push")
        results = []
        for change in body["changes"]:
            self.sent.append((change["tbl"], change["op"]))
            if change["key"] in self.refuse:
                results.append({"seq": change["seq"], "result": "rejected", "retry": False,
                                "reason": "the account has the role reader"})
            else:
                results.append({"seq": change["seq"], "result": "applied", "version": 1})
        return {"results": results}


def test_changes_of_a_table_that_the_server_does_not_know_wait(store_home, monkeypatch):
    """A server of an older version does not give its tables, and does not know
    part_sources: the changes of part_sources stay in the outbox, are not sent and
    get no attempt, and one note tells the user to upgrade the server. The next
    sync after the upgrade sends them. A different refusal removes the change, as
    before."""
    from datasheet_lens import teamsync

    with Store() as store:
        _record(store)
        store.db.execute("UPDATE sync_state SET server = 'https://team.example.com'")
        store.db.commit()
        part = store.add_part("CSD16340Q3", "TI")
        refused = _gid(store, "parts", store.add_part("LM358", "TI"))
        store.set_part_source(part, "jlcpcb", "C47616", library="Extended", found="2026-11-06")
        store.set_part_source(part, "jlcpcb", "C47616", library="Basic", found="2026-11-06")
        store.set_part_source(part, "digikey", "296-25646-1-ND", found="2026-11-06")
        old = _FakeServer(refuse=(refused,))
        monkeypatch.setattr(teamsync, "remote", lambda store: old)
        for _ in range(teamsync.RETRY_LIMIT + 1):  # no attempt counts: the changes do not go away
            report = teamsync.sync(store)
            assert [n for n in report.notes if "does not know" in n] == [
                "the team server does not know the table part_sources: upgrade the team server; the changes of this "
                "table wait in the outbox"]
        assert old.sent == [("parts", "insert"), ("parts", "insert")]  # the first sync: applied, and refused
        rows = store.db.execute("SELECT tbl, op, attempts FROM outbox ORDER BY seq").fetchall()
        assert [tuple(r) for r in rows] == [("part_sources", "insert", 0), ("part_sources", "update", 0),
                                            ("part_sources", "insert", 0)]
        assert any(f"refused by the server: parts insert {refused}" in n for n in teamsync.notes(store))
        new = _FakeServer(tables=teamsync.BASE_TABLES + ("part_sources",))
        monkeypatch.setattr(teamsync, "remote", lambda store: new)
        report = teamsync.sync(store)
        assert report.pushed == 3 and not [n for n in report.notes if "does not know" in n]
        assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert new.sent == [("part_sources", "insert"), ("part_sources", "update"), ("part_sources", "insert")]
