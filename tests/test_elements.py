import pytest

from datasheet_lens.cli import main
from datasheet_lens.elements import build_elements, docling_available, element, list_items, page_map
from datasheet_lens.errors import LensError
from datasheet_lens.setup import element_backend, init, load_config
from datasheet_lens.store import Store

PAGE = [(50, 60, 12, "5.1 Absolute Maximum Ratings"),
        (50, 90, 10, "Supply voltage up to 36 V."),
        (50, 102, 10, "Input voltage within the rails."),
        (50, 300, 9, "Figure 5-1. Total Harmonic Distortion"),
        (50, 400, 10, "Text after the figure.")]


def test_text_backend_map(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("e.pdf", [PAGE]))
    assert build_elements(store, doc.id, "text") == {"elements": 4, "backend": "text", "skipped": False}
    lines = page_map(store, doc.id, 1)
    assert lines[0].startswith("p1.H1") and "5.1 Absolute Maximum Ratings" in lines[0]
    assert lines[1].startswith("p1.C1") and "Figure 5-1." in lines[1]
    assert lines[2].startswith("+ 2 text elements (p1.P1")
    # The two body lines are one block.
    assert element(store, doc.id, "p1.P1")["text"] == "Supply voltage up to 36 V. Input voltage within the rails."
    assert [r["element_id"] for r in list_items(store, doc.id, "caption")] == ["p1.C1"]


def test_text_backend_register_page(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("r.pdf", [[
        (50, 60, 11, "8.27.5.143 DOEPCTL3"),
        (50, 90, 9, "ID L K J I H G F E E D C B A A A"),
        (50, 110, 9, "Reset 0x00000000"),
        (50, 130, 9, "A"), (80, 130, 9, "RW DMAADDR"), (200, 130, 9, "DMA address"),
    ]]))
    build_elements(store, doc.id, "text")
    heads = [line for line in page_map(store, doc.id, 1) if ".H" in line.split()[0]]
    assert len(heads) == 1 and "8.27.5.143 DOEPCTL3" in heads[0]


def test_text_backend_two_columns(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("c.pdf", [[
        (50, 100, 10, "Left column text one."), (320, 100, 10, "Right column text one."),
        (50, 112, 10, "Left column text two."), (320, 112, 10, "Figure 4-1. Block Diagram"),
    ]]))
    build_elements(store, doc.id, "text")
    rows = {r["element_id"]: r for r in store.db.execute(
        "SELECT * FROM elements WHERE document_id = ?", (doc.id,))}
    assert rows["p1.P1"]["text"] == "Left column text one. Left column text two."
    assert rows["p1.P1"]["x1"] < 300
    assert rows["p1.P2"]["text"] == "Right column text one."
    assert rows["p1.C1"]["text"] == "Figure 4-1. Block Diagram"


def test_headings_are_not_values(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("v.pdf", [[
        (50, 60, 10, "105.0 Tj Max 125 C"), (50, 80, 10, "125.0 MHz"),
        (50, 100, 10, "192.168.1.1 Host"), (50, 120, 10, "1.00 Au80 / Sn20"),
        (50, 140, 12, "3.2.1. CONTROL_1 (address 00h)"),
    ]]))
    build_elements(store, doc.id, "text")
    heads = [r["text"] for r in store.db.execute(
        "SELECT text FROM elements WHERE document_id = ? AND code = 'H'", (doc.id,))]
    assert heads == ["3.2.1. CONTROL_1 (address 00h)"]


def test_page_map_bad_page_and_element_bbox(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("e.pdf", [PAGE]))
    build_elements(store, doc.id, "text")
    with pytest.raises(LensError):
        page_map(store, doc.id, 9)
    assert main(["page-image", "1", "--element", "p1.H1", "--bbox", "0,0,10,10"]) == 2


def test_page_map_needs_a_map(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf())
    with pytest.raises(LensError):
        page_map(store, doc.id, 1)


def test_cli_element_image(store_home, text_pdf, capsys):
    path = text_pdf("e.pdf", [PAGE])
    assert main(["add", str(path)]) == 0
    assert main(["elements", "--backend", "text"]) == 0
    assert main(["page-image", "1", "--element", "p1.H1"]) == 0
    out = capsys.readouterr().out
    assert "page 1, region 44," in out and "600 dpi" in out
    assert main(["page-image", "1", "--element", "p1.X9"]) == 2


def test_init_without_docling_is_not_interactive(store_home, monkeypatch):
    monkeypatch.setattr("datasheet_lens.setup.docling_available", lambda: False)
    monkeypatch.setattr("datasheet_lens.elements.docling_available", lambda: False)
    messages = []
    assert init("no", out=messages.append) == 0
    assert load_config()["element_backend"] == "text"
    assert element_backend() == "text"
    assert any("init --layout yes" in m for m in messages)


@pytest.mark.skipif(not docling_available(), reason="docling is not installed")
def test_docling_coordinates_on_a_rotated_page(store_home, text_pdf):
    """The docling bbox must be in the coordinates of the shown page, as the index
    and page_image use them."""
    import pypdfium2 as pdfium

    from datasheet_lens.pdftext import page_lines

    store = Store()
    doc = store.add_document(text_pdf("r.pdf", [[(300, 700, 14, "Landscape Table Title Text")]], rotate=90))
    build_elements(store, doc.id, "docling")
    pdf = pdfium.PdfDocument(str(doc.path))
    line = page_lines(pdf[0], 1).lines[0]
    pdf.close()
    e = store.db.execute("SELECT * FROM elements WHERE document_id = ? AND code != 'R'", (doc.id,)).fetchone()
    assert e is not None
    for got, want in zip((e["x0"], e["y0"], e["x1"], e["y1"]), (line.x0, line.y0, line.x1, line.y1)):
        assert abs(got - want) < 8


@pytest.mark.skipif(not docling_available(), reason="docling is not installed")
def test_docling_backend(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("e.pdf", [PAGE]))
    result = build_elements(store, doc.id, "docling")
    assert result["backend"] == "docling" and result["elements"] >= 2
    types = {r["code"] for r in store.db.execute("SELECT code FROM elements WHERE document_id = ?", (doc.id,))}
    assert "H" in types or "P" in types
