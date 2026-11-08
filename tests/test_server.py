import asyncio
import threading

import pytest

from datasheet_lens import server
from datasheet_lens.server import mcp

PAGE = [(50, 60, 12, "5.1 Absolute Maximum Ratings"),
        (50, 90, 10, "Supply voltage 36 V"),
        (50, 300, 9, "Figure 5-1. Total Harmonic Distortion")]


def call(name, **args):
    return asyncio.run(mcp.call_tool(name, args))


def text_of(result):
    """The text blocks of a tool result."""
    blocks = result.content if hasattr(result, "content") else result
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return "\n".join(getattr(b, "text", "") for b in blocks)


def blocks_of(result):
    blocks = result.content if hasattr(result, "content") else result
    return blocks[0] if isinstance(blocks, tuple) else blocks


@pytest.fixture(autouse=True)
def text_backend(monkeypatch):
    """The text backend: the tests must not depend on an installed docling. The
    background builds of a test end before the next test."""
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    yield
    for job in list(server._builds.values()):
        job["thread"].join(10)
    server._builds.clear()


def tool_error(name, **args):
    """The message of a tool error (the SDK can raise it or return isError)."""
    try:
        result = call(name, **args)
    except Exception as e:
        return str(e)
    assert getattr(result, "is_error", False)
    return text_of(result)


def test_tools_are_listed():
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"get_index", "get_page_map", "get_page_image", "get_pdf_pages", "save_fact",
            "find_facts"} <= names


def test_drill_down(store_home, text_pdf):
    path = text_pdf("opa.pdf", [PAGE, [(50, 60, 12, "6.1 Overview")]])
    out = text_of(call("add_document", path=str(path), part="OPA2134", manufacturer="TI"))
    assert "2 pages" in out
    doc = text_of(call("list_documents", part="opa2134")).split()[0]
    assert "5.1 Absolute Maximum Ratings (p.1)" in text_of(call("get_index", document=doc))
    pmap = text_of(call("get_page_map", document=doc, page=1))
    assert "p1.H1" in pmap and "p1.C1" in pmap
    assert "Supply voltage 36 V" in text_of(call("get_element_text", document=doc, element_ids=["p1.P1"]))
    assert "p.1 [50, 81, 139, 92]: Supply voltage 36 V" in text_of(call("search_text", text="36 V", part="OPA2134"))

    image = blocks_of(call("get_page_image", document=doc, element="p1.H1"))
    assert any(getattr(b, "type", "") == "image" for b in image)
    assert "600 dpi" in text_of(call("get_page_image", document=doc, element="p1.H1"))

    pdf = blocks_of(call("get_pdf_pages", document=doc, pages="2"))
    resource = [b for b in pdf if getattr(b, "type", "") == "resource"][0]
    assert resource.resource.mime_type == "application/pdf"


