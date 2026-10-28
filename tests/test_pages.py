import struct
import zlib

import pypdfium2 as pdfium
import pytest

from datasheet_lens.pages import page_image, parse_pages, pdf_pages, region_dpi
from datasheet_lens.store import Store


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def dark_fraction(path):
    """Fraction of dark pixels of an RGB PNG made by pages._png (one IDAT)."""
    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    row = 1 + 3 * w
    dark = sum(1 for y in range(h) for x in range(w)
               if raw[y * row + 1 + 3 * x] < 128)
    return dark / (w * h)


def test_full_page_fits_the_long_edge(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf(pages=2))
    img = page_image(store, doc.id, 1)
    assert png_size(img.path) == (img.width_px, img.height_px)
    assert max(img.width_px, img.height_px) <= 2576
    assert max(img.width_px, img.height_px) >= 2570
    assert img.bbox is None


def test_region_dpi_limits():
    assert region_dpi((0, 0, 100, 50)) == 600          # small: at most 600 dpi
    # Half a pixel of margin for the rounding of pdfium.
    assert region_dpi((0, 0, 400, 200)) == pytest.approx(2575.5 * 72 / 400)  # 463.6 dpi
    assert region_dpi((0, 0, 800, 100)) == pytest.approx(2575.5 * 72 / 800)  # below 300: fits


def test_region_holds_the_text(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("t.pdf", [[(100, 200, 20, "MMMM")]]))
    img = page_image(store, doc.id, 1, bbox=(90, 180, 220, 210))
    assert img.dpi == 600
    assert dark_fraction(img.path) > 0.05
    empty = page_image(store, doc.id, 1, bbox=(300, 500, 400, 550))
    assert dark_fraction(empty.path) == 0


def test_region_on_a_rotated_page(store_home, text_pdf):
    # The text is horizontal on the shown landscape page; the index gives its bbox
    # in the coordinates of the shown page.
    store = Store()
    doc = store.add_document(text_pdf("r.pdf", [[(300, 700, 20, "MMMM")]], rotate=90))
    from datasheet_lens.indexer import build_index
    from datasheet_lens.pdftext import page_lines

    build_index(store, doc.id)
    pdf = pdfium.PdfDocument(str(doc.path))
    line = page_lines(pdf[0], 1).lines[0]
    pdf.close()
    img = page_image(store, doc.id, 1, bbox=(line.x0 - 5, line.y0 - 5, line.x1 + 5, line.y1 + 5))
    assert img.width_px > img.height_px
    assert dark_fraction(img.path) > 0.05


def test_bad_page_and_region(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf(pages=2))
    with pytest.raises(ValueError):
        page_image(store, doc.id, 3)
    with pytest.raises(ValueError):
        page_image(store, doc.id, 1, bbox=(1000, 1000, 1100, 1100))


def test_pdf_pages(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf(pages=6))
    path, pages = pdf_pages(store, doc.id, "2-3,5")
    assert pages == [2, 3, 5]
    pdf = pdfium.PdfDocument(str(path))
    assert len(pdf) == 3
    pdf.close()


def test_cli_reports_a_bad_page(store_home, make_pdf, capsys):
    from datasheet_lens.cli import main

    assert main(["add", str(make_pdf(pages=2))]) == 0
    assert main(["page-image", "1", "9"]) == 2
    assert "page 9 is not in the document" in capsys.readouterr().err


def test_bbox_and_max_px_are_checked(store_home, make_pdf, capsys):
    from datasheet_lens.cli import main
    from datasheet_lens.errors import LensError

    store = Store()
    doc = store.add_document(make_pdf(pages=1))
    with pytest.raises(LensError):
        page_image(store, doc.id, 1, bbox=(1, 2, 3))
    with pytest.raises(LensError):
        page_image(store, doc.id, 1, bbox=(float("nan"), 0, 10, 10))
    with pytest.raises(LensError):
        page_image(store, doc.id, 1, max_px=100000)
    store.close()
    assert main(["page-image", "1", "1", "--bbox", "1,2,3"]) == 2
    assert main(["page-image", "1", "1", "--bbox", "nan,0,10,10"]) == 2
    assert "four finite numbers" in capsys.readouterr().err


def test_cache_key_keeps_near_regions_apart(store_home, text_pdf):
    store = Store()
    doc = store.add_document(text_pdf("t.pdf", [[(100, 200, 20, "MMMM")]]))
    a = page_image(store, doc.id, 1, bbox=(90.4, 180.4, 220.4, 210.4))
    b = page_image(store, doc.id, 1, bbox=(89.6, 179.6, 219.6, 209.6))
    assert a.path != b.path


def test_long_page_list_gives_a_short_name(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf(pages=300))
    path, pages = pdf_pages(store, doc.id, ",".join(str(p) for p in range(1, 301)))
    assert len(pages) == 300 and len(path.name) < 80


def test_parse_pages_errors():
    assert parse_pages("7", 10) == [7]
    for bad in ("0", "5-3", "11", "a", "3-"):
        with pytest.raises(ValueError):
            parse_pages(bad, 10)
