"""adopt: a part of a source into the store and the project, with its datasheet
and its purchase source. The data is made up (see test_jlcpcb.py)."""

import contextlib

import pytest
from test_jlcpcb import jlc  # noqa: F401  (the fixture: a JLCPCB database of made-up parts)

from datasheet_lens import adopt as adopt_module
from datasheet_lens import fetch
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store


@pytest.fixture
def downloads(monkeypatch, text_pdf):
    """fetch.downloaded gives a local PDF for a URL; each request is kept. A URL
    with "missing" fails."""
    seen = []

    @contextlib.contextmanager
    def downloaded(url, home):
        seen.append(url)
        if "missing" in url:
            raise LensError(f"download of {url}: HTTP 404")
        yield text_pdf(f"ds{len(seen)}.pdf", [[(50, 60, 12, f"1 Features {len(seen)}")]])

    monkeypatch.setattr(fetch, "downloaded", downloaded)
    monkeypatch.setattr("datasheet_lens.distributors.keys_of", lambda project=None: ({}, []))
    return seen


def test_adopt_by_number(jlc, downloads, tmp_path):  # noqa: F811
    project = prj.init_project(tmp_path / "board")
    result = adopt_module.adopt("jlcpcb", "c1001", category="MOSFET", project=prj.load(project.root))
    text = "\n".join(result["lines"])
    assert text.startswith("FET-A (Maker A) from jlcpcb: C1001, library Extended, package DFN3x3-8")
    assert "datasheet: https://x/FET-A.pdf" in text and downloads == ["https://x/FET-A.pdf"]
    part = prj.load(project.root).parts[0]
    assert part["mpn"] == "FET-A" and part["category"] == "MOSFET" and len(part["documents"]) == 1
    assert part["sources"] == [{"source": "jlcpcb", "number": "C1001", "library": "Extended"}]
    with Store() as store:
        [source] = store.part_sources(store.find_part("FET-A", "Maker A", create=False))
    assert (source["source"], source["number"], source["library"]) == ("jlcpcb", "C1001", "Extended")
    assert source["url"] == "https://jlcpcb.com/partdetail/C1001"
    # A second take: the datasheet of the store, no second download.
    result = adopt_module.adopt("jlcpcb", "C1001", project=prj.load(project.root))
    assert "datasheet: in the store already" in "\n".join(result["lines"]) and len(downloads) == 1


def test_number_or_mpn(jlc, downloads):  # noqa: F811
    """The number is the number of the source; the MPN is a separate input. The
    tool never guesses which one a text is."""
    with pytest.raises(LensError, match="not a JLCPCB number: it is C and digits"):
        adopt_module.adopt("jlcpcb", "FET-B")
    with pytest.raises(LensError, match="not both and not none"):
        adopt_module.adopt("jlcpcb", "C1002", mpn="FET-B")
    with pytest.raises(LensError, match="not both and not none"):
        adopt_module.adopt("jlcpcb")
    result = adopt_module.adopt("jlcpcb", mpn="fet-b")
    assert result["lines"][0].startswith("FET-B (Maker B) from jlcpcb: C1002, library Basic")
    with pytest.raises(LensError, match="no part C9999"):
        adopt_module.adopt("jlcpcb", "C9999")
    with pytest.raises(LensError, match="has no part NO-PART"):
        adopt_module.adopt("jlcpcb", mpn="NO-PART")
    with pytest.raises(LensError, match="source: one of digikey, jlcpcb, mouser"):
        adopt_module.adopt("arrow", "X")
    with pytest.raises(LensError, match="datasheet-lens key digikey"):
        adopt_module.adopt("digikey", "296-1-ND")


def test_base_part_with_no_project(jlc, downloads):  # noqa: F811
    """With no project, the concrete part gets the base part and its datasheet."""
    adopt_module.adopt("jlcpcb", "C1003", base="FET")
    with Store() as store:
        assert [r["mpn"] for r in store.part_documents("FET-C")] == ["FET"]  # the datasheet of the base part
        part_id = store.find_part("FET-C", "Maker C", create=False)
        assert store.part(part_id)["base_id"] == store.find_part("FET", "Maker C", create=False)
        assert store.part_sources(part_id)[0]["number"] == "C1003"