def test_facts(store_home, text_pdf):
    path = text_pdf("opa.pdf", [PAGE])
    call("add_document", path=str(path), part="OPA2134", manufacturer="Texas Instruments")
    doc = text_of(call("list_documents")).split()[0]
    out = text_of(call("save_fact", document=doc, page=1, subject="supply voltage max", value="36",
                       unit="V", column="MAX", element="p1.P1", model="test-model", part="OPA2134"))
    fact_id = int(out.split("#")[1].split()[0])
    found = text_of(call("find_facts", query="supply", part="OPA2134"))
    assert "supply voltage max = 36 V" in found and "unchecked" in found and "test-model" in found
    call("set_fact_status", fact_id=fact_id, status="disputed", note="errata gives 34 V")
    found = text_of(call("find_facts", query="36"))
    assert "disputed" in found and "note: disputed: errata gives 34 V" in found

    # A Unicode query matches without regard to case (SQLite lower() is ASCII only).
    call("save_fact", document=doc, page=1, subject="Pull-up", value="10", unit="kΩ")
    call("save_fact", document=doc, page=1, subject="ΔVOS", value="2", unit="µV")
    assert "ΔVOS = 2 µV" in text_of(call("find_facts", query="δvos"))

    # An alias finds the part; the element must be on the page of the fact.
    from datasheet_lens.store import Store
    with Store() as store:
        store.db.execute("INSERT INTO part_aliases (part_id, alias) VALUES (?, ?)",
                         (store.find_part("OPA2134"), "OPA2134PA"))
        store.db.commit()
    assert "saved" in text_of(call("save_fact", document=doc, page=1, subject="x", value="1",
                                   part="opa2134pa"))
    assert "element not found" in tool_error("save_fact", document=doc, page=1, subject="x", value="1",
                                             element="p7.T1")
    assert "x0 < x1" in tool_error("save_fact", document=doc, page=1, subject="x", value="1",
                                   bbox=[100, 10, 50, 20])

    # A fact with the same subject, conditions, column and part gets a note (case
    # and spaces are ignored). A different value of the same table (a different
    # subject, or different conditions) is not a copy: one table holds many values.
    out = text_of(call("save_fact", document=doc, page=1, subject="Supply  Voltage MAX", value="36", unit="V",
                       column="MAX", element="p1.P1", part="OPA2134"))
    assert f"can be a copy of (the same subject and conditions, or the same cell of the table): #{fact_id} page 1: " \
           f"36 V" in out and "set_fact_status" in out
    # The same cell under a different subject: a note. A different value of the table: no note.
    assert f"#{fact_id} page 1: 36 V" in text_of(call("save_fact", document=doc, page=1, subject="VS max",
                                                      value="36", unit="V", column="MAX", element="p1.P1"))
    assert "note" not in text_of(call("save_fact", document=doc, page=1, subject="IQ max", value="5", unit="mA",
                                      column="MAX", element="p1.P1"))
    # Conditions with other spaces, and a fact with no part: the same fact.
    first = text_of(call("save_fact", document=doc, page=1, subject="offset", value="0.5", unit="mV",
                         conditions="TA = 25 °C", part="OPA2134"))
    offset_id = int(first.split("#")[1].split()[0])
    assert f"#{offset_id} page 1: 0.5 mV" in text_of(call("save_fact", document=doc, page=1, subject="Offset",
                                                          value="0.5", unit="mV", conditions="TA=25°C"))
    assert "note" not in text_of(call("save_fact", document=doc, page=1, subject="supply voltage max", value="30",
                                      unit="V", column="MAX", conditions="TA = 125 °C", part="OPA2134"))
    assert "note" not in text_of(call("save_fact", document=doc, page=1, subject="new subject", value="1"))
    assert "part not found" in tool_error("save_fact", document=doc, page=1, subject="offset", value="1",
                                          part="NO-SUCH-PART")
    assert f"known facts on this page (find_facts gives the values): #{fact_id} supply voltage max" in text_of(
        call("get_page_map", document=doc, page=1))
    assert f"#{fact_id} supply voltage max" in text_of(call("get_page_image", document=doc, element="p1.P1"))


def test_background_build(store_home, text_pdf, monkeypatch):
    build = server._run_build  # the real build: a separate process
    go, fail = threading.Event(), []

    def slow_build(doc_id, backend):
        go.wait(10)
        if fail:
            raise RuntimeError("layout model crashed")
        return build(doc_id, backend)

    monkeypatch.setattr(server, "_run_build", slow_build)
    monkeypatch.setattr(server, "ELEMENT_WAIT", 0.05)
    path = str(text_pdf("opa.pdf", [PAGE]))
    assert "a background thread builds the element map" in text_of(call("add_document", path=path))
    doc = text_of(call("list_documents")).split()[0]
    assert "a background thread builds" in tool_error("get_page_map", document=doc, page=1)
    assert "a background thread builds" in tool_error("save_fact", document=doc, page=1, subject="x",
                                                      value="1", element="p1.P1")
    # The index and the page images do not wait for the element map.
    assert "Absolute Maximum Ratings" in text_of(call("get_index", document=doc))
    assert "dpi" in text_of(call("get_page_image", document=doc, page=1))

    # A failed build gives its cause, and a read tool does not start it again.
    fail.append(True)
    go.set()
    server._builds[1]["thread"].join(10)
    for _ in range(2):
        assert "layout model crashed" in tool_error("get_page_map", document=doc, page=1)
    assert not server._builds[1]["thread"].is_alive()

    # add_document starts it again.
    fail.clear()
    monkeypatch.setattr(server, "ELEMENT_WAIT", 10)
    assert "element map: " in text_of(call("add_document", path=path))
    assert "p1.H1" in text_of(call("get_page_map", document=doc, page=1))

    # A document with no map and no build (for example after a restart of the
    # server) starts the build at the first request of the map.
    server._builds.clear()
    from datasheet_lens.store import Store
    with Store() as store:
        store.db.execute("DELETE FROM builds WHERE kind = 'elements'")
        store.db.commit()
    assert "starts to build it now" in tool_error("get_page_map", document=doc, page=1)
    server._builds[1]["thread"].join(10)
    assert "p1.H1" in text_of(call("get_page_map", document=doc, page=1))


