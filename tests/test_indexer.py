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
    assert result == {"pages": 2, "outline": 4, "captions": 2, "skipped": False}
    assert build_index(store, doc.id)["skipped"] is True

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


def test_search_modes_and_page_headers(store_home, text_pdf):
    """A number and its unit with or without a space, whole words, a regex, and
    no lines that repeat on many pages (a page header with its page number)."""
    pages = [[(50, 20, 8, f"GD25LE64E Datasheet  page {n}"), (50, 100, 10, line)]
             for n, line in enumerate(["C1 100nF to GND", "Configuration of the chip", "Table 3. Pin Function",
                                       "VDD 1.8 V supply"], start=1)]
    store = Store()
    doc = store.add_document(text_pdf("gd.pdf", pages))
    build_index(store, doc.id)
    pages_of = lambda *a, **k: [p for _, p, _ in search(store, [doc.id], *a, **k)]  # noqa: E731
    assert pages_of("100 nF") == [1] and pages_of("100nf") == [1] and pages_of("1.8V") == [4]
    assert pages_of("nf") == [1, 2] and pages_of("nF", mode="word") == [1]
    assert pages_of("Pin Functions?", mode="regex") == [3]
    hidden: list = []
    assert pages_of("GD25LE64E", hidden=hidden) == [] and len(hidden) == 4
    assert pages_of("GD25LE64E", repeated=True) == [1, 2, 3, 4]
    with pytest.raises(ValueError, match="not a regular expression"):
        search(store, [doc.id], "(", mode="regex")


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


def test_raster_min_dpi_ignores_strips():
    """Gradient strips (64 x 1 px over 325 x 20 pt, 256 x 4 px over 400 x 24 pt)
    are not low resolution images. A figure of 100 x 100 px over 144 x 144 pt is:
    50 dpi."""
    import pypdfium2 as pdfium

    from datasheet_lens.pdftext import raster_min_dpi

    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(612, 792)
    for px, (x, y, w, h), expected in (((64, 1), (55, 36, 325, 20), None),
                                       ((256, 4), (55, 700, 400, 24), None),
                                       ((100, 100), (100, 300, 144, 144), pytest.approx(50))):
        image = pdfium.PdfImage.new(pdf)
        bitmap = pdfium.PdfBitmap.new_native(*px, pdfium.raw.FPDFBitmap_BGR)
        image.set_bitmap(bitmap)
        bitmap.close()
        image.set_matrix(pdfium.PdfMatrix().scale(w, h).translate(x, y))
        page.insert_obj(image)  # the page owns the image object now
        assert raster_min_dpi(page) == expected
    page.close()
    pdf.close()


def test_search_keeps_register_lines_and_signs(store_home, text_pdf):
    """Register lines that differ only in a number are not page headers. A
    query that starts with a sign has no word boundary there. A regex keeps its
    spaces."""
    pages = [[(50, 20, 8, f"nRF9151  {n}"), (50, 100, 10, f"Address offset: 0x5{n}4"),
              (50, 120, 10, f"TA=-40 °C, VDD = {n}.8 V")] for n in range(1, 5)]
    store = Store()
    doc = store.add_document(text_pdf("nrf.pdf", pages))
    build_index(store, doc.id)
    pages_of = lambda *a, **k: [p for _, p, _ in search(store, [doc.id], *a, **k)]  # noqa: E731
    assert pages_of("Address offset") == [1, 2, 3, 4]
    assert pages_of("nRF9151") == []  # the header with its page number
    assert pages_of("-40", mode="word") == [1, 2, 3, 4]
    assert pages_of("offset:  0x524", mode="regex") == [] and pages_of("offset: 0x524", mode="regex") == [2]
