"""The fields Manufacturer and Distributor of the CAD project (decisions of the
user, 2026-11-08): the manufacturers of the MPNs of the field MPN, and the
preferred distributor of the fitted part."""

from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.store import Store


def test_the_columns_of_a_bom(tmp_path):
    """The BOM reader finds the columns by their names (also MFR and Supplier);
    the field to write is the column of the BOM. Two columns of one role with
    different texts are a conflict."""
    bom = tmp_path / "bom.csv"
    bom.write_text("Reference,Value,MPN,MFR,Manufacturer_Name,Supplier\n"
                   "Q1,2N7002,\"2N7002LT1G, BSS138LT1G\",\"onsemi, onsemi\",,LCSC\n"
                   "Q2,2N7002,2N7002LT1G,onsemi,Nexperia,\n", encoding="utf-8")
    parts = prj.bom_parts(str(bom))
    assert "Manufacturer from MFR, Manufacturer_Name; Distributor from Supplier" in parts.summary
    q1, q2 = parts.cad
    assert (q1.manufacturer, q1.manufacturer_fields) == ("onsemi, onsemi", ["MFR", "Manufacturer_Name"])
    assert (q1.distributor, q1.distributor_fields) == ("LCSC", ["Supplier"]) and q1.conflict == ""
    assert "MFR onsemi, Manufacturer_Name Nexperia" in q2.conflict
    # No such columns: None, and the default fields to write.
    bom.write_text("Reference,Value,MPN\nQ1,2N7002,2N7002LT1G\n", encoding="utf-8")
    [q1] = prj.bom_parts(str(bom)).cad
    assert (q1.manufacturer, q1.manufacturer_fields, q1.distributor, q1.distributor_fields) == (
        None, ["Manufacturer"], None, ["Distributor"])


def _project(store, tmp_path):
    project = prj.init_project(tmp_path / "board")
    for mpn, maker in (("2N7002LT1G", "onsemi"), ("BSS138LT1G", "onsemi"), ("BAT54S", "Nexperia"),
                       ("XYZ1", ""), ("C1", "YAGEO"), ("C2", "YAGEO")):
        prj.add_part(project, store, mpn, maker)
    prj.set_source(project, project.find("2N7002LT1G"), "jlcpcb", "C8545")
    prj.set_source(project, project.find("BAT54S"), "digikey", "BAT54S-ND")
    prj.set_source(project, project.find("BAT54S"), "mouser", "771-BAT54S")
    prj.set_source(project, project.find("C2"), "mouser", "603-C2")
    return prj.load(project.root)


