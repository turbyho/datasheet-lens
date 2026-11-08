"""The check of the manufacturers of the parts in the store against their datasheets."""

from datasheet_lens import makercheck
from datasheet_lens.cli import main
from datasheet_lens.store import Store


def _part(store, text_pdf, mpn, maker, text, name=None):
    """A part with a datasheet whose page 1 names text at the bottom."""
    doc = store.add_document(text_pdf(name or f"{mpn}.pdf", [[(50, 60, 14, f"{mpn} Device"), (50, 760, 8, text)]]))
    part_id = store.add_part(mpn, maker)
    store.link(part_id, doc.id)
    return part_id, doc


def test_the_check_of_the_manufacturers(store_home, text_pdf, capsys):
    with Store() as store:
        store.maker_relation("distinct", "Maker Q", "Texas Instruments")
        _part(store, text_pdf, "0603WAF1002T5E", "UNI ROYAL Uniroyal Elec", "Uniroyal Electronics Global Co., Ltd.")
        _part(store, text_pdf, "TPS1", "Texas Instruments", "TEXAS INSTRUMENTS")  # the name of the datasheet
        _part(store, text_pdf, "TPS2", "", "TEXAS INSTRUMENTS")  # no manufacturer: the datasheet gives it
        _part(store, text_pdf, "TPS3", "Maker Q", "TEXAS INSTRUMENTS")  # a different manufacturer
        _part(store, text_pdf, "SMC1", "Maker Q", "Small Co")  # a name that the tool does not know
        store.add_part("NODS1", "Maker Q")  # no datasheet: not checked
        result = makercheck.check(store)
        assert result["ok"] == 1
        assert [(i["mpn"], i["manufacturer"], i["new"]) for i in result["rename"]] == [
            ("0603WAF1002T5E", "UNI ROYAL Uniroyal Elec", "UNI-ROYAL"), ("TPS2", "", "Texas Instruments")]
        assert [(i["mpn"], i["names"]) for i in result["different"]] == [("TPS3", ["Texas Instruments"])]
        assert [(i["mpn"], i["reason"]) for i in result["unread"]] == [("SMC1", "")]
        text = makercheck.lines(result)
        assert text[1].startswith("rename: 0603WAF1002T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL (the name of the "
                                  "datasheet ")
        assert any(line.startswith("rename: TPS2: no manufacturer -> Texas Instruments") for line in text)
        assert any(line.startswith("different: TPS3 (Maker Q): the datasheet ") and
                   line.endswith(" gives Texas Instruments") for line in text)
        assert any(line.startswith("unread: SMC1 (Maker Q): the datasheet ") for line in text)
        # The user selects one rename: only this part changes.
        changes, _ = makercheck.apply(store, ["0603waf1002t5e", "TPS1"])
        assert changes == ["not changed: tps1: the check proposes no new name for it",
                           "changed: 0603WAF1002T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL"]
        assert store.match_parts("0603WAF1002T5E")[0]["manufacturer"] == "UNI-ROYAL"
        assert store.match_parts("TPS2")[0]["manufacturer"] == ""
        # The user tells that Small Co is the name of Maker Q: the next check proposes the rename.
        store.maker_relation("alias", "Small Co", "Maker Q")
        assert [(i["mpn"], i["new"]) for i in makercheck.check(store)["rename"]] == [
            ("SMC1", "Small Co"), ("TPS2", "Texas Instruments")]
    assert main(["maker", "check", "-a"]) == 0
    assert capsys.readouterr().out.splitlines() == ["changed: SMC1: Maker Q -> Small Co",
                                                    "changed: TPS2: no manufacturer -> Texas Instruments"]
    assert main(["maker", "check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("4 parts have the manufacturer name of their datasheet") and "rename:" not in out


def test_the_check_does_not_make_a_second_row_of_a_part(store_home, text_pdf):
    """A part with no manufacturer next to the part of its MPN from the
    manufacturer of the datasheet: no rename (two rows of one part)."""
    with Store() as store:
        store.maker_relation("distinct", "onsemi", "Texas Instruments")
        _, doc = _part(store, text_pdf, "LM1", "TI", "TEXAS INSTRUMENTS")
        store.add_part("LM1", "onsemi")
        unnamed = store.add_part("LM1", "")
        store.link(unnamed, doc.id)
        changes, result = makercheck.apply(store)
        assert [(i["mpn"], i["manufacturer"]) for i in result["rename"]] == [("LM1", "TI"), ("LM1", "")]
        assert changes[0] == "changed: LM1: TI -> Texas Instruments"
        assert changes[1].startswith("not changed: LM1: the store has LM1 from Texas Instruments already")
        assert store.part(unnamed)["manufacturer"] == ""


def test_the_mcp_tool(store_home, text_pdf):
    import asyncio

    from datasheet_lens.server import mcp

    with Store() as store:
        _part(store, text_pdf, "0603WAF4701T5E", "UNI ROYAL Uniroyal Elec", "UNIOHM")

    def call(**args):
        result = asyncio.run(mcp.call_tool("manufacturer_check", args))
        blocks = result[0] if isinstance(result, tuple) else result.content
        return "\n".join(b.text for b in blocks)

    assert "rename: 0603WAF4701T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL" in call()
    assert call(apply=True, mpns=["0603WAF4701T5E"]) == "changed: 0603WAF4701T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL"
    assert call(apply=True) == "no part to change"


def test_sync_takes_the_checked_name_into_the_project_file(store_home, text_pdf, tmp_path):
    """The project file of an older adopt has the name of the distributor. sync
    changes it only when the store has the name of the datasheet: after the
    rename of the check, not before."""
    from datasheet_lens import project as prj

    with Store() as store:
        _, doc = _part(store, text_pdf, "0603WAF1002T5E", "UNI ROYAL Uniroyal Elec", "Uniroyal Electronics Global")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "0603WAF1002T5E", "UNI ROYAL Uniroyal Elec", doc.sha256)
        report = prj.sync(prj.load(project.root), store)
        assert report["renamed"] == []
        assert prj.load(project.root).parts[0]["manufacturer"] == "UNI ROYAL Uniroyal Elec"
        makercheck.apply(store)
        dry = prj.sync(prj.load(project.root), store, apply=False)
        lines = prj.format_report(dry, False, ("add {part}{base} {sha}", " -b {base}"))
        assert ("manufacturer: 0603WAF1002T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL (the name of the store: the "
                "name of the datasheet); the next sync writes it into the project file") in lines
        assert prj.load(project.root).parts[0]["manufacturer"] == "UNI ROYAL Uniroyal Elec"  # a dry run
        report = prj.sync(prj.load(project.root), store)
        assert report["renamed"] == ["0603WAF1002T5E: UNI ROYAL Uniroyal Elec -> UNI-ROYAL"]
        [part] = prj.load(project.root).parts
        assert part["manufacturer"] == "UNI-ROYAL" and len(part["documents"]) == 1
        assert "was" not in part and prj.sync(prj.load(project.root), store)["renamed"] == []


def test_add_document_and_project_add_part_take_the_name_of_the_datasheet(store_home, text_pdf, tmp_path):
    """The manufacturer of a datasheet comes from the datasheet also in
    add_document and project_add_part: a name of the same manufacturer gives
    the name of the datasheet, a name that the registry does not relate to it
    gives the question for the relation, and nothing is linked."""
    import asyncio

    from datasheet_lens import project as prj
    from datasheet_lens.server import mcp

    def call(tool, **args):
        try:
            result = asyncio.run(mcp.call_tool(tool, args))
        except Exception as e:  # a LensError of a tool
            return str(e)
        blocks = result[0] if isinstance(result, tuple) else result.content
        return "\n".join(b.text for b in blocks)

    page = [[(50, 60, 14, "TPS7A02 Regulator"), (50, 760, 8, "TEXAS INSTRUMENTS")]]
    text = call("add_document", path=str(text_pdf("a.pdf", page)), part="TPS7A02", manufacturer="Small Co")
    assert ("ASK THE USER: the manufacturer of TPS7A02. Its datasheet (document " in text
            and "names Texas Instruments, and the step gives Small Co" in text)
    with Store() as store:
        assert store.match_parts("TPS7A02") == []
    call("add_document", path=str(text_pdf("a.pdf", page)), part="TPS7A02", manufacturer="TI")
    with Store() as store:
        [row] = store.match_parts("TPS7A02")
        assert row["manufacturer"] == "Texas Instruments"  # the name of the datasheet, not TI
        doc = store.add_document(text_pdf("b.pdf", [[(50, 60, 14, "XYZ125 Widget"), (50, 760, 8, "TEXAS INSTRUMENTS")]]))
    project = prj.init_project(tmp_path / "board")
    text = call("project_add_part", mpn="XYZ125", manufacturer="Small Co", document=doc.sha256[:12],
                path=str(project.root))
    assert "ASK THE USER: the manufacturer of XYZ125" in text and prj.load(project.root).parts == []
    call("project_add_part", mpn="XYZ125", document=doc.sha256[:12], path=str(project.root))
    assert prj.load(project.root).parts[0]["manufacturer"] == "Texas Instruments"
