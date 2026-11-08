"""The repairs of the review of the manufacturer from the datasheet: the cases
that the first tests did not cover."""

import asyncio

import pytest
from test_adopt import _source, downloads  # noqa: F401  (the fixture: fetch.downloaded gives local PDFs)

from datasheet_lens import adopt as adopt_module
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store, sha256_file

TI_PAGE = [[(50, 60, 14, "LM358 Dual Op Amp"), (50, 760, 8, "TEXAS INSTRUMENTS")]]


def call(tool, **args):
    from datasheet_lens.server import mcp

    try:
        result = asyncio.run(mcp.call_tool(tool, args))
    except Exception as e:  # a LensError of a tool
        return str(e)
    blocks = result[0] if isinstance(result, tuple) else result.content
    return "\n".join(b.text for b in blocks)


def test_adopt_with_no_datasheet_does_not_take_a_part_of_an_other_manufacturer(downloads, monkeypatch,  # noqa: F811
                                                                                text_pdf, tmp_path):
    """No datasheet: the purchase source of the onsemi part must not go to the
    TI part with the same MPN. The part of the store of the manufacturer of the
    source is the same part."""
    with Store() as store:
        ti = store.add_part("LM358", "Texas Instruments")
    _source(monkeypatch, text_pdf, "LM358", "onsemi", None)
    result = adopt_module.adopt("mouser", "1-LM358")
    assert result["ask"] and result["lines"][-1].startswith(
        "ASK THE USER: the manufacturer of LM358. The tool has no datasheet, mouser gives onsemi, and the store has "
        "LM358 from Texas Instruments")
    with Store() as store:
        assert store.part_sources(ti) == []
    # The source names the manufacturer of the store part (TI is a name of Texas Instruments).
    _source(monkeypatch, text_pdf, "LM358", "TI", None)
    result = adopt_module.adopt("mouser", "1-LM358")
    assert not result["ask"] and ("manufacturer: Texas Instruments (the part of the store with this MPN of the "
                                  "manufacturer of mouser; the tool has no datasheet)") in result["lines"]
    with Store() as store:
        assert [s["number"] for s in store.part_sources(ti)] == ["1-LM358"]


def test_adopt_takes_the_store_part_when_the_datasheet_names_no_manufacturer(downloads, monkeypatch,  # noqa: F811
                                                                             text_pdf, tmp_path):
    """The user confirmed the name of a part before: a second adopt of the MPN
    from the same manufacturer does not ask again. retry does the step again
    with the answer and no second download."""
    seen = _source(monkeypatch, text_pdf, "X-8", "Maker Q", "Made by a small company")
    result = adopt_module.adopt("mouser", "1-X-8")
    assert result["ask"] and len(seen) == 1
    result = result["retry"]("Maker Q")
    assert not result["ask"] and len(seen) == 1 and "manufacturer: Maker Q (given)" in result["lines"]
    seen = _source(monkeypatch, text_pdf, "X-8", "Maker Q", "Made by a small company")
    result = adopt_module.adopt("mouser", "2-X-8")
    assert not result["ask"]
    assert ("manufacturer: Maker Q (the part of the store with this MPN of the manufacturer of mouser; the "
            "datasheet names no manufacturer that the tool knows)") in result["lines"]


def test_a_confirmed_name_of_a_module(store_home, text_pdf, tmp_path):
    """A module datasheet names the maker of its chip more often: with the
    confirmation of the user, the given name is the name of the part. With no
    confirmation, two manufacturers in the registry are an error, and the
    document keeps its index and its element map."""
    page = [[(50, 60, 14, "MDBT50Q-1MV2 Module"), (50, 400, 8, "NORDIC SEMICONDUCTOR nRF52840"),
             (50, 760, 8, "Nordic Semiconductor")]]
    with Store() as store:
        store.maker_relation("distinct", "Raytac", "Nordic Semiconductor")
    path = text_pdf("mdbt50q.pdf", page)
    text = call("add_document", path=str(path), part="MDBT50Q-1MV2", manufacturer="Raytac")
    assert ("part MDBT50Q-1MV2: not linked: the datasheet (document " in text
            and "records Raytac as a different manufacturer" in text and "manufacturer_confirmed=true" in text)
    assert "element map:" in text  # the document, its index and its element map stay
    text = call("add_document", path=str(path), part="MDBT50Q-1MV2", manufacturer="Raytac",
                manufacturer_confirmed=True)
    with Store() as store:
        [row] = store.match_parts("MDBT50Q-1MV2")
        assert row["manufacturer"] == "Raytac"
    # The CLI: the document gets its index, and the command ends with exit code 1.
    other = text_pdf("mdbt50q-2.pdf", page + [[(50, 60, 9, "Revision 2")]])
    assert main(["add", str(other), "-b", "MDBT50Q-1MV2B", "-M", "Raytac", "--no-project"]) == 1
    with Store() as store:
        doc = store.document(sha256_file(other))
        assert store.build(doc.id, "index") is not None and store.match_parts("MDBT50Q-1MV2B") == []
    assert main(["add", str(other), "-b", "MDBT50Q-1MV2B", "-M", "Raytac", "-C", "--no-project"]) == 0
    with Store() as store:
        assert store.match_parts("MDBT50Q-1MV2B")[0]["manufacturer"] == "Raytac"


