import pypdfium2 as pdfium
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