def test_datasheet_url_order(jlc, downloads, monkeypatch):  # noqa: F811
    calls = []

    def others(c, source, project=None):
        calls.append(c["mpn"])
        return ["https://www.lcsc.com/b.pdf", "https://www.ti.com/a.pdf"], []

    monkeypatch.setattr(adopt_module, "_other_urls", others)
    # A URL of the manufacturer: first, and the other sources are asked only when it fails.
    urls = adopt_module.datasheet_urls({"mpn": "X", "datasheet": "https://www.st.com/x.pdf"}, "jlcpcb")
    assert next(urls) == "https://www.st.com/x.pdf" and calls == []
    assert list(urls) == ["https://www.ti.com/a.pdf", "https://www.lcsc.com/b.pdf"] and calls == ["X"]
    # A URL of a distributor: a URL of the manufacturer from a different source comes first.
    urls = adopt_module.datasheet_urls({"mpn": "X", "datasheet": "https://jlcpcb.com/x.pdf"}, "jlcpcb")
    assert list(urls) == ["https://www.ti.com/a.pdf", "https://jlcpcb.com/x.pdf", "https://www.lcsc.com/b.pdf"]
    monkeypatch.setattr(adopt_module, "datasheet_urls",
                        lambda c, s, p=None, n=None: iter(["https://ti.com/missing.pdf", "https://www.lcsc.com/ds.pdf"]))
    text = "\n".join(adopt_module.adopt("jlcpcb", "C1002")["lines"])
    assert "datasheet: https://www.lcsc.com/ds.pdf" in text and "failed before: https://ti.com/missing.pdf" in text
    assert "project: none (not in a project folder)" in text
    monkeypatch.setattr(adopt_module, "datasheet_urls", lambda c, s, p=None, n=None: iter([]))
    result = adopt_module.adopt("jlcpcb", "C1004")
    assert result["document"] is None
    assert "datasheet: NOT ADDED. No source gives a datasheet URL" in "\n".join(result["lines"])


def test_a_new_mpn_removes_the_sources(jlc, downloads, tmp_path):  # noqa: F811
    project = prj.init_project(tmp_path / "board")
    adopt_module.adopt("jlcpcb", "C1001", project=prj.load(project.root))
    with Store() as store:
        part = prj.update_part(prj.load(project.root), store, "FET-A", new_mpn="FET-A2")
    assert "removed the purchase sources of FET-A: jlcpcb C1001" in part["notes"][0]
    assert "sources" not in prj.load(project.root).parts[0]


