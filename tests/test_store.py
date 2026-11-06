import pytest

from datasheet_lens.cli import main
from datasheet_lens.store import Store, sha256_file


def test_add_document_copies_file_by_hash(store_home, make_pdf):
    src = make_pdf(pages=3)
    store = Store()
    doc = store.add_document(src)
    assert doc.sha256 == sha256_file(src)
    assert doc.pages == 3
    assert doc.path == store_home / "blobs" / doc.sha256[:2] / f"{doc.sha256}.pdf"
    assert doc.path.read_bytes() == src.read_bytes()


def test_same_file_is_stored_once(store_home, make_pdf, tmp_path):
    src = make_pdf()
    copy = tmp_path / "other-name.pdf"
    copy.write_bytes(src.read_bytes())
    store = Store()
    a = store.add_document(src)
    b = store.add_document(copy)
    assert a.id == b.id
    assert len(store.documents()) == 1


def test_document_lookup_by_prefix(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf())
    assert store.document(doc.sha256[:10]).id == doc.id
    assert store.document(str(doc.id)).id == doc.id
    assert store.document("ffffffffffff") is None


def test_part_with_document_set(store_home, make_pdf):
    store = Store()
    ds = store.add_document(make_pdf("ds.pdf", pages=2))
    am = store.add_document(make_pdf("am.pdf", pages=5))
    part = store.add_part("RV-3149-C3", "Micro Crystal")
    store.link(part, ds.id, "datasheet")
    store.link(part, am.id, "manual")
    rows = store.part_documents("rv-3149-c3")
    assert [(r["filename"], r["role"]) for r in rows] == [("ds.pdf", "datasheet"), ("am.pdf", "manual")]


def test_digit_only_prefix_is_a_hash_prefix(store_home, make_pdf):
    store = Store()
    doc = store.add_document(make_pdf())
    # Give the document a hash that starts with 12 digits, as a real hash can.
    store.db.execute("UPDATE documents SET sha256 = ? WHERE id = ?", ("204918337265" + "a" * 52, doc.id))
    assert store.document("204918337265").id == doc.id
    assert store.document(str(doc.id)).id == doc.id


def test_part_is_found_without_case_and_manufacturer(store_home):
    store = Store()
    a = store.add_part("OPA2134", "TI")
    assert store.add_part("opa2134") == a
    assert store.add_part("OPA2134", "ti") == a
    assert len(store.parts()) == 1


def test_damaged_pdf_leaves_no_file(store_home, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7 not a real pdf")
    with pytest.raises(ValueError):
        Store().add_document(bad)
    assert not any((store_home / "blobs").rglob("*.pdf"))


def test_unsupported_file_type(store_home, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        Store().add_document(f)


def test_cli_add_and_list(store_home, make_pdf, capsys):
    assert main(["add", str(make_pdf("opa.pdf")), "--part", "OPA2134", "--manufacturer", "TI"]) == 0
    assert main(["parts"]) == 0
    assert main(["docs", "--part", "OPA2134"]) == 0
    out = capsys.readouterr().out
    assert "OPA2134" in out and "opa.pdf" in out and "datasheet" in out


def test_part_sources(store_home):
    """A purchase source of a part: a new source and number is a new row, the
    same source and number changes the URL, the library class and the date."""
    from datetime import datetime, timezone

    from datasheet_lens.errors import LensError

    with Store() as store:
        part = store.add_part("CSD16340Q3", "TI")
        assert store.part_sources(part) == []
        store.set_part_source(part, "jlcpcb", " C47616 ", library="Extended")
        store.set_part_source(part, "digikey", "296-25646-1-ND", url="https://dk/a", found="2026-11-01")
        today = datetime.now(timezone.utc).date().isoformat()
        assert store.part_sources(part) == [
            {"source": "digikey", "number": "296-25646-1-ND", "url": "https://dk/a", "library": "",
             "found": "2026-11-01"},
            {"source": "jlcpcb", "number": "C47616", "url": "", "library": "Extended", "found": today}]
        store.set_part_source(part, "jlcpcb", "C47616", url="https://jlc/C47616", library="Basic",
                              found="2026-11-02")
        assert store.part_sources(part)[1] == {"source": "jlcpcb", "number": "C47616", "url": "https://jlc/C47616",
                                               "library": "Basic", "found": "2026-11-02"}
        assert store.delete_part_source(part, "digikey", "296-25646-1-ND")
        assert not store.delete_part_source(part, "digikey", "296-25646-1-ND")
        assert [s["source"] for s in store.part_sources(part)] == ["jlcpcb"]
        for args, message in ((("farnell", "X"), "source must be one of"), (("mouser", " "), "is empty"),
                              (("jlcpcb", "C1", "", "Standard"), "library must be one of")):
            with pytest.raises(LensError, match=message):
                store.set_part_source(part, *args)
        with pytest.raises(LensError, match="part not found"):
            store.set_part_source(9999, "mouser", "595-X")
        # The sources go with the part.
        store.db.execute("DELETE FROM parts WHERE id = ?", (part,))
        assert store.db.execute("SELECT count(*) FROM part_sources").fetchone()[0] == 0


def test_part_documents_give_the_manufacturer_of_the_part(store_home, make_pdf):
    """The documents table has its own column manufacturer: the row gives the
    manufacturer of the part as part_manufacturer."""
    with Store() as store:
        doc = store.add_document(make_pdf())
        store.link_parts(doc.id, "FET-A", [], "Maker A", "datasheet")
        [row] = store.part_documents("FET-A")
    assert (row["mpn"], row["part_manufacturer"], row["manufacturer"]) == ("FET-A", "Maker A", "")
