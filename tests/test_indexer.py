import pytest

from datasheet_lens.indexer import build_index, index_markdown, outline_under, search
from datasheet_lens.store import Store

PAGES = [
    [(50, 60, 10, "TABLE OF CONTENTS"),
     (50, 80, 10, "1. OVERVIEW .................... 2"),
     (50, 95, 10, "2. PINOUT ...................... 2")],
    [(50, 60, 12, "1. OVERVIEW"),
     (50, 80, 10, "The part is a real-time clock."),
     (50, 120, 12, "2. PINOUT"),
     (50, 140, 12, "2.1. PIN DESCRIPTION"),
     (50, 160, 12, "2.1.1.CONTROL_1 (address 00h…bits)"),
     (50, 180, 9, "6.91 Co: 7440-48-4"),
     (50, 200, 9, "5.0 V"),
     (50, 300, 9, "Table 2-1. Pin Functions"),
     (330, 300, 9, "Figure 2-1.Package Top View"),
     (50, 400, 10, "The circuit of Figure 2-1 shows the pins.")],
]


def test_index_without_bookmarks(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("manual.pdf", PAGES))
    result = build_index(store, doc.id)
    assert result == {"pages": 2, "outline": 4, "captions": 2}

    md = index_markdown(store, doc.id)
    # Headings from the text layer, not from the table of contents (page 1).
    assert "- 1 OVERVIEW (p.2)" in md
    assert "  - 2.1 PIN DESCRIPTION (p.2)" in md
    # Two captions on one line: both, with order and side of the page.
    assert "Table 2-1. Pin Functions (p.2, 1/2, top left" in md
    assert "Figure 2-1. Package Top View (p.2, 2/2, top right" in md
    # A short document gets a page map.
    assert "- p.1: TABLE OF CONTENTS" in md

    # The section itself and the entries under it.
    assert [o["title"] for o in outline_under(store, doc.id, "2")] == [
        "2 PINOUT", "2.1 PIN DESCRIPTION", "2.1.1 CONTROL_1 (address 00h…bits)"]


def test_search_finds_page_and_line(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("manual.pdf", PAGES))
    build_index(store, doc.id)
    hits = search(store, [doc.id], "real-time")
    assert hits == [(doc.id, 2, "The part is a real-time clock.")]


def test_search_is_substring_and_rejects_empty(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("s.pdf", [[(50, 100, 10, "AVDD supply pin of OPA2134")]]))
    build_index(store, doc.id)
    assert search(store, [doc.id], "vdd")[0][1] == 1
    assert search(store, [doc.id], "OPA213")[0][1] == 1
    with pytest.raises(ValueError):
        search(store, [doc.id], "  ")


def test_heading_with_a_number_at_its_end(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("t.pdf", [[(50, 60, 12, "4. TIMERS"), (50, 90, 12, "4.2 Timer 2")]]))
    build_index(store, doc.id)
    assert [o["title"] for o in outline_under(store, doc.id, "4")] == ["4 TIMERS", "4.2 Timer 2"]


def test_rotated_page_gives_horizontal_lines(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("r.pdf", [[(300, 700, 12, "Landscape Table Title")]], rotate=90))
    build_index(store, doc.id)
    row = store.db.execute("SELECT width_pt, height_pt, text FROM pages WHERE document_id = ?",
                           (doc.id,)).fetchone()
    assert (row["width_pt"], row["height_pt"]) == (842, 595)
    assert row["text"] == "Landscape Table Title"


def test_page_fingerprint_ignores_margins(store_home, text_pdf):
    body = [(50, 200, 10, "Electrical characteristics")]
    a = text_pdf("a.pdf", [body + [(50, 20, 8, "Downloaded from distributor A")]])
    b = text_pdf("b.pdf", [body + [(50, 20, 8, "Downloaded from distributor B")]])
    store = Store()
    ids = [store.add_document(p).id for p in (a, b)]
    for i in ids:
        build_index(store, i)
    shas = [store.db.execute("SELECT text_sha FROM pages WHERE document_id = ?", (i,)).fetchone()[0]
            for i in ids]
    assert shas[0] and shas[0] == shas[1]
