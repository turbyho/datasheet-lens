"""project_check and the JLCPCB (LCSC) numbers of the CAD symbols (phase 7 of
plans/parametric-search.md). The data is made up (see test_jlcpcb.py)."""

from test_jlcpcb import jlc  # noqa: F401  (the fixture: a JLCPCB database of made-up parts)

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
    assert ("LCSC: Q2: the field gives C1002 for FET-B, and the project has no JLCPCB purchase source. When the "
            "user agrees, save it: adopt_part(source='jlcpcb', number='C1002').") in text
    assert "LCSC: Q3: the field gives C1001, the JLCPCB purchase source of FET-C is C1003." in text
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
