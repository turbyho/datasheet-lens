"""The bundles of derived data and the relink of the facts (no server is necessary)."""

import json
import sqlite3

import pytest

from datasheet_lens import bundles, knowledge
from datasheet_lens.elements import build_elements
from datasheet_lens.errors import LensError
from datasheet_lens.indexer import build_index
from datasheet_lens.store import Store

PAGES = [[(72, 80, 16, "1 Features"), (72, 120, 10, "Output voltage 3.3 V"), (72, 140, 10, "Table 1. Ratings")],
         [(72, 80, 16, "2 Electrical Characteristics"), (72, 120, 10, "Quiescent current 25 nA")]]


@pytest.fixture
def built(store_home, text_pdf):
    """A store with a document that has an index and a text element map."""
    store = Store()
    doc = store.add_document(text_pdf("ldo.pdf", PAGES))
    build_index(store, doc.id)
    build_elements(store, doc.id, "text")
    return store, doc


def _rows(store, doc, kind):
    return {t: store.db.execute(f"SELECT {', '.join(c)} FROM {t} WHERE document_id = ? ORDER BY 1",
                                (doc.id,)).fetchall() for t, c in bundles.TABLES[kind].items()}


def test_export_is_the_same_for_the_same_build(built):
    store, doc = built
    first = bundles.export(store, doc, "elements")
    assert bundles.export(store, doc, "elements") == first
    build_elements(store, doc.id, "text", force=True)  # a new build of the same file
    assert bundles.export(store, doc, "elements") == first


def test_import_needs_no_file(built, tmp_path):
    """A second store has only the row of the document: the bundles give its index
    and element map."""
    store, doc = built
    other = Store(tmp_path / "other")
    other.db.execute("INSERT INTO documents (sha256, filename, media_type, size, pages, added) VALUES (?, ?, ?, ?,"
                     " ?, ?)", (doc.sha256, doc.filename, doc.media_type, doc.size, doc.pages, "2026-11-05"))
    other.db.commit()
    copy = other.document(doc.sha256)
    assert not copy.path.exists()
    for kind in ("index", "elements"):
        raw = bundles.export(store, doc, kind)
        counts = bundles.import_bundle(other, copy, kind, raw, "f" * 64)
        assert counts == {t: len(r) for t, r in _rows(store, doc, kind).items()}
        assert [tuple(r) for rows in _rows(other, copy, kind).values() for r in rows] == [
            tuple(r) for rows in _rows(store, doc, kind).values() for r in rows]
        build = other.build(copy.id, kind)
        assert build["bundle"] == "f" * 64 and json.loads(build["versions"])["datasheet-lens"]
    other.close()


def test_import_refuses_a_wrong_bundle(built):
    store, doc = built
    raw = bundles.export(store, doc, "index")
    with pytest.raises(LensError, match="not a elements bundle"):
        bundles.import_bundle(store, doc, "elements", raw, "a" * 64)
    with pytest.raises(LensError, match="not gzip JSON"):
        bundles.import_bundle(store, doc, "index", b"not gzip", "a" * 64)


def test_canonical_order(store_home):
    with Store() as store:
        for backend, created, blob in (("text", "2026-01-01", "b" * 64), ("docling", "2026-02-01", "c" * 64)):
            store.db.execute("INSERT INTO bundles VALUES ('d', 'elements', '1', ?, ?, '{}', ?)",
                             (backend, blob, created))
        # docling before text, also when the text bundle is older
        assert bundles.canonical(store.db, "d", "elements", "1")["blob_sha"] == "c" * 64
        assert bundles.canonical(store.db, "d", "index", "1") is None


def test_fact_keeps_the_region_of_its_element(built):
    store, doc = built
    element = store.db.execute("SELECT * FROM elements WHERE document_id = ? AND page = 1 ORDER BY page_order"
                               " LIMIT 1", (doc.id,)).fetchone()
    fact = knowledge.save_fact(store, doc.id, 1, "title", "Features", element_id=element["element_id"])
    bbox = json.loads(store.db.execute("SELECT bbox FROM knowledge WHERE id = ?", (fact,)).fetchone()[0])
    assert bbox == [element["x0"], element["y0"], element["x1"], element["y1"]]


