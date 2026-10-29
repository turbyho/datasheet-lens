"""The three levels: datasheet -> base part -> concrete orderable part."""
import sqlite3

import pytest

from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import SCHEMA_VERSION, Store

LDO = [[(50, 60, 14, "TLV755P 500-mA Low-Dropout Regulator"), (50, 760, 8, "TEXAS INSTRUMENTS")]]


def test_concrete_part_finds_the_datasheet_of_its_base(store_home, text_pdf, tmp_path):
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        base = store.add_part("TLV755P", "TI")
        store.link(base, doc.id)
        concrete = store.add_part("TLV75533PDBVR", "TI")
        store.set_base(concrete, base)
        assert [r["filename"] for r in store.part_documents("TLV75533PDBVR")] == ["tlv755p.pdf"]
        assert [r["filename"] for r in store.documents_of_part(concrete)] == ["tlv755p.pdf"]
        assert store.base_of(concrete)["mpn"] == "TLV755P"
        with pytest.raises(LensError, match="its own base"):
            store.set_base(base, base)
        other = store.add_part("TLV75518PDQNR", "TI")
        with pytest.raises(LensError, match="is a concrete part"):
            store.set_base(other, concrete)

        project = prj.init_project(tmp_path / "board")
        part = prj.add_part(project, store, "TLV75533PDBVR", base="TLV755P")
        assert part["base"] == "TLV755P" and "variants" not in part
        assert [d["file"] for d in part["documents"]] == ["TI/tlv755p.pdf"]
    text = project.path.read_text(encoding="utf-8")
    assert 'mpn = "TLV75533PDBVR"' in text and 'base = "TLV755P"' in text
    assert prj.load(project.root).parts[0]["base"] == "TLV755P"


def test_add_with_an_orderable_mpn(store_home, text_pdf, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    project = prj.init_project(tmp_path / "board")
    path = text_pdf("tlv755p.pdf", LDO)
    assert main(["add", str(path), "--project", str(project.root), "--yes", "--orderable", "TLV75533PDBVR"]) == 0
    assert "project part: TLV75533PDBVR (base part TLV755P)" in capsys.readouterr().out
    part = prj.load(project.root).parts[0]
    assert (part["mpn"], part["base"]) == ("TLV75533PDBVR", "TLV755P")
    assert (project.folder_path / "Texas Instruments/tlv755p.pdf").is_file()


def test_store_migration_from_version_1(tmp_path):
    """A store of version 1 (no base_id) gets the column, and keeps its data."""
    home = tmp_path / "old"
    (home / "db").mkdir(parents=True)
    db = sqlite3.connect(home / "db" / "lens.db")
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE parts (id INTEGER PRIMARY KEY, mpn TEXT NOT NULL COLLATE NOCASE,"
        " manufacturer TEXT NOT NULL DEFAULT '' COLLATE NOCASE, family TEXT NOT NULL DEFAULT '',"
        " created TEXT NOT NULL, UNIQUE (mpn, manufacturer));"
        "INSERT INTO parts (mpn, manufacturer, created) VALUES ('OPA2134', 'TI', '2026-10-28');")
    db.commit()
    db.close()
    with Store(home) as store:
        assert store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(
            SCHEMA_VERSION)
        part_id = store.find_part("OPA2134")
        assert store.base_of(part_id) is None
        store.set_base(store.add_part("OPA2134PA", "TI"), part_id)
    # A newer store than this version: a clear error.
    db = sqlite3.connect(home / "db" / "lens.db")
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="install a newer one"):
        Store(home)


def test_base_from_a_project_file_of_a_team(store_home, text_pdf, tmp_path, monkeypatch):
    """A project from a different machine: sync puts the base part of the file
    into the new store, links the document to the base part, and keeps base."""
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        store.link(store.add_part("TLV755P", "TI"), doc.id)
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TLV75533PDBVR", base="TLV755P")
        prj.sync(project, store)
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path / "store2"))
    for _ in range(2):
        with Store() as store:
            prj.sync(prj.load(project.root), store)
    with Store() as store:
        concrete = store.find_part("TLV75533PDBVR")
        assert store.base_of(concrete)["mpn"] == "TLV755P"
        assert [r["filename"] for r in store.part_documents("TLV755P")] == ["tlv755p.pdf"]
    assert prj.load(project.root).parts[0]["base"] == "TLV755P"


def test_base_rules(store_home, text_pdf):
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        base, concrete, family = (store.add_part(m, "TI") for m in ("TLV755P", "TLV75533PDBVR", "TLV75x"))
        store.set_base(concrete, base)
        with pytest.raises(LensError, match="is the base part of other parts"):
            store.set_base(base, family)  # no third level
        # The role of the link of the concrete part wins.
        store.link(base, doc.id, "datasheet")
        store.link(concrete, doc.id, "errata")
        assert [r["role"] for r in store.documents_of_part(concrete)] == ["errata"]


