import ctypes

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "store"
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(home))
    return home


@pytest.fixture
def make_pdf(tmp_path):
    """Make a small PDF with blank pages (no third-party document is necessary)."""

    def make(name="test.pdf", pages=2):
        pdf = pdfium.PdfDocument.new()
        for _ in range(pages):
            pdf.new_page(595, 842)
        path = tmp_path / name
        pdf.save(str(path))
        pdf.close()
        return path

    return make


@pytest.fixture
def text_pdf(tmp_path):
    """Make a PDF with text lines on its pages (pages: a list of lists of
    (x, y_from_top, size, text)), and optional bookmarks."""

    def make(name, pages, height=842, rotate=0):
        """rotate=90: the page has /Rotate 90, and the text is drawn turned by 90°
        in user space, thus it is horizontal on the shown page (a landscape page).
        x and y are then positions in user space."""
        pdf = pdfium.PdfDocument.new()
        for lines in pages:
            page = pdf.new_page(595, height)
            for x, y, size, text in lines:
                # The raw pdfium API: pypdfium2 has no helper to make a text object.
                obj = pdfium_c.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", float(size))
                wide = ctypes.create_string_buffer((text + "\0").encode("utf-16-le"))
                pdfium_c.FPDFText_SetText(obj, ctypes.cast(wide, ctypes.POINTER(pdfium_c.FPDF_WCHAR)))
                if rotate == 90:
                    pdfium_c.FPDFPageObj_Transform(obj, 0, 1, -1, 0, float(x), float(height - y))
                else:
                    pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, float(x), float(height - y))
                pdfium_c.FPDFPage_InsertObject(page.raw, obj)
            pdfium_c.FPDFPage_GenerateContent(page.raw)
            if rotate:
                page.set_rotation(rotate)
        path = tmp_path / name
        pdf.save(str(path))
        pdf.close()
        return path

    return make
