import asyncio

import pytest

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
    """The text backend: the tests must not depend on an installed docling."""
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")


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
    assert "p.1: Supply voltage 36 V" in text_of(call("search_text", text="36 V", part="OPA2134"))

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