def test_no_second_bom_line_for_the_base(store_home, text_pdf, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    project = prj.init_project(tmp_path / "board")
    root = str(project.root)
    assert main(["add", str(text_pdf("tlv755p.pdf", LDO)), "--project", root, "--yes",
                 "--orderable", "TLV75533PDBVR"]) == 0
    # A new revision of the datasheet, with no concrete MPN: the concrete part
    # gets it, and the base part is not a second part of the project.
    assert main(["add", str(text_pdf("tlv755p-revb.pdf", LDO)), "--project", root, "--part", "TLV755P",
                 "--yes"]) == 0
    assert "TLV75533PDBVR (base part TLV755P) get the document" in capsys.readouterr().out
    parts = prj.load(project.root).parts
    assert [p["mpn"] for p in parts] == ["TLV75533PDBVR"] and len(parts[0]["documents"]) == 2


def test_migration_by_two_processes(tmp_path):
    """Two stores that open one version 1 store: the second one does not fail."""
    home = tmp_path / "old"
    (home / "db").mkdir(parents=True)
    db = sqlite3.connect(home / "db" / "lens.db")
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE parts (id INTEGER PRIMARY KEY, mpn TEXT NOT NULL COLLATE NOCASE,"
        " manufacturer TEXT NOT NULL DEFAULT '' COLLATE NOCASE, family TEXT NOT NULL DEFAULT '',"
        " created TEXT NOT NULL, UNIQUE (mpn, manufacturer));")
    db.commit()
    db.close()
    first, second = Store(home), Store(home)
    try:
        assert second.db.execute("SELECT value FROM meta").fetchone()[0] == str(SCHEMA_VERSION)
    finally:
        first.close()
        second.close()


def test_concrete_part_gets_the_manufacturer_of_its_base(store_home, text_pdf, tmp_path):
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        store.link(store.add_part("TLV755P", "TI"), doc.id)
        project = prj.init_project(tmp_path / "board")
        part = prj.add_part(project, store, "TLV75533PDBVR", base="TLV755P")
        assert part["manufacturer"] == "TI"
        assert store.part(store.find_part("TLV75533PDBVR"))["manufacturer"] == "TI"
        assert prj.sync(project, store)["moved"] == []  # the copy stays in TI/
    assert part["documents"][0]["file"] == "TI/tlv755p.pdf"


def test_sync_gives_a_concrete_part_the_manufacturer_of_its_base(store_home, text_pdf, tmp_path):
    """A concrete part that came with no manufacturer (an older version): sync
    takes the manufacturer of its base part, in the store and in the file."""
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        base = store.add_part("TLV755P", "TI")
        store.link(base, doc.id)
        concrete = store.add_part("TLV75533PDBVR", "")
        store.set_base(concrete, base)
        project = prj.init_project(tmp_path / "board", layout="")
        project.parts.append({"mpn": "TLV75533PDBVR", "manufacturer": "", "base": "TLV755P", "documents": []})
        prj.save(project)
        prj.sync(prj.load(project.root), store)
        assert store.part(concrete)["manufacturer"] == "TI"
    assert prj.load(project.root).parts[0]["manufacturer"] == "TI"


def test_base_part_note(store_home, text_pdf, tmp_path, capsys, monkeypatch):
    """A project part that is probably the base part of its datasheet gets a note
    with the orderable codes of the text, or with the hint to read the scheme."""
    from datasheet_lens.identify import base_note
    from datasheet_lens.indexer import build_index

    pages = [[(50, 60, 14, "ADC34J24 Quad-Channel ADC")],
             [(50, 40, 9, "Order the ADC34J24EVM board."), (50, 60, 9, "ADC34J24IRGZR  Active  VQFN (RGZ) 48"),
              (50, 80, 9, "ADC34J24IRGZT.A  Active  VQFN 48")]]
    with Store() as store:
        doc = store.add_document(text_pdf("adc34j24.pdf", pages))
        build_index(store, doc.id)  # the codes come from the text of the index
        base = store.add_part("ADC34J24", "TI")
        store.link(base, doc.id)
        note = base_note(store, base, "ADC34J24", "add {code} --base {base}")
        assert "ADC34J24IRGZR (p.2), ADC34J24IRGZT (p.2)" in note and "EVM" not in note and "add ADC34J24IRGZR --base ADC34J24" in note
        # A scheme only (no list of codes): the hint to read the original.
        ldo = store.add_document(text_pdf("mcp1827.pdf", [[(50, 60, 14, "MCP1827 1.5A LDO")]]))
        mcp = store.add_part("MCP1827", "Microchip")
        store.link(mcp, ldo.id)
        assert "no list of codes" in base_note(store, mcp, "MCP1827", "{code}")
        # A concrete part: no note.
        concrete = store.add_part("ADC34J24IRGZR", "TI")
        store.set_base(concrete, base)
        assert base_note(store, concrete, "ADC34J24IRGZR", "{code}") == ""

    project = prj.init_project(tmp_path / "board")
    assert main(["project", "add-part", "ADC34J24", "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "probably the base part" in out and 'project add-part "ADC34J24IRGZR" --base ADC34J24' in out
    assert "run 'datasheet-lens sync'" in out  # the copy is not in the folder yet


def test_no_second_concrete_part_for_an_other_spelling(store_home, text_pdf, tmp_path):
    with Store() as store:
        doc = store.add_document(text_pdf("tlv755p.pdf", LDO))
        store.link(store.add_part("TLV755P", "TI"), doc.id)
        store.add_part("TLV75533PDBVR", "Texas Instruments")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TLV75533PDBVR", base="TLV755P")
        assert len(store.match_parts("TLV75533PDBVR")) == 1
        # set_base gives a concrete part with no manufacturer the one of its base part.
        concrete = store.add_part("TLV75518PDQNR", "")
        store.set_base(concrete, store.find_part("TLV755P"))
        assert store.part(concrete)["manufacturer"] == "TI"