def test_relink(built):
    """A new element map: a fact gets the element with its region; a fact with
    no such element keeps its page and bbox, and gets a note; a fact with only a
    region stays. No outbox rows."""
    store, doc = built
    e1, e2 = store.db.execute("SELECT * FROM elements WHERE document_id = ? AND page = 1 ORDER BY page_order"
                              " LIMIT 2", (doc.id,)).fetchall()
    moved = knowledge.save_fact(store, doc.id, 1, "a", "1", element_id=e1["element_id"])
    lost = knowledge.save_fact(store, doc.id, 1, "b", "2", element_id=e2["element_id"], note="from the table")
    region = knowledge.save_fact(store, doc.id, 1, "c", "3", bbox=[e1["x0"], e1["y0"], e1["x1"], e1["y1"]])
    store.db.execute("UPDATE sync_state SET recording = 1")
    # A different map: e1 has a new ID, e2 is gone.
    store.db.execute("UPDATE elements SET element_id = 'p1.new' WHERE document_id = ? AND element_id = ?",
                     (doc.id, e1["element_id"]))
    store.db.execute("DELETE FROM elements WHERE document_id = ? AND element_id = ?", (doc.id, e2["element_id"]))
    store.db.commit()
    with store.db:
        assert bundles.relink(store.db, doc.id) == 2
    facts = {r["id"]: r for r in store.db.execute("SELECT id, element_id, note FROM knowledge")}
    assert facts[moved]["element_id"] == "p1.new"
    assert facts[lost]["element_id"] == "" and facts[lost]["note"] == "from the table; " + bundles.CHANGED_NOTE
    assert facts[region]["element_id"] == ""
    assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    with store.db:
        assert bundles.relink(store.db, doc.id) == 0  # a second relink changes nothing
    # The element comes back (a newer map): the note of the change goes.
    store.db.execute("INSERT INTO elements SELECT * FROM elements WHERE 0")
    store.db.execute("INSERT INTO elements (document_id, element_id, page, type, code, page_order, x0, y0, x1, y1,"
                     " text) VALUES (?, 'p1.back', 1, 'text', 'X', 99, ?, ?, ?, ?, '')",
                     (doc.id, e2["x0"], e2["y0"], e2["x1"], e2["y1"]))
    with store.db:
        assert bundles.relink(store.db, doc.id) == 1
    row = store.db.execute("SELECT element_id, note FROM knowledge WHERE id = ?", (lost,)).fetchone()
    assert tuple(row) == ("p1.back", "from the table")


def test_migration_gives_the_bbox_of_the_element(built):
    """Version 5: a fact with an element and no bbox gets the region of its
    element, and a connected store records it for the team server."""
    store, doc = built
    element = store.db.execute("SELECT * FROM elements WHERE document_id = ? LIMIT 1", (doc.id,)).fetchone()
    fact = knowledge.save_fact(store, doc.id, element["page"], "x", "1", element_id=element["element_id"])
    store.db.execute("UPDATE knowledge SET bbox = '' WHERE id = ?", (fact,))
    store.db.execute("UPDATE sync_state SET recording = 1")
    store.db.execute("DELETE FROM outbox")
    store.db.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    store.db.commit()
    home = store.home
    store.close()
    db = sqlite3.connect(home / "db" / "lens.db")
    for column in ("bundle", "versions"):
        db.execute(f"ALTER TABLE builds DROP COLUMN {column}")
    db.commit()
    db.close()
    with Store(home) as again:
        bbox = json.loads(again.db.execute("SELECT bbox FROM knowledge WHERE id = ?", (fact,)).fetchone()[0])
        assert bbox == [element["x0"], element["y0"], element["x1"], element["y1"]]
        assert [tuple(r) for r in again.db.execute("SELECT tbl, op FROM outbox")] == [("knowledge", "update")]
        assert {"bundle", "versions"} <= {r[1] for r in again.db.execute("PRAGMA table_info(builds)")}


def test_bundle_with_a_worse_backend_is_not_taken(built):
    """A request for docling: a text bundle of the server is not taken (the store
    builds docling, and the next sync makes it canonical)."""
    store, doc = built
    with store.db:
        store.db.execute("UPDATE sync_state SET server = 'http://127.0.0.1:9'")
        store.db.execute("INSERT INTO bundles VALUES (?, 'elements', ?, 'text', ?, '{}', '2026-11-05')",
                         (doc.sha256, bundles.kind_format("elements"), "e" * 64))
    assert bundles.from_server(store, doc, "elements", "docling") is None
    assert store.build(doc.id, "elements")["bundle"] is None


def test_same_note_is_one_note(store_home):
    from datasheet_lens import teamsync

    with Store() as store, store.db:
        teamsync.note(store.db, "the same")
        store.db.execute("UPDATE sync_notes SET shown = 1")
        teamsync.note(store.db, "the same")
        assert [tuple(r) for r in store.db.execute("SELECT text, shown FROM sync_notes")] == [("the same", 0)]