def test_update_part_checks_a_new_manufacturer(store_home, text_pdf, tmp_path):
    with Store() as store:
        doc = store.add_document(text_pdf("lm358.pdf", TI_PAGE))
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "LM358", document=doc.sha256)
        assert project.parts[0]["manufacturer"] == "Texas Instruments"
        with pytest.raises(LensError, match="ASK THE USER: the manufacturer of LM358. Its datasheet"):
            prj.update_part(prj.load(project.root), store, "LM358", new_manufacturer="Small Co")
        assert prj.load(project.root).parts[0]["manufacturer"] == "Texas Instruments"
        part = prj.update_part(prj.load(project.root), store, "LM358", new_manufacturer="TI")
        assert part["manufacturer"] == "Texas Instruments"  # the name of the datasheet
        store.maker_relation("alias", "Small Co", "Texas Instruments")
        part = prj.update_part(prj.load(project.root), store, "LM358", new_manufacturer="Small Co",
                               confirmed=True)
        assert part["manufacturer"] == "Small Co"


def test_the_fields_after_the_next_sync(store_home, text_pdf, tmp_path):
    """project_check compares the field Manufacturer with the name that the next
    sync writes, counts the rename as a step of sync, gives the next steps
    with the field findings, and reads one name with a comma as one name."""
    with Store() as store:
        doc = store.add_document(text_pdf("lm358.pdf", TI_PAGE))
        part_id = store.add_part("LM358", "TI")
        store.link(part_id, doc.id)
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "LM358", "TI")
        store.add_part("GRM1", "Murata Manufacturing Co., Ltd.")
        prj.add_part(prj.load(project.root), store, "GRM1", "Murata Manufacturing Co., Ltd.")
        store.db.execute("UPDATE parts SET manufacturer = 'Texas Instruments' WHERE id = ?", (part_id,))
        store.db.commit()
        project = prj.load(project.root)
        cad = [{"reference": "U1", "mpn": "LM358", "manufacturer": "Texas Instruments", "distributor": "N/A"},
               {"reference": "C1", "mpn": "GRM1", "manufacturer": "Murata Manufacturing Co., Ltd."}]
        result = prj.check_parts(project, store, cad)
        assert [(f["reference"], f["kind"]) for f in result["fields"]] == [
            ("U1", "distributor_save"), ("C1", "distributor_ask")]
        result["sync"] = prj.sync_steps(prj.dry_run(project, store)[0])
        text = "\n".join(prj.format_check(result))
        assert "project folder: 1 copy missing, 1 manufacturer name to update. Run project_sync" in text
        assert "next steps: project_sync copies the documents" in text  # the fields do not block them


def test_one_mention_counts_one_time_and_the_prefix_is_no_name(store_home, text_pdf):
    """A name of the registry that holds a built-in name counts one time for one
    mention. A guess from the prefix of the MPN is a proposal, not a name of
    the datasheet: the check of a given name does not use it."""
    from datasheet_lens.identify import named_manufacturer, propose

    names = {"MURATA ELECTRONICS NORTH AMERICA": ("Murata", "Murata")}
    text = "MURATA ELECTRONICS NORTH AMERICA, TEXAS INSTRUMENTS, TEXAS INSTRUMENTS"
    assert named_manufacturer([text], names) == "Texas Instruments"  # 2 against 1, not 2 against 2
    with Store() as store:
        doc = store.add_document(text_pdf("nrf.pdf", [[(50, 60, 14, "nRF52840 Module")]]))
        p = propose(doc)
        assert (p.manufacturer, p.named) == ("Nordic Semiconductor", "")
        # The datasheet names no manufacturer: the given name of the maker of the module.
        from datasheet_lens.makercheck import datasheet_maker

        assert datasheet_maker(store, doc, "Raytac", "nRF52840-MOD") == ("Raytac", "")


def test_a_read_error_is_not_kept_and_not_hidden(store_home, text_pdf, monkeypatch):
    from datasheet_lens import identify
    from datasheet_lens.makercheck import datasheet_maker, found_maker

    with Store() as store:
        doc = store.add_document(text_pdf("lm358.pdf", TI_PAGE))
        real = identify.propose
        monkeypatch.setattr(identify, "propose", lambda d, names=None: (_ for _ in ()).throw(OSError("busy")))
        assert found_maker(store, doc) == ("", "busy")
        with pytest.raises(LensError, match="the tool cannot read the datasheet .* of LM358: busy"):
            datasheet_maker(store, doc, "Small Co", "LM358")  # not the given name with no check
        monkeypatch.setattr(identify, "propose", real)
        assert found_maker(store, doc) == ("Texas Instruments", "")  # the error was not kept


def test_the_selection_of_the_part_blocks_the_next_steps(store_home, tmp_path):
    with Store() as store:
        store.maker_relation("distinct", "UMW", "Texas Instruments")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "SN74LVC1G14DBVR", "Texas Instruments")
        prj.add_part(prj.load(project.root), store, "SN74LVC1G14DBVR", "UMW")
        result = prj.check_parts(prj.load(project.root), store, [{"reference": "U7", "mpn": "SN74LVC1G14DBVR"}])
        assert [f["kind"] for f in result["fields"]] == ["maker_select"]
        text = "\n".join(prj.format_check(result, cli=True))
        assert "Then run datasheet-lens check again" in text and "next steps" not in text