def test_the_findings_of_the_fields(store_home, tmp_path):
    with Store() as store:
        project = _project(store, tmp_path)
        store.maker_relation("alias", "ON Semiconductor", "onsemi")
        cad = [
            # The fields agree: the manufacturers in the order of the MPNs, the channel of the source.
            {"reference": "Q1", "mpn": "2N7002LT1G, BSS138LT1G", "manufacturer": "onsemi, onsemi",
             "distributor": "lcsc"},
            # No fields: write them.
            {"reference": "Q2", "mpn": "2N7002LT1G"},
            # The same manufacturer with an other name; a part with more channels.
            {"reference": "D1", "mpn": "BAT54S", "manufacturer": "Nexperia"},
            {"reference": "Q3", "mpn": "2N7002LT1G", "manufacturer": "ON Semiconductor", "distributor": "Mouser"},
            # A different manufacturer; a distributor of the BOM for a part with no channel.
            {"reference": "C1", "mpn": "C1", "manufacturer": "Samsung", "distributor": "TME"},
            # No manufacturer in the project; a part with no channel and no distributor.
            {"reference": "U1", "mpn": "XYZ1", "manufacturer": ""},
        ]
        result = prj.check_parts(project, store, cad)
        kinds = [(f["reference"], f["kind"]) for f in result["fields"]]
        assert kinds == [("Q2", "maker_write"), ("Q2", "distributor_write"), ("D1", "distributor_select"),
                         ("Q3", "maker_names"), ("Q3", "distributor_different"), ("C1", "maker_different"),
                         ("C1", "distributor_save"), ("U1", "maker_unknown"), ("U1", "distributor_ask")]
        assert result["fields_ok"] == 3  # Q1 (two fields), D1 (Manufacturer)
        found = {(f["reference"], f["kind"]): f for f in result["fields"]}
        assert found["D1", "distributor_select"]["choices"] == ["DigiKey", "Mouser"]
        # The choices for a part with no channel: N/A first, then the distributors of the project.
        assert found["U1", "distributor_ask"]["choices"] == ["N/A", "LCSC", "Mouser"]
        text = "\n".join(prj.format_check(result))
        assert ("Manufacturer: Q2: the field has no manufacturer; the project gives onsemi. Write Manufacturer = "
                "'onsemi' into the symbol.") in text
        assert ("Distributor: Q2: the field has no distributor; the preferred distributor of 2N7002LT1G is LCSC. "
                "Write Distributor = 'LCSC' into the symbol.") in text
        assert ("Distributor: D1: BAT54S has more purchase channels (DigiKey, Mouser). Ask the user which one is "
                "the preferred distributor, save it with project_update_part(mpn='BAT54S', distributor='<name>'), "
                "and write it into Distributor of the symbol.") in text
        assert ("Manufacturer: Q3: the field gives 'ON Semiconductor', the datasheets give the same manufacturers "
                "as 'onsemi'. Ask the user, then write Manufacturer = 'onsemi' into the symbol.") in text
        assert ("Manufacturer: C1: the field gives 'Samsung', the project gives 'YAGEO' (the manufacturers of the "
                "datasheets). The CAD BOM is the master, but the manufacturer of a part comes from its datasheet: "
                "ask the user which is correct (the field, or the MPN or its datasheet).") in text
        assert ("Distributor: C1: the field gives 'TME' for C1, and the project has no preferred distributor. The "
                "CAD BOM is the master: save it with project_update_part(mpn='C1', distributor='TME').") in text
        assert ("Distributor: U1: XYZ1 has no purchase channel. Ask the user for the preferred distributor: N/A, "
                "LCSC, Mouser, or a different name.") in text
        assert "next steps" not in text
        cli = "\n".join(prj.format_check(result, cli=True))
        assert "save it with datasheet-lens set \"C1\" -D \"TME\"" in cli


def test_the_preferred_distributor_in_the_project_file(store_home, tmp_path, capsys):
    """The answer of the user goes into the project file (also N/A) and wins
    over the channels; the next check gives no question then."""
    with Store() as store:
        project = _project(store, tmp_path)
        part = prj.update_part(project, store, "BAT54S", distributor="Mouser")
        assert part["changes"] == ["distributor: (none) -> Mouser"]
        prj.update_part(prj.load(project.root), store, "XYZ1", distributor="na")
    text = (tmp_path / "board" / prj.PROJECT_FILE).read_text(encoding="utf-8")
    assert 'distributor = "Mouser"' in text and 'distributor = "N/A"' in text
    project = prj.load(project.root)
    assert prj.preferred_distributor(project.find("BAT54S")) == ("Mouser", ["DigiKey", "Mouser"])
    assert prj.preferred_distributor(project.find("XYZ1")) == ("N/A", [])
    assert prj.project_distributors(project) == ["LCSC", "Mouser"]
    assert main(["set", "C1", "-D", "TME", "-p", str(project.root)]) == 0
    assert "distributor: (none) -> TME" in capsys.readouterr().out
    with Store() as store:
        result = prj.check_parts(prj.load(project.root), store, [
            {"reference": "D1", "mpn": "BAT54S", "manufacturer": "Nexperia", "distributor": "Mouser"},
            {"reference": "U1", "mpn": "XYZ1", "distributor": "N/A"},
            {"reference": "C1", "mpn": "C1", "manufacturer": "YAGEO", "distributor": "TME"}])
    assert [(f["reference"], f["kind"]) for f in result["fields"]] == [("U1", "maker_unknown")]
    assert result["fields_ok"] == 5
