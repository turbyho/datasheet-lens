"""MCP server (stdio): the progressive drill-down into component datasheets.

Method for the model:
  1. list_documents / get_index: the outline and the captions of a document.
  2. get_outline: the outline under one section of a large document.
  3. get_page_map: the structure of one page (headings, tables, figures, formulas).
  4. get_element_text: the full text of an element.
  5. get_page_image / get_pdf_pages: the original, to read the values.
Values must come from the original (step 5). Index, map and text only find them.
"""

from __future__ import annotations

import base64
import functools
import threading

import pypdfium2 as pdfium
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource

from . import __version__
from .errors import LensError
from .store import ROLES, Store

# The PDF of get_pdf_pages goes into one tool result: a larger request must
# use more calls.
MAX_PDF_PAGES = 50

# The SDK runs the tools in worker threads. Only one add_document at a time:
# a second call for the same file (for example a retry after a client timeout)
# then finds the current build and does not start a second one.
_add_lock = threading.Lock()

INSTRUCTIONS = """\
datasheet-lens gives the content of component datasheets (PDF). It does not convert
a datasheet to text: read the values from the original pages.

Go into a document step by step, and ask only for what the work needs:
1. list_parts / list_documents: find the documents of a part (a part can have a
   datasheet, a manual, errata).
2. get_index: the outline and the table and figure captions, with pages.
3. get_outline: the entries under one section (large documents).
4. search_text: find a word or a value in the text layer (page and line).
5. get_page_map: the headings, tables, figures and formulas of one page, with IDs.
6. get_element_text: the full text of elements.
7. get_page_image (an element ID, a region, or a page) or get_pdf_pages: the
   original. Read the values here, never from the text layer alone (it can lose
   superscripts, signs and table structure).
Save a value that the work uses with save_fact (with its page and element), and
look for saved values first with find_facts.
"""

mcp = MCPServer("datasheet-lens", instructions=INSTRUCTIONS)


