"""project_check and the JLCPCB (LCSC) numbers of the CAD symbols (phase 7 of
plans/parametric-search.md). The data is made up (see test_jlcpcb.py)."""

import sqlite3

from test_jlcpcb import jlc  # noqa: F401  (the fixture: a JLCPCB database of made-up parts)

from datasheet_lens import jlcpcb
from datasheet_lens import project as prj
from datasheet_lens.store import Store


def _symbol(ref: str, mpn: str, lcsc: str | None) -> dict:
    return {"reference": ref, "value": f"FET ({mpn})", "mpn": mpn, "lcsc": lcsc}


def test_lcsc_numbers_of_the_symbols(jlc, tmp_path):  # noqa: F811
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("FET-A", "FET-B", "FET-C", "FET-D", "BJT-E"):
            prj.add_part(project, store, mpn)
        project = prj.load(project.root)
        for i, number in ((0, "C1001"), (2, "C1003"), (3, "C1004")):
            prj.set_source(project, project.parts[i], "jlcpcb", number)
        project = prj.load(project.root)
        cad = [_symbol("Q1", "FET-A", "C1001"),   # agrees with the purchase source
               _symbol("Q2", "FET-B", "c1002"),   # no purchase source in the project
               _symbol("Q3", "FET-C", "C1001"),   # a different number: of FET-A in the library
               _symbol("Q4", "FET-D", ""),        # the field has no number
               _symbol("Q5", "BJT-E", "abc"),     # not a JLCPCB number
               _symbol("Q6", "FET-A", "C9999"),   # the library does not have it
               _symbol("Q7", "BJT-E", None)]      # no field, no purchase source
        result = prj.check_parts(project, store, cad)
    assert [(f["reference"], f["kind"]) for f in result["lcsc"]] == [
        ("Q2", "no_source"), ("Q3", "different"), ("Q3", "other_mpn"), ("Q4", "write"), ("Q5", "not_number"),
        ("Q6", "different"), ("Q6", "not_in_library"), ("Q7", "none")]
    assert result["lcsc_ok"] == 1
    text = "\n".join(prj.format_check(result))
    assert "LCSC: the JLCPCB numbers that agree with the purchase sources: 1" in text
    assert ("LCSC: Q2: the field gives C1002 for FET-B, and the project has no JLCPCB purchase source. The BOM is "
            "the master: save it with adopt_part(source='jlcpcb', number='C1002').") in text
    # C1001 of the field is FET-A in the library: the BOM cannot be the master, the user decides.
    assert ("LCSC: Q3: the field gives C1001, the JLCPCB purchase source of FET-C is C1003, and the library does not "
            "give C1001 for FET-C (see the next line). The BOM cannot be the master here: ask the user") in text
    assert "LCSC: Q3: C1001 is FET-A (Maker A) in the JLCPCB parts library, not FET-C." in text
    assert ("LCSC: Q4: the field has no JLCPCB number; the purchase source of FET-D is C1004. Write LCSC = 'C1004' "
            "into the symbol.") in text
    assert "LCSC: Q5: the field has 'abc': not a JLCPCB number" in text
    assert "LCSC: Q6: the JLCPCB parts library does not have C9999" in text
    assert "LCSC: no JLCPCB number: Q7 (BJT-E). For the JLCPCB assembly" in text
    assert "datasheet-lens adopt jlcpcb C1002" in "\n".join(prj.format_check(result, cli=True))


def test_no_lcsc_check_with_no_jlcpcb(jlc, tmp_path):  # noqa: F811
    """A project with no JLCPCB purchase source and no LCSC field: no check."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "FET-A")
        result = prj.check_parts(prj.load(project.root), store, [_symbol("Q1", "FET-A", None)])
    assert result["lcsc"] == [] and result["lcsc_ok"] == 0
    assert not any(line.startswith("LCSC") for line in prj.format_check(result))


def test_lcsc_column_of_a_bom(jlc, tmp_path):  # noqa: F811
    """The BOM reader finds the LCSC column by the names of the KiCad plugins;
    the field to write is the column of the BOM. Two LCSC columns with different
    numbers are a conflict."""
    bom = tmp_path / "bom.csv"
    bom.write_text('Reference,Value,MPN,LCSC Part #,JLCPCB Part #\n'
                   'Q1,FET (FET-A),FET-A,,\n'
                   'Q2,FET (FET-B),FET-B,C1002,C1002\n'
                   'Q3,FET (FET-C),FET-C,C1003,C1001\n', encoding="utf-8")
    parts = prj.bom_parts(str(bom))
    assert "LCSC from LCSC Part #, JLCPCB Part #" in parts.summary
    q1, q2, q3 = parts.cad
    assert (q1.lcsc, q1.lcsc_fields) == ("", ["LCSC Part #", "JLCPCB Part #"]) and q2.lcsc == "C1002"
    assert "LCSC Part # C1003, JLCPCB Part # C1001" in q3.conflict
    assert q2.conflict == ""  # two columns with the same number


def test_lcsc_review_fixes(jlc, tmp_path):  # noqa: F811
    """A CAD MPN of a project part with an other MPN finds its purchase source; a
    missing part gets no line about a number to select; a text that is not a
    number does not stop the check of the purchase source in the library; a
    number with zeros after C is the same number."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("FET-A", "FET-B", "FET-D", "BJT-E"):
            prj.add_part(project, store, mpn)
        project = prj.load(project.root)
        for i, number in ((0, "C1001"), (1, "C1002"), (2, "C1003")):  # C1003 is the C number of FET-C
            prj.set_source(project, project.parts[i], "jlcpcb", number)
        project = prj.load(project.root)
        cad = [_symbol("Q1", "FET-B", "C01002"),   # the same number
               _symbol("Q2", "FET-D", "n/a"),      # not a number; the source is of FET-C
               _symbol("Q3", "FET-X", None),       # missing in the project
               _symbol("Q4", "FET-A1", "C1001"),   # a project part with an other MPN (FET-A)
               _symbol("Q5", "BJT-E", "C1002")]    # no source, and the number is of FET-B: not to save
        result = prj.check_parts(project, store, cad)
    assert [d["cad"] for d in result["different"]] == ["FET-A1"]
    assert [(f["reference"], f["kind"]) for f in result["lcsc"]] == [("Q2", "not_number"), ("Q2", "other_mpn"),
                                                                      ("Q5", "other_mpn")]
    assert result["lcsc_ok"] == 2  # Q1 (C01002 is C1002) and Q4 (the purchase source of FET-A)


