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