def test_cli(jlc, downloads, tmp_path, capsys):  # noqa: F811
    project = prj.init_project(tmp_path / "board")
    assert main(["adopt", "jlcpcb", "--mpn", "FET-B", "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "FET-B (Maker B) from jlcpcb: C1002, library Basic" in out
    loaded = prj.load(project.root).parts[0]
    assert loaded["sources"][0]["number"] == "C1002"
    assert (project.root / "datasheets" / loaded["documents"][0]["file"]).is_file()  # the CLI syncs
    assert main(["adopt", "jlcpcb", "-p", str(project.root)]) == 2


def test_review_fixes(jlc, downloads, tmp_path, monkeypatch):  # noqa: F811
    """A datasheet of a different manufacturer with the same MPN is not used
    again; a file that is not a readable PDF gives the next URL; a project file
    that a different tool changed during the download is read again."""
    with Store() as store:  # FET-A of a second manufacturer, with its own datasheet
        doc = store.add_document(fetch_pdf(tmp_path), source_url="https://other.example/fet-a.pdf")
        store.link_parts(doc.id, "FET-A", [], "Other Maker", "datasheet")
    assert main(["maker", "distinct", "Maker A", "Other Maker"]) == 0  # a second source of FET-A
    project = prj.init_project(tmp_path / "board")
    loaded = prj.load(project.root)
    with Store() as store:  # a different tool changes the project file after adopt read it
        prj.add_part(prj.load(project.root), store, "OTHER-PART")
    result = adopt_module.adopt("jlcpcb", "C1001", project=loaded)
    assert "datasheet: https://x/FET-A.pdf" in "\n".join(result["lines"])  # not the datasheet of Other Maker
    assert sorted(p["mpn"] for p in prj.load(project.root).parts) == ["FET-A", "OTHER-PART"]
    assert result["project"].parts is not loaded.parts

    calls = []

    @contextlib.contextmanager
    def broken_then_good(url, home):
        calls.append(url)
        path = tmp_path / f"f{len(calls)}.pdf"
        path.write_bytes(b"%PDF-1.4 broken" if len(calls) == 1 else fetch_pdf(tmp_path, "good.pdf").read_bytes())
        yield path

    monkeypatch.setattr(fetch, "downloaded", broken_then_good)
    monkeypatch.setattr(adopt_module, "datasheet_urls",
                        lambda c, s, p=None, n=None: iter(["https://ti.com/broken.pdf", "https://www.lcsc.com/good.pdf"]))
    text = "\n".join(adopt_module.adopt("jlcpcb", "C1004")["lines"])
    assert "datasheet: https://www.lcsc.com/good.pdf" in text and "failed before: https://ti.com/broken.pdf" in text


def fetch_pdf(tmp_path, name="other.pdf"):
    """A small PDF with one page of text."""
    import pypdfium2 as pdfium

    path = tmp_path / name
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 200)
    pdf.save(str(path))
    return path


def test_datasheets_of_other_sources_only_of_the_same_manufacturer(jlc, downloads, monkeypatch):  # noqa: F811
    """An MPN can have parts of more manufacturers (a second source, a copy),
    each with its own datasheet: a different source gives only the URLs of the
    parts of the same manufacturer (a live call, 2026-11-07: a Mouser part got
    the first JLCPCB URL of its MPN, which can be the URL of a copy)."""
    from datasheet_lens import distributors

    offers = [distributors.Offer("Mouser", "FET-A", "Maker A", datasheet_url="https://a.example/fet-a.pdf"),
              distributors.Offer("Mouser", "FET-A", "Maker Z", datasheet_url="https://z.example/fet-a.pdf"),
              distributors.Offer("Mouser", "FET-A", "", datasheet_url="https://unknown.example/fet-a.pdf")]
    monkeypatch.setattr(distributors, "keys_of", lambda project=None: ({"mouser": "key"}, []))
    monkeypatch.setattr(distributors, "offers_of", lambda name, mpn, exact, cred: offers)
    # The JLCPCB library has FET-A of Maker A (https://x/FET-A.pdf).
    assert adopt_module._other_urls({"mpn": "FET-A", "manufacturer": "maker a"}, "digikey") == (
        ["https://a.example/fet-a.pdf", "https://x/FET-A.pdf"], ["Mouser: Maker Z"])
    assert adopt_module._other_urls({"mpn": "FET-A", "manufacturer": "Maker Z"}, "digikey") == (
        ["https://z.example/fet-a.pdf"], ["Mouser: Maker A", "JLCPCB: Maker A"])
    assert adopt_module._other_urls({"mpn": "FET-A", "manufacturer": ""}, "digikey") == ([], [])
    # The result names the URLs that the tool did not use, for the user to decide.
    # A part of Mouser with a datasheet URL at Mouser: the other sources are asked.
    monkeypatch.setattr(distributors, "keys_of", lambda project=None: ({"digikey": "key"}, []))
    monkeypatch.setattr(distributors, "offers_of", lambda name, mpn, exact, cred: [
        distributors.Offer("DigiKey", "FET-A", "Maker Z", datasheet_url="https://z.example/fet-a.pdf")])
    monkeypatch.setattr(adopt_module, "_part", lambda source, number, mpn: {
        "mpn": "FET-A", "manufacturer": "Maker A", "package": "", "number": "1-FET-A", "library": "",
        "url": "https://www.mouser.com/p/1", "datasheet": "https://www.mouser.com/ds/fet-a.pdf"})
    text = "\n".join(adopt_module.adopt("mouser", "1-FET-A")["lines"])
    assert "datasheet: https://x/FET-A.pdf" in text  # the URL of Maker A in the JLCPCB library
    assert ("datasheet URLs not used: the parts of other manufacturers with the MPN FET-A: DigiKey: Maker Z. When "
            "a name is a different name of Maker A, tell the user: record it with manufacturer_relation") in text
    # An offer with no number of the source cannot be a purchase source.
    with pytest.raises(LensError, match="Mouser gives no part number for FET-A"):
        adopt_module._one([distributors.Offer("Mouser", "FET-A", "Maker A")], "FET-A", "Mouser")


def test_texts_of_the_result(jlc, downloads, monkeypatch):  # noqa: F811
    """Each part to select from is in the list. The line of a failed URL gives
    the URL one time, and the sentence ends before the next one."""
    from datasheet_lens import distributors

    offers = [distributors.Offer("Mouser", "X", f"Maker {i}", distributor_pn=f"{i}-X") for i in range(13)]
    with pytest.raises(LensError) as e:
        adopt_module._one(offers, "X", "Mouser")
    assert "0-X (X, Maker 0); " in str(e.value) and str(e.value).endswith("12-X (X, Maker 12)")

    @contextlib.contextmanager
    def html(url, home):
        raise LensError(f"{url} did not give a PDF (text/html): download the file by hand")
        yield  # pragma: no cover

    monkeypatch.setattr(fetch, "downloaded", html)
    monkeypatch.setattr(adopt_module, "datasheet_urls", lambda c, s, p=None, n=None: iter(["https://ti.com/x"]))
    text = "\n".join(adopt_module.adopt("jlcpcb", "C1004")["lines"])
    assert ("datasheet: NOT ADDED. The URLs that failed: https://ti.com/x: did not give a PDF (text/html): download "
            "the file by hand. Find the datasheet") in text