def test_more_purchase_sources_and_their_removal(jlc, tmp_path, capsys):  # noqa: F811
    """A part can have more numbers of one source (adopt_part adds one). The LCSC
    check agrees with each of them, asks which one to write, and a wrong number
    goes with project_remove_source (CLI rm -s), from the store too with store."""
    from datasheet_lens import distributors, server
    from datasheet_lens.cli import main

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "FET-A")
        project = prj.load(project.root)
        prj.set_source(project, project.parts[0], "jlcpcb", "C1001")
        project = prj.load(project.root)
        prj.set_source(project, project.parts[0], "jlcpcb", "C1002")  # the C number of FET-B: wrong
        project = prj.load(project.root)
        assert distributors.source_numbers(project.parts[0], "jlcpcb") == ["C1001", "C1002"]
        result = prj.check_parts(project, store, [_symbol("Q1", "FET-A", "C1001"), _symbol("Q2", "FET-A", "")])
    assert result["lcsc_ok"] == 1
    assert [(f["reference"], f["kind"]) for f in result["lcsc"]] == [
        ("Q1", "other_mpn"), ("Q2", "select"), ("Q2", "other_mpn")]
    text = "\n".join(prj.format_check(result))
    assert "LCSC: Q2: the field has no JLCPCB number; FET-A has more JLCPCB purchase sources (C1001, C1002)." in text
    lines = distributors.availability(prj.load(project.root))
    # C1001 is FET-A with stock: the purchase source can deliver.
    assert "FET-A (C1001, C1002, the purchase sources): orderable at the purchase source (JLCPCB)" in lines
    line = lines[lines.index("FET-A (C1001, C1002, the purchase sources): orderable at the purchase source (JLCPCB)") + 1]
    assert "JLCPCB: stock 9000, library Basic (C1002); C1001: stock 500, library Extended" in line
    assert "WARNING: C1002 is FET-B (Maker B), not FET-A" in line
    # The removal: the project file, and with store also the store.
    out = server.project_remove_source("FET-A", "c1002", path=str(project.root))
    assert out == "removed the purchase source jlcpcb C1002 from FET-A in the project file; the store keeps it"
    assert distributors.source_numbers(prj.load(project.root).parts[0], "jlcpcb") == ["C1001"]
    with Store() as store:
        part_id = store.find_part("FET-A")
        store.set_part_source(part_id, "jlcpcb", "C1002")
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C1002")
    assert main(["rm", "FET-A", "-s", "jlcpcb:C1002", "--store", "-n", "-p", str(project.root)]) == 0
    assert "would remove: the purchase source jlcpcb C1002 of FET-A; would remove it from the store too" in \
        capsys.readouterr().out
    assert main(["rm", "FET-A", "-s", "jlcpcb:C1002", "--store", "-p", str(project.root)]) == 0
    assert "removed: the purchase source jlcpcb C1002 of FET-A; removed it from the store too" in \
        capsys.readouterr().out
    with Store() as store:
        assert [s["number"] for s in store.part_sources(store.find_part("FET-A"))] == []
    assert main(["rm", "FET-A", "-s", "C9999", "-p", str(project.root)]) == 2
    assert "has no purchase source C9999 (its sources: jlcpcb C1001)" in capsys.readouterr().err



def test_the_bom_is_the_master(jlc, tmp_path):  # noqa: F811
    """A valid number of the BOM that is not the purchase source: the BOM is the
    master, the hint saves it and removes the sources that the BOM does not use,
    by the MPN of the project part. The JLCPCB section warns for each C number
    with no stock."""
    from datasheet_lens import distributors

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "FET-C")
        project = prj.load(project.root)
        prj.set_source(project, project.parts[0], "jlcpcb", "C1003")
        result = prj.check_parts(prj.load(project.root), store, [_symbol("Q1", "FET-C1", "C1003")])  # renamed
        assert result["lcsc_ok"] == 1
        db = sqlite3.connect(jlcpcb.db_path())
        db.execute("UPDATE jlc_components SET mfr = 'FET-C' WHERE lcsc = 1004")  # a second listing of FET-C
        db.commit()
        db.close()
        result = prj.check_parts(prj.load(project.root), store, [_symbol("Q1", "FET-C1", "C1004")])
    text = "\n".join(prj.format_check(result))
    assert ("The BOM is the master: save C1004 with adopt_part(source='jlcpcb', number='C1004'), and remove the "
            "purchase sources that the BOM does not use with project_remove_source(mpn='FET-C', "
            "number='C1003').") in text
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C1004")
    lines = distributors.availability(prj.load(project.root))
    head = "FET-C (C1003, C1004, the purchase sources): orderable at the purchase source (JLCPCB)"
    line = lines[lines.index(head) + 1]
    assert "WARNING: C1003 NOT IN STOCK at JLCPCB" in line and "C1004 NOT IN STOCK" not in line