def tool(func):
    """Register a tool. A LensError, a file error or a PDF error becomes a tool
    error with its message, so that the model can correct the request."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except LensError as e:
            raise ToolError(str(e)) from e
        except (OSError, pdfium.PdfiumError) as e:
            raise ToolError(f"{e.__class__.__name__}: {e}") from e

    return mcp.tool()(wrapper)


def _doc(store: Store, ref: str):
    doc = store.document(ref)
    if doc is None:
        raise LensError(f"document not found: {ref} (use list_documents)")
    return doc


@tool
def list_parts() -> str:
    """List the parts in the store, with the number of documents of each part."""
    with Store() as store:
        rows = store.parts()
        if not rows:
            return "No parts. Add a document with add_document."
        return "\n".join(f"{r['mpn']} ({r['manufacturer'] or 'manufacturer unknown'}): "
                         f"{r['documents']} documents" for r in rows)


@tool
def list_documents(part: str = "") -> str:
    """List the documents, or the documents of one part (MPN or alias). Each line
    gives the document ID to use in the other tools, the pages and the role."""
    with Store() as store:
        rows = store.part_documents(part) if part else store.documents()
        if not rows:
            return f"No documents for part {part}." if part else "No documents."
        out = []
        for r in rows:
            role = r["role"] if part else (r["parts"] or "no part")
            out.append(f"{r['sha256'][:12]}  {r['pages']} pages  {r['filename']}  ({role})"
                       + (f"  title: {r['title']}" if r["title"] else ""))
        return "\n".join(out)


@tool
def add_document(path: str, part: str = "", manufacturer: str = "", role: str = "datasheet") -> str:
    """Add a PDF file to the store, link it to a part, and build its index and
    element map. role: datasheet, manual, errata, appnote or other."""
    from pathlib import Path

    from .elements import build_elements
    from .indexer import build_index
    from .setup import element_backend

    if role not in ROLES:
        raise LensError(f"role must be one of {', '.join(ROLES)}")
    with _add_lock, Store() as store:
        doc = store.add_document(Path(path).expanduser())
        if part:
            store.link(store.add_part(part, manufacturer), doc.id, role)
        idx = build_index(store, doc.id)
        el = build_elements(store, doc.id, element_backend())
        return (f"document {doc.sha256[:12]}: {doc.filename}, {doc.pages} pages; index: "
                f"{idx['outline']} outline entries, {idx['captions']} captions; element map: "
                f"{el['elements']} elements ({el['backend']})")


@tool
def get_index(document: str) -> str:
    """The index of a document: outline with pages, and the table and figure
    captions with their page, order and position on the page."""
    from .indexer import index_markdown

    with Store() as store:
        return index_markdown(store, _doc(store, document).id)


@tool
def get_outline(document: str, section: str) -> str:
    """The outline entries under a section number (for example 8.27), with pages.
    Use it for a large document, where get_index gives only the top levels."""
    from .indexer import outline_under

    with Store() as store:
        rows = outline_under(store, _doc(store, document).id, section)
        if not rows:
            return f"No outline entries under {section}."
        return "\n".join(f"{'  ' * r['level']}- {r['title']} (p.{r['page']})" for r in rows)


@tool
def search_text(text: str, document: str = "", part: str = "", max_lines: int = 30) -> str:
    """Find a text in the text layer (case-insensitive substring) of one document,
    or of all documents of a part. Gives the page and the line. Use it to find the
    page; read the values from the original (get_page_image)."""
    from .indexer import search

    with Store() as store:
        if document:
            doc = _doc(store, document)
            shas = {doc.id: doc.sha256}
        elif part:
            shas = {r["id"]: r["sha256"] for r in store.part_documents(part)}
        else:
            raise LensError("give a document or a part")
        hits = search(store, list(shas), text, max(1, min(max_lines, 200)))
        if not hits:
            return f"No line holds {text!r}."
        return "\n".join(f"{shas[d][:12]} p.{p}: {line}" for d, p, line in hits)


@tool
def get_page_map(document: str, page: int, all_elements: bool = False) -> str:
    """The structure of one page: the headings, tables, figures, formulas and
    captions with their IDs and regions (pt, top-left origin). The text elements
    are only counted, unless all_elements is true."""
    from .elements import page_map

    with Store() as store:
        return "\n".join(page_map(store, _doc(store, document).id, page, all_elements)) or "(no elements)"


@tool
def get_element_text(document: str, element_ids: list[str]) -> str:
    """The full text of elements (IDs from get_page_map, for example p7.P3)."""
    from .elements import element

    with Store() as store:
        doc = _doc(store, document)
        out = []
        for eid in element_ids:
            e = element(store, doc.id, eid)
            out.append(f"{eid} ({e['type']}, p.{e['page']}, [{e['x0']:.0f}, {e['y0']:.0f}, "
                       f"{e['x1']:.0f}, {e['y1']:.0f}]): {e['text']}")
        return "\n".join(out)


@tool
def list_items(document: str, kind: str = "") -> str:
    """The tables, figures or captions of the whole document, with ID, page and
    region. kind: table, figure, caption, or empty for all."""
    from .elements import list_items as items

    with Store() as store:
        rows = items(store, _doc(store, document).id, kind or None)
        if not rows:
            return "No items."
        return "\n".join(f"{r['element_id']}  p.{r['page']}  [{r['x0']:.0f}, {r['y0']:.0f}, {r['x1']:.0f}, "
                         f"{r['y1']:.0f}]  {r['text'][:100] or '(no caption)'}" for r in rows)


@tool
def get_page_image(document: str, page: int = 0, element: str = "", bbox: list[float] | None = None,
                   max_px: int = 2576) -> list:
    """The original as a PNG image: an element (ID from get_page_map), a region
    (bbox x0, y0, x1, y1 in pt, top-left origin) or a full page. A region or an
    element is rendered at 300 to 600 dpi; a full page at the dpi that fits max_px
    on its long edge. Read values from this image."""
    from .elements import element as get_element
    from .pages import page_image

    if element and bbox:
        raise LensError("give an element or a bbox, not the two")
    with Store() as store:
        doc = _doc(store, document)
        if element:
            e = get_element(store, doc.id, element)
            if page and page != e["page"]:
                raise LensError(f"element {element} is on page {e['page']}, not on page {page}")
            page = e["page"]
            pad = 6.0
            bbox = [e["x0"] - pad, e["y0"] - pad, e["x1"] + pad, e["y1"] + pad]
        if not page:
            raise LensError("give a page, an element or a page with a bbox")
        img = page_image(store, doc.id, page, tuple(bbox) if bbox else None, max_px)
        region = "full page" if img.bbox is None else "region [" + ", ".join(f"{v:.0f}" for v in img.bbox) + "]"
        meta = (f"{doc.sha256[:12]} page {img.page}, {region}, {img.dpi:.0f} dpi, "
                f"{img.width_px} x {img.height_px} px")
        return [meta, Image(path=img.path)]


@tool
def get_pdf_pages(document: str, pages: str) -> list:
    """The original pages as a new PDF (pages: "7" or "3-5,8", at most 50 pages).
    Gives the PDF as an embedded resource and its file path (a client that cannot
    read the resource can read the file)."""
    from .pages import parse_pages, pdf_pages

    with Store() as store:
        doc = _doc(store, document)
        count = len(parse_pages(pages, doc.pages or 0))
        if count > MAX_PDF_PAGES:
            raise LensError(f"{count} pages: ask for {MAX_PDF_PAGES} pages or fewer in one call")
        path, numbers = pdf_pages(store, doc.id, pages)
        blob = base64.b64encode(path.read_bytes()).decode("ascii")
        resource = EmbeddedResource(resource=BlobResourceContents(
            uri=path.resolve().as_uri(), mime_type="application/pdf", blob=blob))
        return [f"{doc.sha256[:12]} pages {pages} ({len(numbers)}): {path}", resource]


@tool
def save_fact(document: str, page: int, subject: str, value: str, unit: str = "", conditions: str = "",
              column: str = "", element: str = "", bbox: list[float] | None = None, note: str = "",
              model: str = "", part: str = "") -> str:
    """Save a value that you read from the original, with its source: document,
    page, element ID or bbox, the column (MIN/TYP/MAX) and the conditions. model:
    your model name. The fact gets the state 'unchecked'."""
    from .knowledge import save_fact as save

    with Store() as store:
        doc = _doc(store, document)
        fact_id = save(store, doc.id, page, subject, value, unit=unit, conditions=conditions, column=column,
                       element_id=element, bbox=bbox, note=note, source_kind="model", model=model, part=part)
        return f"fact #{fact_id} saved (unchecked)"


@tool
def find_facts(query: str = "", document: str = "", part: str = "", limit: int = 50) -> str:
    """Find saved facts by a text in the subject or the value, in one document, in
    the documents of a part, or in all documents. Each fact gives its source and
    state; check an important value against the original."""
    from .knowledge import find_facts as find
    from .knowledge import format_fact

    with Store() as store:
        if document:
            ids = [_doc(store, document).id]
        elif part:
            ids = [r["id"] for r in store.part_documents(part)]
        else:
            ids = None
        rows = find(store, query, ids, max(1, min(limit, 500)))
        return "\n".join(format_fact(r) for r in rows) or "No facts."


@tool
def set_fact_status(fact_id: int, status: str, note: str = "") -> str:
    """Set the state of a fact: unchecked, checked (compared with the original) or
    disputed (the original or another source disagrees; give the reason in note)."""
    from .knowledge import set_status

    with Store() as store:
        set_status(store, fact_id, status, note)
        return f"fact #{fact_id}: {status}"


def main() -> None:
    mcp.run("stdio")


__all__ = ["mcp", "main", "__version__"]
