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
    call("add_document", path=str(path), part="OPA2134")
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

    # A second fact of the same element or subject gets a note; the page tools show the facts.
    out = text_of(call("save_fact", document=doc, page=1, subject="VS max", value="36", element="p1.P1"))
    assert f"#{fact_id} supply voltage max" in out and "set_fact_status" in out
    assert "note" not in text_of(call("save_fact", document=doc, page=1, subject="new subject", value="1"))
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
    assert main(["add", str(text_pdf("rtc.pdf", [[(50, 60, 12, "1 RTC")]])), "--part", "RV-3149-C3"]) == 2
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
