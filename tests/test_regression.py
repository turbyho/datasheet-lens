"""Regression tests with the datasheets of the spike (OPA2134, RV-3149-C3,
nRF54LM20A). The expected pages come from the end-to-end tests of the spike,
which a person checked against the original pages.

The PDF files are third-party documents: they are not in the public repository.
A test skips when its file is not present. The large nRF54LM20A manual (1302
pages) and the docling element map run only when DATASHEET_LENS_SLOW=1.
"""

import os
from pathlib import Path

import pytest

from datasheet_lens.elements import build_elements, docling_available, page_map
from datasheet_lens.indexer import build_index, index_markdown, outline_under, search
from datasheet_lens.pages import page_image
from datasheet_lens.store import Store

SPIKE = Path(__file__).resolve().parent.parent / "spike"
DOCS = {
    "opa2134": SPIKE / "opa2134/pdf/opa2134.pdf",
    "rv3149": SPIKE / "rv3149/pdf/rv3149.pdf",
    "rv3149-am": SPIKE / "rv3149/pdf/rv3149-app-manual.pdf",
    "nrf54lm20": SPIKE / "nrf54lm20/pdf/nrf54lm20.pdf",
}
SLOW = os.environ.get("DATASHEET_LENS_SLOW") == "1"


@pytest.fixture(scope="module")
def lens(tmp_path_factory):
    """A store for the spike documents: (store home, {name: document ID}). doc_id()
    adds a document when a test needs it."""
    return tmp_path_factory.mktemp("regression-store"), {}


def doc_id(lens, name):
    """The ID of a spike document, with its index and text element map."""
    home, ids = lens
    if name not in ids:
        if not DOCS[name].is_file():
            pytest.skip(f"{DOCS[name].name}: file not present")
        if name == "nrf54lm20" and not SLOW:
            pytest.skip(f"{DOCS[name].name}: set DATASHEET_LENS_SLOW=1")
        with Store(home) as store:
            doc = store.add_document(DOCS[name])
            build_index(store, doc.id)
            build_elements(store, doc.id, "text")
            ids[name] = doc.id
    return ids[name]


def outline(store, doc):
    return {(r["title"], r["page"]) for r in store.db.execute(
        "SELECT title, page FROM outline WHERE document_id = ?", (doc,))}


def captions(store, doc):
    return {(r["label"], r["caption"], r["page"]) for r in store.db.execute(
        "SELECT label, caption, page FROM captions WHERE document_id = ?", (doc,))}


def pages_of(store, doc, text):
    return {p for _, p, _ in search(store, [doc], text, 1000)}


def test_opa2134(lens):
    doc = doc_id(lens, "opa2134")
    with Store(lens[0]) as store:
        assert store.document(doc).pages == 35
        out = outline(store, doc)
        for entry in [("4 Pin Configuration and Functions", 3), ("5.1 Absolute Maximum Ratings", 5),
                      ("5.7 Electrical Characteristics", 7), ("7.2 Typical Application", 17)]:
            assert entry in out
        caps = captions(store, doc)
        for entry in [
                ("Figure 4-2", "OPA2134: D Package, 8-Pin SOIC, and P Package, 8-Pin PDIP (Top View)", 3),
                ("Table 4-1", "Pin Functions: OPA2134", 3),
                # Two figures have the same caption: the index keeps the two.
                ("Figure 5-1", "Total Harmonic Distortion + Noise vs Frequency", 9),
                ("Figure 5-3", "Total Harmonic Distortion + Noise vs Frequency", 9),
                ("Figure 5-8", "Input-Referred Noise Voltage vs Noise Bandwidth", 10),
                ("Figure 7-1", "OPA134 2nd-Order, 30kHz, Low-Pass Filter Schematic", 17)]:
            assert entry in caps
        assert pages_of(store, doc, "Input voltage noise") == {7}
        assert "5.7 Electrical Characteristics (p.7)" in index_markdown(store, doc)
        # The headings of the page map come from the text layer.
        assert any("Electrical Characteristics" in line for line in page_map(store, doc, 7))
        # A full page fits the maximum long edge of the model.
        img = page_image(store, doc, 7)
        assert max(img.width_px, img.height_px) <= 2576 and img.dpi > 200


def test_rv3149(lens):
    doc = doc_id(lens, "rv3149")
    with Store(lens[0]) as store:
        assert store.document(doc).pages == 2
        assert 2 in pages_of(store, doc, "PIN CONNECTIONS")


def test_rv3149_application_manual(lens):
    doc = doc_id(lens, "rv3149-am")
    with Store(lens[0]) as store:
        assert store.document(doc).pages == 66
        assert ("3.2.1 CONTROL_1 (address 00h…bits description)", 11) in outline(store, doc)
        assert 11 in pages_of(store, doc, "CONTROL_1")
        # A region of a register table at 300 dpi or more.
        assert page_image(store, doc, 11, (40, 100, 560, 400)).dpi >= 300


def test_nrf54lm20(lens):
    doc = doc_id(lens, "nrf54lm20")
    with Store(lens[0]) as store:
        assert store.document(doc).pages == 1302
        under = {(r["title"], r["page"]) for r in outline_under(store, doc, "8.27")}
        assert ("8.27.5.143 DOEPCTL3", 1014) in under
        assert ("Absolute maximum ratings", 1284) in outline(store, doc)
        assert ("Table 93", "Supply voltage", 1284) in captions(store, doc)
        assert 1014 in pages_of(store, doc, "DOEPCTL3")
        assert any("8.27.5.143 DOEPCTL3" in line for line in page_map(store, doc, 1014))
        # A large document: the index gives only the top outline levels.
        assert "8.27.5.143" not in index_markdown(store, doc)


@pytest.mark.skipif(not SLOW or not docling_available(), reason="set DATASHEET_LENS_SLOW=1 (needs docling)")
def test_opa2134_docling(tmp_path):
    if not DOCS["opa2134"].is_file():
        pytest.skip("opa2134.pdf: file not present")
    with Store(tmp_path / "store") as store:
        doc = store.add_document(DOCS["opa2134"]).id
        build_elements(store, doc, "docling")
        lines = page_map(store, doc, 9)
        # Figure 5-1 is a figure element with its caption.
        assert any(" picture " in line or "figure" in line.lower() for line in lines)
        assert any("Figure 5-1" in line and "Total Harmonic Distortion" in line for line in lines)
        assert any("Table" in line or "table" in line for line in page_map(store, doc, 7))