def test_errors_are_tool_errors(store_home, text_pdf, tmp_path, monkeypatch):
    path = text_pdf("opa.pdf", [PAGE, PAGE])
    call("add_document", path=str(path))
    doc = text_of(call("list_documents")).split()[0]
    assert "page 9 is not in the document" in tool_error("get_page_map", document=doc, page=9)
    assert "is on page 1, not on page 2" in tool_error("get_page_image", document=doc, page=2,
                                                       element="p1.H1")
    # A file error gives its cause, not only "Error executing tool".
    assert "No such file" in tool_error("add_document", path=str(tmp_path / "missing.pdf"))
    monkeypatch.setattr("datasheet_lens.server.MAX_PDF_PAGES", 1)
    assert "1 pages or fewer" in tool_error("get_pdf_pages", document=doc, pages="1-2")


def test_missing_index(store_home, text_pdf, monkeypatch):
    """A document with no index: the library tools say so (not "no match"), and
    the MCP tools build the index, because the model cannot run the CLI."""
    from datasheet_lens.errors import LensError
    from datasheet_lens.indexer import outline_under, search
    from datasheet_lens.store import Store

    with Store() as store:
        doc = store.add_document(text_pdf("opa.pdf", [PAGE]))
        for check in (lambda: search(store, [doc.id], "36 V"), lambda: outline_under(store, doc.id, "5")):
            with pytest.raises(LensError, match="has no index"):
                check()
    assert "p.1 [50, 81, 139, 92]: Supply voltage 36 V" in text_of(call("search_text", text="36 V", document=doc.sha256[:12]))

    # The CLI command add keeps the link to the part when the index fails.
    from datasheet_lens import indexer
    from datasheet_lens.cli import main

    def fail(*args, **kwargs):
        raise LensError("index failed")

    monkeypatch.setattr(indexer, "build_index", fail)
    assert main(["add", str(text_pdf("rtc.pdf", [[(50, 60, 12, "1 RTC")]])), "--part", "RV-3149-C3",
                 "-M", "Micro Crystal"]) == 2
    with Store() as store:
        assert [r["filename"] for r in store.part_documents("RV-3149-C3")] == ["rtc.pdf"]


def test_each_tool_parameter_has_a_description():
    """The model reads the descriptions: a parameter with none is a defect."""
    for t in asyncio.run(mcp.list_tools()):
        schema = t.input_schema if hasattr(t, "input_schema") else t.inputSchema
        assert len(t.description) > 40, t.name
        for name, prop in schema.get("properties", {}).items():
            assert prop.get("description"), f"{t.name}.{name}"


def test_instructions_fit_the_client_limit():
    """Claude Code cut a text of 4162 characters after character 4091 (a review
    on 2026-10-29). The limit keeps a margin."""
    from datasheet_lens.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) < 3800


def test_part_links():
    """Search links for each site, with the MPN in the URL (encoded)."""
    out = text_of(call("part_links", mpn="MAX20361BEWC+"))
    assert "Findchips: https://www.findchips.com/search/MAX20361BEWC%2B" in out
    assert "symbol, footprint, 3D model:" in out and "componentsearchengine.com/search?term=MAX20361BEWC%2B" in out
    assert "the MPN is empty" in tool_error("part_links", mpn=" ")


