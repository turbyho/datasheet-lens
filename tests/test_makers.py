import asyncio

import pytest

from datasheet_lens import makers
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.distributors import Offer, _of_maker
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store


def test_key():
    """Two spellings of one name are the same manufacturer with no registry."""
    assert makers.same(None, "Microchip", "Microchip Inc.")
    assert makers.same(None, "Murata Manufacturing Co., Ltd.", "MURATA MANUFACTURING")
    assert makers.key("Würth Elektronik GmbH") == "würthelektronik"
    assert makers.key("  ") == ""
    assert not makers.same(None, "TI", "Texas Instruments")


def test_registry(store_home):
    with Store() as store:
        db = store.db
        # The names of MANUFACTURERS are aliases of one manufacturer.
        assert makers.same(db, "Microchip", "MICROCHIP TECHNOLOGY INC.")
        assert makers.same(db, "ROHM Semiconductor", "ROHM")
        assert makers.same(db, "ti", "TEXAS INSTRUMENTS Inc.")  # a short name of SHORT_NAMES
        assert not makers.same(db, "TXN", "Texas Instruments")
        assert not makers.same(db, "TI", "")
        store.maker_relation("alias", "TXN", "Texas Instruments")
        assert makers.same(db, "TXN", "TI")
        store.maker_relation("distinct", "TI", "onsemi")
        assert makers.distinct(db, "Texas Instruments", "ON Semiconductor")
        with pytest.raises(LensError, match="two manufacturers"):
            store.maker_relation("alias", "onsemi", "TI")
        with pytest.raises(LensError, match="one manufacturer in the registry"):
            store.maker_relation("distinct", "TI", "Texas Instruments")
        with pytest.raises(LensError, match="relation must be one of"):
            store.maker_relation("successor", "A", "B")
        lines = makers.listing(db)
    assert "Texas Instruments = TI, TI.COM, TXN; not the same as: onsemi" in lines


def test_alias_merges_two_manufacturers(store_home):
    """The user tells that two manufacturers of the registry are one: their names
    and their distinct pairs go to one manufacturer."""
    with Store() as store:
        db = store.db
        store.maker_relation("distinct", "Fairchild", "TI")
        store.maker_relation("distinct", "Nexperia", "onsemi")
        store.maker_relation("alias", "Fairchild", "onsemi")  # two manufacturers of the registry
        assert makers.same(db, "Fairchild", "ON Semiconductor")
        assert makers.distinct(db, "onsemi", "TI") and makers.distinct(db, "Fairchild", "Nexperia")


def test_add_part_rules(store_home):
    with Store() as store:
        lm = store.add_part("LM358", "Texas Instruments")
        assert store.add_part("LM358", "TEXAS INSTRUMENTS INC.") == lm
        assert store.add_part("LM358", "TI") == lm
        # A different name that the registry does not know: an error, no new part.
        with pytest.raises(LensError, match='alias "TXN" "Texas Instruments"'):
            store.add_part("LM358", "TXN")
        assert len(store.match_parts("LM358")) == 1
        # One manufacturer: the same part.
        store.maker_relation("alias", "TXN", "Texas Instruments")
        assert store.add_part("LM358", "TXN") == lm
        # A second source: a new part.
        store.maker_relation("distinct", "ROHM", "TI")
        rohm = store.add_part("LM358", "ROHM Semiconductor")
        assert rohm != lm and store.find_part("LM358", "TI") == lm and store.find_part("LM358", "ROHM") == rohm
        with pytest.raises(LensError, match="not unique.*give the manufacturer"):
            store.find_part("LM358")


def test_known_maker(store_home):
    """A proposal from a PDF takes the name of the part that the store has."""
    with Store() as store:
        store.add_part("OPA2134", "TXN")
        assert store.known_maker("opa2134", "Texas Instruments") == "TXN"
        assert store.known_maker("OPA1612", "Texas Instruments") == "Texas Instruments"
        # A second source keeps the name of its PDF.
        store.maker_relation("distinct", "TXN", "STMicroelectronics")
        assert store.known_maker("OPA2134", "STMicroelectronics") == "STMicroelectronics"


