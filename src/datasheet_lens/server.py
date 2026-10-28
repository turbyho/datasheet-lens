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
import time

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

# The SDK runs the tools in worker threads. Only one add_document (or change of
# the project file) at a time:
# a second call for the same file (for example a retry after a client timeout)
# then finds the current build and does not start a second one.
_add_lock = threading.Lock()

# The element map of a large document takes minutes with docling: longer than
# the timeout of an MCP client. A background thread builds it, and add_document
# waits only ELEMENT_WAIT seconds for it. The build state is in this process
# only: two server processes can build the same map, and a build stops when its
# process stops (the next request of the map then starts it again).
ELEMENT_WAIT = 20.0
_builds: dict[int, dict] = {}  # document ID -> {"thread", "started", "result", "error"}
_builds_lock = threading.RLock()

INSTRUCTIONS = """\
datasheet-lens gives the content of component datasheets (PDF). It does not convert
a datasheet to text: read the values from the original pages.

Go into a document step by step, and ask only for what the work needs:
1. list_parts / list_documents: find the documents of a part (a part can have a
   datasheet, a manual, errata). project_status gives the parts of the user
   project; project_sync and project_add_part keep its documents current.
2. get_index: the outline and the table and figure captions, with pages.
3. get_outline: the entries under one section (large documents).
4. search_text: find a word or a value in the text layer (page and line).
5. get_page_map: the headings, tables, figures and formulas of one page, with IDs.
   The element map of a large new document can take minutes to build. Until it
   is complete, use steps 2, 4 and 7 with a page or a region.
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


def _indexed(store: Store, doc_ids: list[int]) -> list[int]:
    """Build the index of a document that has none (for example a document that
    an older version added with no index): the model cannot run the CLI."""
    from .indexer import build_index

    for doc_id in doc_ids:
        if store.build(doc_id, "index") is None:
            with _add_lock:
                build_index(store, doc_id)
    return doc_ids


def _build_elements(doc_id: int) -> dict:
    """Start the element map build of a document in a background thread, if no
    build of the document runs. Give the build. A failed build starts again only
    from here (add_document), not from a read tool: a build that always fails must
    not run again for each request."""
    from .setup import element_backend

    with _builds_lock:
        job = _builds.get(doc_id)
        if job and job["thread"].is_alive():
            return job
        job = {"started": time.monotonic(), "result": None, "error": None, "backend": element_backend()}

        def run():
            from .elements import build_elements

            try:
                with Store() as store:
                    job["result"] = build_elements(store, doc_id, job["backend"])
            except Exception as e:  # the next request of the map gives it to the model
                job["error"] = f"{e.__class__.__name__}: {e}"
            finally:
                if job["result"] is None and job["error"] is None:
                    job["error"] = "the build stopped"

        job["thread"] = threading.Thread(target=run, name=f"elements-{doc_id}", daemon=True)
        _builds[doc_id] = job
        job["thread"].start()
        return job


def _state(job: dict | None) -> tuple[str, str]:
    """The state of a build (none, running, failed, done) and its text."""
    if job is None:
        return "none", ""
    if job["thread"].is_alive():
        return "running", (f"a background thread builds the element map ({job['backend']}, "
                           f"{time.monotonic() - job['started']:.0f} s)")
    if job["error"]:
        return "failed", f"the element map build failed: {job['error']} (add_document starts it again)"
    return "done", f"{job['result']['elements']} elements ({job['result']['backend']})"


def _elements_ready(store: Store, doc_id: int) -> str:
    """Raise a LensError with the state of the build if the document has no
    element map. Start the build if the document has no map and no build.
    Give a note if the map is older than a rebuild that failed."""
    from .elements import has_elements

    alternative = "Until the map is complete, use get_index, search_text and get_page_image with a page or a bbox."
    with _builds_lock:
        state, text = _state(_builds.get(doc_id))
        if has_elements(store, doc_id):
            return f"note: {text}; this map is from an older build" if state == "failed" else ""
        if state in ("running", "failed"):
            raise LensError(f"{text}. {alternative}")
        _build_elements(doc_id)
    raise LensError(f"the document has no element map. A background thread starts to build it now. {alternative}")


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
    """Add a PDF to the store, link it to a part, and build its index and element
    map. path: a local file, or an http or https URL of the PDF (for example the
    datasheet URL from a part registry, a distributor or a CAD symbol); the tool
    downloads it and records the URL. role: datasheet, manual, errata, appnote or
    other. The element map of a large document can take minutes: it is then built
    in the background, and the index and the page tools are available at once. To
    copy the document into the user project: project_add_part, then project_sync."""
    import contextlib
    from pathlib import Path

    from .config import store_home
    from .fetch import downloaded, is_url
    from .indexer import build_index

    if role not in ROLES:
        raise LensError(f"role must be one of {', '.join(ROLES)}")
    with contextlib.ExitStack() as stack:
        # The download runs before the lock: a slow server must not stop the other tools.
        if is_url(path):
            file, source = stack.enter_context(downloaded(path, store_home())), path.strip()
        else:
            file, source = Path(path).expanduser(), ""
        with _add_lock, Store() as store:
            doc = store.add_document(file, source_url=source, move=bool(source))
            if part:
                store.link(store.add_part(part, manufacturer), doc.id, role)
            idx = build_index(store, doc.id)
            job = _build_elements(doc.id)
    job["thread"].join(ELEMENT_WAIT)
    state, text = _state(job)
    if state == "running":
        text += "; get_page_map tells when the map is complete"
    return (f"document {doc.sha256[:12]}: {doc.filename}, {doc.pages} pages; index: "
            f"{idx['outline']} outline entries, {idx['captions']} captions; element map: {text}")


@tool
def get_index(document: str) -> str:
    """The index of a document: outline with pages, and the table and figure
    captions with their page, order and position on the page."""
    from .indexer import index_markdown

    with Store() as store:
        return index_markdown(store, _indexed(store, [_doc(store, document).id])[0])


@tool
def get_outline(document: str, section: str) -> str:
    """The outline entries under a section number (for example 8.27), with pages.
    Use it for a large document, where get_index gives only the top levels."""
    from .indexer import outline_under

    with Store() as store:
        rows = outline_under(store, _indexed(store, [_doc(store, document).id])[0], section)
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
        hits = search(store, _indexed(store, list(shas)), text, max(1, min(max_lines, 200)))
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
        doc = _doc(store, document)
        note = _elements_ready(store, doc.id)
        lines = page_map(store, doc.id, page, all_elements) or ["(no elements)"]
        return "\n".join(lines + ([note] if note else []))


@tool
def get_element_text(document: str, element_ids: list[str]) -> str:
    """The full text of elements (IDs from get_page_map, for example p7.P3)."""
    from .elements import element

    with Store() as store:
        doc = _doc(store, document)
        _elements_ready(store, doc.id)
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
        doc = _doc(store, document)
        _elements_ready(store, doc.id)
        rows = items(store, doc.id, kind or None)
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
            _elements_ready(store, doc.id)
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
        if element:
            _elements_ready(store, doc.id)
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


PROJECT_ADD_HINT = "project_add_part(mpn='{mpn}', document='{sha}')"


@tool
def project_status(path: str = "") -> str:
    """The parts of the user project and their documents (document IDs for the
    other tools), and the copies in the project folder that are missing or
    changed. The project file datasheet-lens.toml is in path or in a parent
    folder (default: the working folder of the server)."""
    from .project import format_parts, format_report, load, sync

    project = load(path or None)
    with Store() as store:
        report = sync(project, store, apply=False)
    return "\n".join(format_parts(project) + format_report(report, False, PROJECT_ADD_HINT))


@tool
def project_sync(path: str = "", restore: bool = False) -> str:
    """Synchronize the project with the central store: add the documents that the
    store links to the parts of the project, copy the missing copies into the
    project folder, and import the new PDF files of the folder (with index and
    element map). A new file gets a part only from the store; for the other files
    the result proposes a part, and project_add_part assigns it. restore: copy a
    changed copy again from the store."""
    from .project import format_report, load, sync

    with _add_lock, Store() as store:
        project = load(path or None)
        report = sync(project, store, restore=restore)
        queued = _build_in_sequence(store, report["documents"])
    lines = format_report(report, True, PROJECT_ADD_HINT)
    if queued:
        lines.append(f"element map: a background thread builds the maps of {queued} documents, one after "
                     "the other; get_page_map tells when a map is complete")
    return "\n".join(lines)


def _build_in_sequence(store: Store, doc_ids: list[int]) -> int:
    """Build the element maps of the documents that have no map and no build, one
    after the other in one background thread. Give their number."""
    from .elements import has_elements

    todo = [d for d in doc_ids if not has_elements(store, d) and _state(_builds.get(d))[0] == "none"]
    if todo:
        threading.Thread(target=lambda: [_build_elements(d)["thread"].join() for d in todo],
                         name="elements-sequence", daemon=True).start()
    return len(todo)


@tool
def project_add_part(mpn: str, manufacturer: str = "", document: str = "", role: str = "datasheet",
                     category: str = "", path: str = "") -> str:
    """Add a part to the user project, with the documents that the store links to
    it. document (ID from project_status or list_documents): link this document
    to the part first, for example a new file of the project folder. category:
    the function of the part (for example ADC, LDO, MCU); the layout of the
    project folder can use it. Then project_sync copies the documents into the
    folder."""
    from .project import add_part, load

    with _add_lock, Store() as store:
        project = load(path or None)
        part = add_part(project, store, mpn, manufacturer, document, role, category)
    return "\n".join([f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'}): "
                      f"{len(part['documents'])} documents"]
                     + [f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}"
                        for d in part["documents"]])


def main() -> None:
    mcp.run("stdio")


__all__ = ["mcp", "main", "__version__"]