def test_project_check_reads_a_bom_file(store_home, tmp_path):
    """project_check with bom reads the CSV file of kicad-cli (the default fields of
    export_bom of kicad-workbench), with the reader of the CLI command check."""
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134PA")
        prj.add_part(project, store, "LM358")
    bom = tmp_path / "bom.csv"
    bom.write_text(
        '"Reference","Value","Footprint","Datasheet","QUANTITY","DNP","Description","MPN"\n'
        '"C1","100nF","C_0603","","1","","",""\n'
        '"U1","OPA2134","SOIC-8","https://example.com/opa.pdf","1","","","OPA2134PA"\n'
        '"U2","TLV755P","SOT-23-5","","1","","","TLV75533PDBVR"\n'
        '"U3","LM358N","DIP-8","","1","DNP","","LM358P"\n', encoding="utf-8")
    out = text_of(call("project_check", bom=str(bom), path=str(project.root)))
    assert f"{bom}: MPN from MPN; Value from Value; references from Reference; no column Value_Confirmed" in out
    # U3 is not fitted (DNP): its part stays a part of the CAD project.
    assert "CAD lines with a part: 3" in out and "not fitted (DNP): U3 (LM358P)" in out
    assert "the same in the CAD project and in the project file: 1 parts" in out
    assert "missing: TLV75533PDBVR (U2)" in out and "different: CAD LM358P, project LM358" in out
    assert "extra:" not in out
    # Value is always Value, not the column of the MPN.
    assert "is the column Value, which the tool always reads as Value" in tool_error(
        "project_check", bom=str(bom), bom_column="Value", path=str(project.root))

    assert "not the two" in tool_error("project_check", path=str(project.root))
    assert "not the two" in tool_error("project_check", parts=[], bom=str(bom), path=str(project.root))
    assert "need a BOM file" in tool_error("project_check", parts=[], bom_column="MPN",
                                                      path=str(project.root))
    assert "cannot read the BOM file" in tool_error("project_check", bom=str(tmp_path / "no.csv"),
                                                    path=str(project.root))
    assert "no column 'Nope'" in tool_error("project_check", bom=str(bom), bom_column="Nope",
                                            path=str(project.root))