def test_base_part_gets_the_manufacturer(store_home):
    with Store() as store:
        base = store.add_part("MCP1827S")
        concrete = store.add_part("MCP1827S-3302E/EBVAO", "Microchip")
        store.set_base(concrete, base)
        assert store.part(base)["manufacturer"] == "Microchip"


def _old_store(make_pdf) -> int:
    """A store of an older version with the duplicate parts of a real store: TI
    and Texas Instruments for one document, Microchip and Microchip Technology
    Inc., and a base part with no manufacturer. Give the ID of the TMP112D
    document."""
    with Store() as store:
        tmp = store.add_document(make_pdf("tmp112d.pdf"))
        mcp = store.add_document(make_pdf("mcp1827.pdf", pages=3))
        db = store.db
        rows = [(15, "TMP112D", "TI", None), (43, "TMP112D0IDPWR", "TI", 15),
                (54, "TMP112D", "Texas Instruments", None), (55, "TMP112D0", "Texas Instruments", 54),
                (60, "TMP112D0IDPWR", "Texas Instruments", 54),
                (10, "MCP1827S", "", None), (9, "MCP1827S-3302E/EBVAO", "Microchip", 10),
                (11, "MCP1827S-3302E/EBVAO", "Microchip Technology Inc.", None),
                (70, "OPA2134", "Burr-Brown", None), (71, "OPA2134", "TI", None)]
        for pid, mpn, maker, _ in rows:  # not through add_part: the old version made these rows
            db.execute("INSERT INTO parts (id, mpn, manufacturer, created) VALUES (?, ?, ?, '2026-10-29')",
                       (pid, mpn, maker))
        for pid, *_, base in rows:
            db.execute("UPDATE parts SET base_id = ? WHERE id = ?", (base, pid))
        for pid, doc in ((15, tmp), (54, tmp), (10, mcp), (9, mcp), (11, mcp), (70, tmp), (71, tmp)):
            db.execute("INSERT INTO part_documents (part_id, document_id) VALUES (?, ?)", (pid, doc.id))
        db.execute("INSERT INTO part_aliases VALUES (54, 'TMP112-D')")
        db.execute("INSERT INTO knowledge (part_id, document_id, page, subject, value, source_kind, created)"
                   " VALUES (54, ?, 1, 'accuracy', '0.1', 'model', '2026-10-29')", (tmp.id,))
        for table in ("manufacturer_distinct", "manufacturer_aliases", "manufacturers"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM meta WHERE key = ?", (makers.SEED_KEY,))
        db.commit()
        return tmp.id


def test_registry_update_of_an_old_store(store_home, make_pdf):
    """The first open with the registry: the names of one manufacturer in the
    built-in tables merge (TI and Texas Instruments, Microchip and Microchip
    Technology Inc.), with their links, aliases, facts and concrete parts. The
    base part gets a manufacturer. An unknown pair (Burr-Brown and TI) is only a
    proposal. The schema version stays: an older version opens the store."""
    tmp = _old_store(make_pdf)
    with Store() as store:
        db = store.db
        assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
        parts = {r["id"]: r for r in db.execute("SELECT * FROM parts")}
        assert sorted(parts) == [9, 10, 15, 43, 55, 70, 71]
        assert parts[10]["manufacturer"] == "Microchip"
        assert parts[43]["base_id"] == 15 and parts[55]["base_id"] == 15
        assert [r["id"] for r in store.part_documents("TMP112D0IDPWR")] == [tmp]
        assert store.find_part("TMP112-D") == 15
        assert db.execute("SELECT part_id FROM knowledge").fetchone()[0] == 15
        assert any(line.startswith("Burr-Brown and TI: the parts OPA2134") for line in store.maker_listing())

        # The user confirms the proposal: the parts merge.
        assert store.maker_relation("alias", "Burr-Brown", "TI") == ["OPA2134: TI into Burr-Brown"]
        assert not any("Burr-Brown and TI" in line for line in store.maker_listing())


def test_merge_keeps_two_levels(store_home):
    """A merge does not make a chain of three parts: the concrete parts of the
    merged base part go to the base part of the part that stays."""
    with Store() as store:
        db = store.db
        store.maker_relation("distinct", "AA", "BB")
        base = store.add_part("X100", "AA")
        keep = store.add_part("X100-Q", "AA")
        store.set_base(keep, base)
        dup = store.add_part("X100-Q", "BB")
        child = store.add_part("X100-QR", "BB")
        store.set_base(child, dup)
        db.execute("DELETE FROM manufacturer_distinct")
        store.maker_relation("alias", "BB", "AA")
        assert store.part(child)["base_id"] == base and store.part(dup) is None


def test_project_part_with_an_other_name(store_home, make_pdf, tmp_path):
    """The project file keeps its name of the manufacturer (the layout uses it as
    a folder): a store part with a different name of the same manufacturer is
    the same part, not a second project part."""
    project = prj.init_project(tmp_path / "board")
    with Store() as store:
        doc = store.add_document(make_pdf("tps7a02.pdf"))
        store.maker_relation("alias", "TI", "Texas Instruments")
        store.link(store.add_part("TPS7A02", "TI"), doc.id)  # the store has the name TI
        store.set_base(store.add_part("TPS7A0218PYCHR", "TI"), store.find_part("TPS7A02"))
        prj.add_part(project, store, "TPS7A0218PYCHR", "Texas Instruments", doc.sha256, base="TPS7A02")
        prj.add_part(project, store, "TPS7A0218PYCHR", "TI", doc.sha256, base="TPS7A02")
        assert [(p["mpn"], p["manufacturer"]) for p in project.parts] == [("TPS7A0218PYCHR", "Texas Instruments")]
        assert len(store.match_parts("TPS7A02")) == 1 and len(store.match_parts("TPS7A0218PYCHR")) == 1
        assert not prj.sync(project, store)["problems"]
        part = prj.update_part(project, store, "TPS7A0218PYCHR", "TI", category="LDO")
        assert part["manufacturer"] == "Texas Instruments"
        assert (project.folder_path / "Texas Instruments" / "tps7a02.pdf").is_file()
        prj.remove_part(project, "TPS7A0218PYCHR", "TI", store=store)
    assert project.parts == []


def test_offers_of_the_manufacturer():
    ti, rohm = Offer("Mouser", "LM358DR", "Texas Instruments"), Offer("Mouser", "LM358DR", "ROHM Semiconductor")
    assert _of_maker([ti, rohm], "texas instruments inc.") == ([ti], True)
    assert _of_maker([ti, rohm], "Unknown Co") == ([ti, rohm], False)
    assert _of_maker([ti, rohm], "") == ([ti, rohm], False)
    # One manufacturer: no comparison of the names (each distributor has its own form).
    adi = Offer("Mouser", "MAX20361CEWC+T", "Analog Devices / Maxim Integrated")
    assert _of_maker([adi], "Analog Devices") == ([adi], True)


def test_cli_and_mcp(store_home, capsys):
    from datasheet_lens import server

    assert main(["maker", "alias", "TI", "Texas Instruments"]) == 0
    assert "recorded: alias 'TI' 'Texas Instruments'" in capsys.readouterr().out
    assert main(["maker"]) == 0
    assert "Texas Instruments = TI, TI.COM" in capsys.readouterr().out
    assert main(["maker", "distinct", "TI", "TI"]) == 2
    assert "one manufacturer in the registry" in capsys.readouterr().err

    result = asyncio.run(server.mcp.call_tool("manufacturer_relation", {
        "relation": "distinct", "name": "ROHM", "other": "TI"}))
    blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    assert "recorded: distinct" in "\n".join(getattr(b, "text", "") for b in blocks)