def test_project_check_bom_errors(store_home, tmp_path):
    """A relative BOM path is relative to the project folder. A BOM with no data
    lines gives a result. A line with more values than the header, a file that is
    not CSV text and a broken CSV file give a tool error, never an exception."""
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134PA")
    root = str(project.root)
    (project.root / "bom.csv").write_text("Reference,Value,MPN\n", encoding="utf-8")
    out = text_of(call("project_check", bom="bom.csv", path=root))
    assert "bom.csv: MPN from MPN; Value from Value; references from Reference; no column Value_Confirmed" in out
    assert "CAD lines with a part: 0" in out
    assert "extra: OPA2134PA" in out
    # A comma in a value with no quotes: the columns of the line are not known.
    (project.root / "bom.csv").write_text("Reference,Value,MPN\nC1,10uF, 25V,GRM188\nU1,OPA,OPA2134PA\n",
                                          encoding="utf-8")
    assert "line 2 has more values than the header line" in tool_error("project_check", bom="bom.csv", path=root)
    (project.root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    error = tool_error("project_check", bom=".env", path=root)
    assert "must be CSV text" in error and "secret" not in error
    (project.root / "open.csv").write_text('"Reference","MPN"\n"U1","abc\n"U2","XQ1001"\n', encoding="utf-8")
    assert "is not valid CSV" in tool_error("project_check", bom="open.csv", path=root)
    (project.root / "u16.txt").write_bytes(b"\xff\xfeM\x00P\x00N\x00\n")
    assert "not valid UTF-16 text" in tool_error("project_check", bom="u16.txt", path=root)
    (project.root / "semi.csv").write_text("'Reference';'MPN'\n'U1';'XQ1001'\n", encoding="utf-8")
    assert "give the quote ' with bom_quote" in tool_error("project_check", bom="semi.csv", path=root)
    out = text_of(call("project_check", bom="semi.csv", bom_quote="'", path=root))
    assert "MPN from MPN; references from Reference; no column Value_Confirmed" in out
    assert "CAD lines with a part: 1" in out
    (project.root / "tab.txt").write_text('Reference\tMPN\nU1\t"XQ1001" YQ2002\n', encoding="utf-8")
    out = text_of(call("project_check", bom="tab.txt", bom_delimiter="tab", bom_quote="none", path=root))
    assert "CAD lines with a part: 1" in out and 'missing: "XQ1001" YQ2002' in out  # the quotes are text
    assert "must be different (bom_delimiter, bom_quote)" in tool_error(
        "project_check", bom="semi.csv", bom_delimiter="'", bom_quote="'", path=root)
    # "~" is an empty field of KiCad, also in the parts of the MCP server of a CAD tool.
    out = text_of(call("project_check", parts=[{"mpn": "~", "value": "~", "reference": "U9"}], path=root))
    assert "missing: ~" not in out
    out = text_of(call("project_check", parts=[], bom_quote="", path=root))  # the default of the agent
    assert "extra: OPA2134PA" in out
    (project.root / "x.csv").write_text("Name,Qty\nA,1\n", encoding="utf-8")
    assert "give the column of the MPN with bom_column" in tool_error("project_check", bom="x.csv", path=root)


def test_project_check_reads_the_bom_files_of_variants(store_home, tmp_path):
    """One BOM file for each variant of the product: the parts of all files count, a
    reference with an other MPN in an other variant is not a conflict, and the same
    message of two files comes one time."""
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "2N7002LT1G")
    root = str(project.root)
    header = "Reference,Value,MPN,Value_Confirmed\n"
    (project.root / "a.csv").write_text(header + "Q1,2N7002 (2N7002LT1G),2N7002LT1G,2N7002 (2N7002LT1G)\n"
                                        "C1,100nF,,\n", encoding="utf-8")
    (project.root / "b.csv").write_text(header + "Q1,BSS138 (BSS138LT1G),BSS138LT1G,BSS138 (BSS138LT1G)\n"
                                        "C1,100nF,,\n", encoding="utf-8")
    out = text_of(call("project_check", bom=["a.csv", "b.csv"], path=root))
    assert "missing: BSS138LT1G (Q1)" in out and "conflict" not in out and "change in the" not in out
    assert "complete the fields" not in out
    # Each line of a BOM file of a variant needs its reference.
    (project.root / "c.csv").write_text("Value,MPN\n2N7002 (2N7002LT1G),2N7002LT1G\n", encoding="utf-8")
    assert "variant c.csv: a line with no reference" in tool_error("project_check", bom=["a.csv", "c.csv"],
                                                                   path=root)
    assert out.count("no part selected") == 1 and "C1." in out


def test_project_check_bom_path_is_relative_to_the_project(store_home, tmp_path):
    """A relative BOM path is relative to the folder of the project file, also when
    path is a subfolder of the project."""
    from datasheet_lens import project as prj

    project = prj.init_project(tmp_path / "board")
    (project.root / "hw").mkdir()
    (project.root / "bom.csv").write_text("Reference,MPN\nU1,XQ1001\n", encoding="utf-8")
    out = text_of(call("project_check", bom="bom.csv", path=str(project.root / "hw")))
    assert "missing: XQ1001 (U1)" in out


def test_project_check_parts_with_no_value_confirmed(store_home, tmp_path):
    """The parts of an MCP server with no value_confirmed: the message tells the agent
    to give it; with value_confirmed, the message does not come."""
    from datasheet_lens import project as prj

    root = str(prj.init_project(tmp_path / "board").root)
    out = text_of(call("project_check", parts=[{"mpn": "2N7002LT1G", "value": "2N7002", "reference": "Q1"}],
                       path=root))
    assert "no field Value_Confirmed: 1 CAD parts" in out and "With parts: give value_confirmed of each part." in out
    out = text_of(call("project_check", parts=[{"mpn": "2N7002LT1G", "value": "2N7002", "reference": "Q1",
                                                "value_confirmed": '["2N7002"]'}], path=root))
    assert "no field Value_Confirmed" not in out and "missing: 2N7002LT1G (Q1)" in out


def test_a_part_with_no_document(store_home):
    """search_text and find_facts for a part with no document in the store tell
    it: the datasheet was not searched."""
    assert "The store has no document of the part NO-PART: nothing to search" in text_of(
        call("search_text", text="VDD", part="NO-PART"))
    assert "The store has no document of the part NO-PART: no facts" in text_of(call("find_facts", part="NO-PART"))


def test_element_text_of_a_table_gives_the_image_call(store_home, text_pdf):
    """A table, figure or formula has only its caption as text: the result gives
    the call of get_page_image. A table with a caption label also gets the
    caption call (a table that continues gives one image for each page)."""
    from datasheet_lens.store import Store

    path = text_pdf("opa.pdf", [PAGE])
    call("add_document", path=str(path), part="OPA2134", manufacturer="TI")
    for job in list(server._builds.values()):
        job["thread"].join(10)
    with Store() as store:
        doc = store.document(text_of(call("list_documents", part="opa2134")).split()[0])
        rows = [("p1.T9", "table", "T", "Table 7-1. Pin Functions"), ("p1.T8", "table", "T", ""),
                ("p1.F9", "picture", "F", "Figure 9. Block Diagram"), ("p1.Q9", "formula", "Q", "")]
        for n, (eid, type_, code, text) in enumerate(rows):
            store.db.execute("INSERT INTO elements VALUES (?, ?, 1, ?, ?, ?, 40, 400, 500, 700, ?)",
                             (doc.id, eid, type_, code, 100 + n, text))
        store.db.execute("INSERT INTO captions VALUES (?, 99, 'table', 'Table 7-1', 'Pin Functions', 1, 50, 390,"
                         " 1, 1)", (doc.id,))
        store.db.commit()
    ref = doc.sha256[:12]
    lines = text_of(call("get_element_text", document=ref, element_ids=[r[0] for r in rows] + ["p1.P1"])).splitlines()
    image = "  The values are in the image: get_page_image(document='{}', element='{}')."
    assert lines[1] == (image.format(ref, "p1.T9") + f" The table can continue on the next pages: "
                        f"get_page_image(document='{ref}', caption='Table 7-1') gives one image for each page.")
    assert lines[3] == image.format(ref, "p1.T8")  # no caption label: no caption call
    assert lines[5] == image.format(ref, "p1.F9") and lines[7] == image.format(ref, "p1.Q9")
    assert lines[8].startswith("p1.P1 (") and len(lines) == 9  # a paragraph: its text only


def test_add_document_title(store_home, text_pdf):
    """The parameter title, and the title in the result. The title of a document
    that the store has already does not change: the result tells it."""
    plain = text_pdf("plain.pdf", [PAGE])
    out = text_of(call("add_document", path=str(plain)))
    assert "\nno title (the PDF metadata has none)" in out
    ldo = text_pdf("ldo.pdf", [[(50, 60, 14, "TLV755P 500-mA Low-Dropout Regulator")]])
    title = "TLV755P 500-mA Low-Dropout Regulator"
    out = text_of(call("add_document", path=str(ldo), part="TLV755P", manufacturer="TI", title=title))
    assert f"\ntitle: {title!r}\n" in out + "\n"
    assert f"title: {title}" in text_of(call("list_documents", part="TLV755P"))
    out = text_of(call("add_document", path=str(ldo), title="An other title"))
    assert (f"\ntitle: {title!r}. The store has this document already: the parameter title does not change it"
            in out)
    out = text_of(call("add_document", path=str(ldo)))
    assert f"\ntitle: {title!r} (from the PDF metadata or an earlier add)" in out
    # A document of the store with no title gets the title.
    out = text_of(call("add_document", path=str(plain), title="Plain"))
    assert "\ntitle: 'Plain'\n" in out + "\n"
