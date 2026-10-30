"""MCP server (stdio): the step-by-step access of a model to component datasheets.

INSTRUCTIONS tells the model the method. The docstring of each tool and the
description of each parameter go to the model: write them for a model that
knows nothing about this tool. Tell what the tool does, when to use it, and what
it gives. Values come from the original pages (images or PDF pages); the index,
the page map and the text layer only find them.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import functools
import subprocess
import sys
import threading
import time

from typing import Annotated

import pypdfium2 as pdfium
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource
from pydantic import BaseModel, Field

from . import __version__
from .errors import LensError
from .store import ROLES, Store

# The parameters that more tools use: the same description everywhere.
DocRef = Annotated[str, Field(description=(
    "The document: its reference from list_documents, project_status or add_document (the first 12 "
    "characters of its SHA-256). A longer SHA-256 prefix or the numeric document ID also works."))]
PartRef = Annotated[str, Field(description="A part number (MPN) or an alias. Case is ignored.")]
Page = Annotated[int, Field(description="The page number, from 1, as get_index and search_text give it.")]
Manufacturer = Annotated[str, Field(description=(
    "The manufacturer, any of its names (TI or Texas Instruments: the store knows the names of one "
    "manufacturer). Necessary only when two parts have the same MPN."))]
Role = Annotated[str, Field(description="The role of the document: datasheet, manual, errata, appnote or other.")]
ProjectPath = Annotated[str, Field(description=(
    "A folder of the user project (default: the working folder of the server). The project file "
    "datasheet-lens.toml is in it or in a parent folder."))]
DryRun = Annotated[bool, Field(description="True: change nothing, only tell what would change.")]
DeletePinned = Annotated[bool, Field(description=(
    "True: also delete a file that the user put into the project folder (pinned = true)."))]

# The PDF of get_pdf_pages goes into one tool result: a larger request must
# use more calls.
MAX_PDF_PAGES = 50
REGION_PAGES = 10  # search_text gives the regions of the hits on this number of pages
MAX_IMAGES = 6  # get_page_image with a caption: the images of one result

# The SDK runs the tools in worker threads. Only one add_document (or change of
# the project file) at a time:
# a second call for the same file (for example a retry after a client timeout)
# then finds the current build and does not start a second one.
_add_lock = threading.Lock()

# The element map of a large document takes minutes with docling: longer than
# the timeout of an MCP client. A background thread builds it (with docling in a
# child process, see _run_build), and add_document waits only ELEMENT_WAIT
# seconds for it. The build state is in this process only: two server processes
# can build the same map. When the server stops, it stops its child processes
# (the next request of the map then starts the build again).
ELEMENT_WAIT = 20.0
_children: set[subprocess.Popen] = set()


@atexit.register
def _stop_children() -> None:
    """Stop the child processes (element map builds) when the server stops."""
    for child in list(_children):
        with contextlib.suppress(OSError):
            child.kill()


_builds: dict[int, dict] = {}  # document ID -> {"thread", "started", "result", "error"}
_builds_lock = threading.RLock()

INSTRUCTIONS = """\
datasheet-lens gives the content of component datasheets (PDF): an index, a text
search and the original pages. Read each value from the original (get_page_image
or get_pdf_pages), never from the text layer alone: the text layer can lose
signs, units, subscripts and the structure of tables. When a tool tells about a
newer release or a restart of the client, tell the user one time.

User project: the project file datasheet-lens.toml must hold the same parts as
the CAD project (schematic, BOM). This is critical: the team uses the file as
the list of the datasheets of the design. At the start of the work, and after
each change of the CAD project, do these steps:
1. Get the parts of the CAD project (MPN, Datasheet field) from the CAD tool,
   for example its MCP server or its BOM export. A part with no MPN (a generic
   resistor, a test point) needs no datasheet.
2. project_check(parts) with all of these parts: it gives the parts that are
   missing, extra or different, with the next call. Give path when the working
   folder of the server is not in the project.
3. For each missing part: find its datasheet (list_documents), or add it with
   add_document (the URL of the Datasheet field or of a distributor, or a local
   PDF). Then project_add_part(mpn, base, document). mpn is the concrete
   orderable MPN of the BOM (TLV75533PDBVR, get_ordering_codes gives them), base
   is the part of the datasheet (TLV755P). If you cannot get the datasheet, tell
   the user the MPN, the reason and the search links of part_links(mpn).
4. Tell the user about each different or extra part, and when a CAD MPN and its
   datasheet do not agree (value, package, variant). Change a part with
   project_update_part, and remove one only when the user agrees.
5. project_sync: it copies the documents into the project folder. Then
   project_check again: it must give no missing, extra or different part.
6. project_availability: tell the user about each part that is not orderable
   or has no stock, and give the user each note about a missing API key.
   search_distributors gives the concrete MPNs of a part.

The store identifies a document by its SHA-256: add_document of a file that it
has copies nothing and gives the reference. Do not match a file to a document
by its name only. add_document puts each new file into the central store: use
it only for the documents of the parts. project_sync imports only the PDF files
of the folder that the project file names. If the repository keeps its
datasheets in a different folder, tell the user.

Find the content step by step:
1. The document: project_status, list_documents(part), list_parts (the central
   store). A part can have a datasheet, a manual and errata.
2. find_facts(part or document): the values that a session read before. Do this
   before you read a value.
3. get_index: the outline and the table and figure captions, with pages.
   get_outline gives one section of a large document. list_items gives all
   tables and figures.
4. search_text: a word, a register name or a value, with page and line.
5. get_page_map: the elements of one page, with IDs and regions.
6. get_page_image with an element ID, a caption (all pages of a table) or a
   region (300 to 600 dpi): read the values here. For a table that fills the page, a full page at max_px 1600 is
   enough.
7. save_fact: save each value that the work uses, with its page and element.
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
    """The document of a reference, or a LensError that tells the model to use list_documents."""
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


def _run_build(doc_id: int, backend: str) -> dict:
    """Build the element map. The text backend runs here (it holds the pdfium
    lock). docling runs in a child process (the CLI command elements): it uses
    pdfium inside, and the lock of this process cannot reach it. A crash of
    docling then also does not stop the server."""
    from .elements import build_elements
    from .setup import element_backend

    if backend == "auto":
        backend = element_backend()
    if backend == "text":
        with Store() as store:
            return build_elements(store, doc_id, backend)
    command = [sys.executable, "-m", "datasheet_lens.cli", "elements", str(doc_id), "--backend", backend]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    _children.add(child)
    try:
        out, err = child.communicate()
    finally:
        _children.discard(child)
    if child.returncode != 0:
        lines = [line for line in (err or out).splitlines() if line.strip()]
        raise LensError(lines[-1] if lines else f"the build process stopped with code {child.returncode}")
    with Store() as store:
        count = store.db.execute("SELECT count(*) FROM elements WHERE document_id = ?", (doc_id,)).fetchone()[0]
    return {"elements": count, "backend": backend}


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
            try:
                job["result"] = _run_build(doc_id, job["backend"])
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
    """The parts in the store, with their manufacturer and the number of their
    documents. Gives one line for each part: "MPN (manufacturer): N documents".
    list_documents gives the documents of a part."""
    with Store() as store:
        rows = store.parts()
        if not rows:
            return "No parts. Add a document with add_document."
        return "\n".join(f"{r['mpn']} ({r['manufacturer'] or 'manufacturer unknown'}): "
                         f"{r['documents']} documents" for r in rows)


@tool
def list_documents(part: PartRef = "") -> str:
    """The documents in the store, or the documents of one part (a concrete part
    also gets the documents of its base part). Gives one line for each document:
    its reference (12 characters, for the other tools), the number of pages, the
    file name, the role or the parts, and the title."""
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
def add_document(
        path: Annotated[str, Field(description="A local PDF file, or an http or https URL of a PDF.")],
        part: Annotated[str, Field(description=(
            "The MPN that the datasheet describes: the base part, for example TLV755P (or one variant of "
            "a family). Empty: the result proposes an MPN from the PDF."))] = "",
        manufacturer: Manufacturer = "",
        role: Role = "datasheet",
        variants: Annotated[list[str] | None, Field(description=(
            "The other parts that the same datasheet covers, for example [\"ADC34J22\", \"ADC34J23\"] "
            "for ADC34J24. Only with part."))] = None) -> str:
    """Add a PDF document to the store: copy or download it, build its index and
    its element map, and link it to its part. Use it when list_documents does
    not find the datasheet; the URL can come from a distributor tool or from the
    Datasheet field of a CAD symbol. The element map of a large document can take
    minutes: a background thread builds it, and the index and the page images
    are available at once. Gives the document reference, the pages, the size of
    the index and the state of the element map. Without part, it also gives a
    rough proposal of the MPN from the text of the PDF; it can be wrong. Read the
    MPN on page 1 of the original, then call add_document again with part. To put
    the part into the user project: project_add_part."""
    from pathlib import Path

    from .config import store_home
    from .fetch import downloaded, is_url
    from .indexer import build_index

    if role not in ROLES:
        raise LensError(f"role must be one of {', '.join(ROLES)}")
    if variants and not part.strip():
        raise LensError("variants need part: the MPN that the datasheet describes")
    with contextlib.ExitStack() as stack:
        # The download runs before the lock: a slow server must not stop the other tools.
        if is_url(path):
            file, source = stack.enter_context(downloaded(path, store_home())), path.strip()
        else:
            file, source = Path(path).expanduser(), ""
        with _add_lock, Store() as store:
            doc = store.add_document(file, source_url=source, move=bool(source))
            idx = build_index(store, doc.id)
            # An MPN or an alias, as in the CLI command add. A failure is a line of
            # the result: the document, its index and its element map stay.
            failures = store.link_parts(doc.id, part, variants or [], manufacturer, role)[1] if part.strip() else []
            job = _build_elements(doc.id)
            proposal = "" if part.strip() or store.parts_of_document(doc.id) else _proposal(store, doc)
    job["thread"].join(ELEMENT_WAIT)
    state, text = _state(job)
    if state == "running":
        text += "; get_page_map tells when the map is complete"
    problems = "".join(f"\n{f}" for f in failures)
    return (f"document {doc.sha256[:12]}: {doc.filename}, {doc.pages} pages; index: "
            f"{idx['outline']} outline entries, {idx['captions']} captions; element map: {text}"
            + problems + proposal)


def _proposal(store: Store, doc) -> str:
    """The part that the PDF proposes, for a document with no part."""
    from .identify import propose

    p = propose(doc)
    if not p.mpn:
        return "\nno part: the PDF gives no MPN; find it (title, ordering information) and link it"
    covers = (f" The datasheet covers the variants {', '.join(p.variants)}: give one as part and the "
              "others as variants." if p.variants else "")
    return (f"\nno part: a rough proposal from the text of the PDF (it can be wrong): {p.mpn} "
            f"({store.known_maker(p.mpn, p.manufacturer) or 'manufacturer unknown'}; from {', '.join(p.sources)}"
            + (f"; other candidates: {', '.join(p.candidates)}" if p.candidates else "")
            + f").{covers} Read the MPN on page 1 of the original (get_page_image page 1), then link it: "
            f"add_document again with part (and variants). For the user "
            f"project: project_add_part(mpn=<the concrete orderable MPN>, base=<part>, "
            f"document='{doc.sha256[:12]}')")


@tool
def get_index(document: DocRef) -> str:
    """The index of a document: the outline (bookmarks or numbered headings) with
    pages, and the table and figure captions with their page and position. A
    large document gives only the top outline levels: get_outline gives the
    entries under a section. Also the pages with no text layer or with images of
    a low resolution. Start here to find where the content is."""
    from .indexer import index_markdown

    with Store() as store:
        return index_markdown(store, _indexed(store, [_doc(store, document).id])[0])


@tool
def get_outline(
        document: DocRef,
        section: Annotated[str, Field(description="A section number, for example \"8.27\".")]) -> str:
    """The outline entries under one section of a document, with pages. Use it
    for a large document, when get_index gives only the top levels."""
    from .indexer import outline_under

    with Store() as store:
        rows = outline_under(store, _indexed(store, [_doc(store, document).id])[0], section)
        if not rows:
            return f"No outline entries under {section}."
        return "\n".join(f"{'  ' * r['level']}- {r['title']} (p.{r['page']})" for r in rows)


@tool
def search_text(
        text: Annotated[str, Field(description=(
            "The text to find. Case is ignored. In mode text it is a substring (\"VDD\" also finds \"AVDD\"), "
            "and the space between a number and its unit is optional (\"100 nF\" also finds \"100nF\")."))],
        document: DocRef = "",
        part: PartRef = "",
        max_lines: Annotated[int, Field(description="The maximum number of lines (1 to 200).")] = 30,
        mode: Annotated[str, Field(description=(
            "text (a substring); word (whole words only: \"nF\" does not find \"configuration\"); or regex "
            "(a Python regular expression, for example \"Pin Functions?\")."))] = "text",
        repeated: Annotated[bool, Field(description=(
            "True: also the lines that repeat on many pages (page headers and footers)."))] = False) -> str:
    """Find a text in the text layer of one document, or of all documents of a
    part. Gives one line for each hit: the document reference, the page, the
    table or figure that holds the line (an element ID) or the region of the
    line, and the line. Give the element or the region to get_page_image: it
    costs less than a full page. The page headers and footers are not shown: a
    last line gives their
    number. Use it to find the page of a register, a parameter or a pin. Then
    read the value from the original (get_page_image): the text layer can be
    wrong."""
    from .indexer import hit_regions, search

    with Store() as store:
        if document:
            doc = _doc(store, document)
            shas = {doc.id: doc.sha256}
        elif part:
            shas = {r["id"]: r["sha256"] for r in store.part_documents(part)}
        else:
            raise LensError("give a document or a part")
        hidden: list = []
        limit = max(1, min(max_lines, 200))
        hits = search(store, _indexed(store, list(shas)), text, limit, mode, repeated, hidden)
        # The element or the region of each hit, for get_page_image (on the first pages only).
        wanted: dict[int, dict[int, list[str]]] = {}
        for d, p in list(dict.fromkeys((d, p) for d, p, _ in hits))[:REGION_PAGES]:
            wanted.setdefault(d, {})[p] = [line for dd, pp, line in hits if (dd, pp) == (d, p)]
        regions: dict[tuple[int, int, str], str] = {}
        for d, pages in wanted.items():
            with contextlib.suppress(OSError, pdfium.PdfiumError):
                regions.update({(d, p, line): r for (p, line), r in hit_regions(store, d, pages).items()})
        lines = [f"{shas[d][:12]} p.{p}" + (f" {regions[(d, p, line)]}" if (d, p, line) in regions else "")
                 + f": {line}" for d, p, line in hits] or [f"No line holds {text!r}."]
        if hidden:  # the search stops at the limit: then the count is a minimum
            lines.append(f"({'at least ' if len(hits) >= limit else ''}{len(hidden)} lines in page headers "
                         "or footers are not shown: repeated=true shows them)")
        return "\n".join(lines)


@tool
def get_page_map(
        document: DocRef,
        page: Page,
        all_elements: Annotated[bool, Field(description=(
            "True: also list each text element (paragraph, list item); else they are only counted."))] = False,
) -> str:
    """The structure of one page: headings, tables, figures, formulas and
    captions. Gives one line for each element: its ID (for example p7.T1), its
    type, its region [x0, y0, x1, y1] in pt (origin at the top left of the page)
    and its text or caption. Use the IDs with get_element_text and
    get_page_image. The element map of a large new document can take minutes to
    build. Until it is complete, the result tells so: then use get_index,
    search_text and get_page_image with a page or a region."""
    from .elements import page_map
    from .knowledge import facts_line

    with Store() as store:
        doc = _doc(store, document)
        note = _elements_ready(store, doc.id)
        lines = page_map(store, doc.id, page, all_elements) or ["(no elements)"]
        facts = facts_line(store, doc.id, page)
        return "\n".join(lines + [x for x in (note, facts) if x])


@tool
def get_element_text(
        document: DocRef,
        element_ids: Annotated[list[str], Field(description=(
            "Element IDs from get_page_map or list_items, for example [\"p7.T1\", \"p7.P3\"]."))]) -> str:
    """The full text of elements from the text layer, with their type, page and
    region. Use it for headings and paragraphs. Read table values, figures and
    formulas from get_page_image: the text layer can lose their structure."""
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
def list_items(
        document: DocRef,
        kind: Annotated[str, Field(description="table, figure or caption. Empty: all three.")] = "") -> str:
    """The tables, figures and captions of the whole document. Gives one line for
    each item: element ID, page, region and caption. Use it to find a table or a
    figure when you do not know its page."""
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
def get_page_image(
        document: DocRef,
        page: Annotated[int, Field(description="The page number, from 1. Not necessary with element.")] = 0,
        element: Annotated[str, Field(description=(
            "An element ID from get_page_map or list_items, for example p7.T1: the image shows this element."
        ))] = "",
        bbox: Annotated[list[float] | None, Field(description=(
            "Or a region [x0, y0, x1, y1] in pt, origin at the top left of the page (the units of "
            "get_page_map). Needs page."))] = None,
        max_px: Annotated[int, Field(description=(
            "The maximum long edge of a full-page image in pixels (256 to 8000). 1600 is enough for a "
            "table that fills the page. Use more for small print or a fine drawing."))] = 2576,
        caption: Annotated[str, Field(description=(
            "Or a caption label, for example \"Table 57\" or \"Figure 3-1\": the image shows this table or "
            "figure. A table that continues on the next pages gives one image for each page."))] = "") -> list:
    """The original as a PNG image: an element, a caption, a region or a full
    page. Read the values from this image. An element, a caption or a region is
    rendered at 300 to 600 dpi: prefer them, because a full page costs more
    input. A full page is correct for a table that fills the page. It is rendered
    at the resolution that fits max_px. Gives a line with the page, the region,
    the dpi and the size, and the image."""
    from .elements import element as get_element
    from .elements import find_caption
    from .knowledge import facts_line
    from .pages import page_image

    if sum(bool(x) for x in (element, bbox, caption)) > 1:
        raise LensError("give one of element, caption and bbox")
    pad = 6.0
    with Store() as store:
        doc = _doc(store, document)
        parts: list[tuple[int, list[float] | None]]
        note = ""
        if element or caption:
            _elements_ready(store, doc.id)
            if element:
                e = get_element(store, doc.id, element)
                found = [{"page": e["page"], "bbox": [e["x0"], e["y0"], e["x1"], e["y1"]]}]
            else:
                found = find_caption(store, doc.id, caption)
            what = f"element {element}" if element else f"caption {caption!r}"
            if page and page not in {f["page"] for f in found}:
                raise LensError(f"{what} is on page {', '.join(str(f['page']) for f in found)}, not on page {page}")
            parts = [(f["page"], [f["bbox"][0] - pad, f["bbox"][1] - pad, f["bbox"][2] + pad, f["bbox"][3] + pad]
                      if f["bbox"] else None) for f in found]
            if len(parts) > MAX_IMAGES:
                note = (f"the table continues on pages {', '.join(str(p) for p, _ in parts[MAX_IMAGES:])}: ask "
                        "for them with element IDs from get_page_map")
                parts = parts[:MAX_IMAGES]
        elif not page:
            raise LensError("give a page, an element, a caption or a page with a bbox")
        else:
            parts = [(page, bbox)]
        out: list = [note] if note else []
        for number, box in parts:
            img = page_image(store, doc.id, number, tuple(box) if box else None, max_px)
            region = "full page" if img.bbox is None else "region [" + ", ".join(f"{v:.0f}" for v in img.bbox) + "]"
            meta = (f"{doc.sha256[:12]} page {img.page}, {region}, {img.dpi:.0f} dpi, "
                    f"{img.width_px} x {img.height_px} px")
            facts = facts_line(store, doc.id, img.page)
            out += [meta + (f"\n{facts}" if facts else ""), Image(path=img.path)]
        return out


@tool
def get_pdf_pages(
        document: DocRef,
        pages: Annotated[str, Field(description="The pages, for example \"7\" or \"3-5,8\" (at most 50).")]
) -> list:
    """The original pages as a new PDF file. Use it when your client can read a
    PDF. Gives the file path and the PDF as an embedded resource."""
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
def save_fact(
        document: DocRef,
        page: Page,
        subject: Annotated[str, Field(description=(
            "What the value is, for example \"VDD maximum\" or \"CTRL register reset value\"."))],
        value: Annotated[str, Field(description=(
            "The value as the original shows it, for example \"3.6\". One value: for MIN, TYP and MAX, "
            "save one fact for each column. A range with no columns: \"1 to 22\"."))],
        unit: Annotated[str, Field(description="The unit, for example V, mA, °C.")] = "",
        conditions: Annotated[str, Field(description=(
            "The conditions of the value, for example \"TA = 25 °C, VDD = 3.3 V\"."))] = "",
        column: Annotated[str, Field(description="The table column of the value: MIN, TYP or MAX.")] = "",
        element: Annotated[str, Field(description="The element ID that holds the value, for example p7.T1.")] = "",
        bbox: Annotated[list[float] | None, Field(description=(
            "Or the region that holds the value: [x0, y0, x1, y1] in pt."))] = None,
        note: Annotated[str, Field(description="A note, for example a footnote of the table.")] = "",
        model: Annotated[str, Field(description="Your model ID, for example claude-opus-5-5.")] = "",
        part: Annotated[str, Field(description=(
            "The part that the value is for, when the document covers more parts."))] = "") -> str:
    """Save a value that you read from the original, with its source and your
    model ID, so that a later session finds it with find_facts. Before you read a
    value, look for it with find_facts: do not save a value two times. A new fact
    has the state unchecked. Gives the fact number."""
    from .knowledge import save_fact as save
    from .knowledge import similar_facts

    with Store() as store:
        doc = _doc(store, document)
        if element:
            _elements_ready(store, doc.id)
        similar = similar_facts(store, doc.id, page, subject, element)
        fact_id = save(store, doc.id, page, subject, value, unit=unit, conditions=conditions, column=column,
                       element_id=element, bbox=bbox, note=note, source_kind="model", model=model, part=part)
        out = f"fact #{fact_id} saved (unchecked)"
        if similar:
            out += ("\nnote: the same page has facts with this element or subject: "
                    + ", ".join(f"#{r['id']} {r['subject']}" for r in similar)
                    + ". If the new fact is a copy, set its state to disputed with set_fact_status.")
        return out


@tool
def find_facts(
        query: Annotated[str, Field(description="A text in the subject or the value. Empty: all facts.")] = "",
        document: DocRef = "",
        part: PartRef = "",
        limit: Annotated[int, Field(description="The maximum number of facts (1 to 500).")] = 50) -> str:
    """Find values that were read before (save_fact). With part, it searches the
    documents of the part and of its base part: a concrete MPN also finds the
    facts of its datasheet. Gives one line for each
    fact: number, subject, value, column, conditions, source (document, page,
    element or region), state and author. A fact is not the source of truth:
    check an important value against the original, then set its state with
    set_fact_status."""
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
def set_fact_status(
        fact_id: Annotated[int, Field(description="The fact number from find_facts or save_fact.")],
        status: Annotated[str, Field(description=(
            "unchecked; checked (you compared it with the original); or disputed (the original or a "
            "different source does not agree)."))],
        note: Annotated[str, Field(description=(
            "The reason, for example for disputed. It is added to the existing note."))] = "") -> str:
    """Set the state of a saved fact after you compared it with the original.
    Gives the fact number and its new state."""
    from .knowledge import set_status

    with Store() as store:
        set_status(store, fact_id, status, note)
        return f"fact #{fact_id}: {status}"


PROJECT_ADD_HINT = ("project_add_part(mpn='{part}'{base}, document='{sha}')", ", base='{base}'")


@tool
def project_status(path: ProjectPath = "") -> str:
    """The parts and documents of the user project, and the state of the copies
    in the project folder: missing or changed copies, new files, problems. It
    changes nothing. Gives one line for each part (MPN, manufacturer, base part,
    variants) and one line for each document (reference, role, file). A first
    line can tell about a newer release or a necessary restart of the LLM
    client: tell the user."""
    from .project import format_parts, format_report, load, sync
    from .version_check import version_notes

    project = load(path or None)
    with Store() as store:
        report = sync(project, store, apply=False)
    return "\n".join(version_notes() + format_parts(project) + format_report(report, False, PROJECT_ADD_HINT))


@tool
def project_sync(
        path: ProjectPath = "",
        restore: Annotated[bool, Field(description=(
            "True: copy a changed copy again from the store (the file in the folder is replaced)."
        ))] = False) -> str:
    """Synchronize the user project with the central store: add the documents
    that the store links to the project parts, copy the missing copies into the
    project folder, move the copies into the subfolders of the layout, and import
    the new PDF files of the folder. A new file gets a part only from the store;
    for a different file the result proposes a part, and project_add_part
    assigns it. Gives the changes and the problems."""
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
def project_add_part(
        mpn: Annotated[str, Field(description=(
            "The concrete orderable MPN that the design (the BOM) uses, with its package, voltage or "
            "grade, for example TLV75533PDBVR. A base part MPN only for a short time, when the BOM has no "
            "concrete one yet: the result then gives a note."))],
        manufacturer: Annotated[str, Field(description=(
            "The manufacturer, any of its names (the store knows the names of one manufacturer). The "
            "layout of the project folder can use it as a folder name: use the name that the project has "
            "already (project_status)."))] = "",
        document: Annotated[str, Field(description=(
            "A document to link first: its reference from project_status, list_documents or "
            "add_document. The link is in the central store (to the base part, if base is given), and "
            "other projects with this part get the document too."))] = "",
        role: Role = "datasheet",
        category: Annotated[str, Field(description=(
            "The function of the part, for example ADC, LDO, MCU, flash, sensor. The layout of the "
            "project folder can use it as a folder name: use the values that the project has "
            "already (project_status)."))] = "",
        base: Annotated[str, Field(description=(
            "The base part of the datasheet, for example TLV755P. The concrete part gets the documents "
            "of the base part."))] = "",
        path: ProjectPath = "") -> str:
    """Add a part to the user project, with the documents that the store links to
    it. Then project_sync copies the documents into the project folder. Gives the
    part and its documents."""
    from .project import add_part, load

    with _add_lock, Store() as store:
        project = load(path or None)
        part = add_part(project, store, mpn, manufacturer, document, role, category, base)
        from .identify import project_base_note

        note = project_base_note(store, part, "project_update_part(mpn='{mpn}', new_mpn='{code}', base='{base}')")
    return "\n".join([f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'}): "
                      f"{len(part['documents'])} documents"]
                     + [f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}"
                        for d in part["documents"]] + ([note] if note else []))


@tool
def manufacturer_relation(
        relation: Annotated[str, Field(description=(
            "alias: one manufacturer with two names (TI and Texas Instruments). distinct: two manufacturers "
            "that each make a part with the same MPN (a second source). Empty: only show the registry."))] = "",
        name: Annotated[str, Field(description="A manufacturer name.")] = "",
        other: Annotated[str, Field(description="The other manufacturer name.")] = ""
) -> str:
    """The manufacturer registry of the store: the names of each manufacturer,
    and the pairs that are two manufacturers. A part keeps the manufacturer
    name of its datasheet; the registry tells if two names are one
    manufacturer. When a tool reports that the registry does not know if two
    names are the same manufacturer, ask the user which relation is correct,
    then record it here and do the step again. Do not record a relation that the
    user did not confirm. Parts with the same MPN that become one manufacturer
    merge. Gives the merges, or the registry and the pairs of names that are
    probably one manufacturer (give them to the user to decide)."""
    with Store() as store:
        if not relation:
            return "\n".join(store.maker_listing())
        merged = store.maker_relation(relation, name, other)
    return "\n".join([f"recorded: {relation} {name!r} {other!r}"] + [f"merged: {m}" for m in merged])


@tool
def part_links(
        mpn: Annotated[str, Field(description="The MPN, best the concrete orderable one, for example TPS7A0233PYCHR.")]
) -> str:
    """Search links for a part: datasheet, price and stock (DigiKey, Mouser,
    Farnell, LCSC, Octopart, Findchips), and symbol, footprint and 3D model
    (Component Search Engine, SnapMagic, Ultra Librarian). Use them when the store
    and the CAD project have no datasheet, and for the CAD files. datasheet-lens
    does not open these pages: open one with a web tool, or give the links to the
    user. A datasheet that you find: add_document with its URL. Gives one line
    for each site."""
    from .links import format_links

    if not mpn.strip():
        raise LensError("the MPN is empty")
    return "\n".join(format_links(mpn))


@tool
def search_distributors(
        mpn: Annotated[str, Field(description=(
            "The MPN: a concrete orderable MPN, or the start of the MPNs (a base part, for example MCP1827S).")
        )],
        exact: Annotated[bool, Field(description=(
            "True: only this MPN. False: each MPN that starts with it (the concrete MPNs of a base part, "
            "up to 50)."))] = False,
        path: ProjectPath = "") -> str:
    """The orderable parts at the enabled distributors, from their official
    APIs: lifecycle state, stock, prices, package and datasheet URL. A part that
    is obsolete or discontinued is marked NOT ORDERABLE. Use it to select the
    concrete MPN of the BOM, and to check that it can be ordered. Each result
    names its source (the terms of the distributors ask for it). The enabled
    distributors come from the project file or the global configuration
    (distributor_status). Each needs a personal API key: for a distributor with
    no key, the result has a note with the steps for the user. Give the note
    to the user. With no key at all, the result gives the search links of
    part_links."""
    from .distributors import search_lines

    from .project import load_optional

    return "\n".join(search_lines(mpn, exact, load_optional(path or None)))


@tool
def project_availability(path: ProjectPath = "") -> str:
    """The lifecycle state and the stock of each part of the user project at
    the enabled distributors (official APIs). Gives one line for each part, and
    a warning for a part that is not orderable (obsolete, discontinued, with the
    replacement), that the distributor does not have, or that has no stock. Tell
    the user about each warning, and give the user each note about a missing
    API key (see search_distributors)."""
    from .distributors import availability
    from .project import load

    with Store() as store:
        return "\n".join(availability(load(path or None), store))


@tool
def distributor_status(path: ProjectPath = "") -> str:
    """The distributors that datasheet-lens asks (from the project file, the
    global configuration or the default), and if each has an API key (no key
    values). For a distributor with no key, a note with the steps for the user:
    give it to the user. The user changes the list with the CLI command
    "datasheet-lens dist NAME ..." (global) or "datasheet-lens dist --local NAME ..."
    (this project)."""
    from .distributors import status_lines
    from .project import load_optional
    from .version_check import version_notes

    return "\n".join(version_notes() + status_lines(load_optional(path or None)))


@tool
def get_ordering_codes(document: DocRef) -> str:
    """The orderable codes of a datasheet, from its text layer. Gives one line for
    each code: the code, its page and its line, which in an ordering table gives
    the package, the packing, the temperature and the state (ACTIVE, NRND ...).
    Use it to find the concrete orderable MPN for the BOM. A datasheet that gives
    only a scheme of its codes has no list: read its ordering information
    (get_page_image). A code of the datasheet is not always sold: check it with
    a distributor."""
    from .identify import ordering_codes

    with Store() as store:
        doc = _doc(store, document)
        rows = ordering_codes(store, _indexed(store, [doc.id])[0])
    if not rows:
        return ("No orderable codes in the text. The datasheet can give a scheme of its codes only: "
                "search_text \"ordering\" and read the page with get_page_image.")
    table = [f"{code}  p.{page}  {line}" for code, page, line, t in rows if t]
    other = [f"{code}  p.{page}  {line}" for code, page, line, t in rows if not t]
    return "\n".join((["Codes at the start of a line (an ordering table):"] + table if table else [])
                     + (["Other codes in the text (a variant, an example or a register name):"] + other
                        if other else []))


class CadPart(BaseModel):
    """One part of the CAD project (a symbol or a BOM line)."""
    mpn: str = Field("", description="The MPN field of the symbol, as it is (empty if there is none).")
    value: str = Field("", description=(
        "The Value field of the symbol, as it is: it can hold a value (100nF), an MPN, a value and an MPN "
        "(\"390uF (EEU-FS1H391L)\") or alternatives (\"AP2205-33YR-13 / AP7370-33Y-13\"). The tool finds the "
        "MPNs in it."))
    datasheet: str = Field("", description="The Datasheet field: a URL or a path. Empty if there is none.")
    reference: str = Field("", description="The reference of the symbol, for example U3.")


@tool
def project_check(
        parts: Annotated[list[CadPart], Field(description=(
            "The parts of the CAD project, from the CAD tool (for example its MCP server or its BOM export). "
            "One item for each symbol or BOM line, with its MPN and Value fields as they are; give each "
            "part, not only the new ones. A part with no MPN in the two fields needs no datasheet."))],
        path: ProjectPath = "") -> str:
    """Compare the project file with the parts of the CAD project, so that the
    two hold the same parts. Each part with an MPN needs its datasheet, also a
    passive part with an MPN in its Value. Gives one line for each symbol whose
    MPN and Value fields name different parts (a conflict in the CAD project),
    the number of the same parts, and one line for each part that is different
    (one part with two MPNs), missing (with the documents of the store or the
    Datasheet field, and the next call; an alternative of a part is missing
    too) or extra. It changes nothing."""
    from .project import check_parts, format_check, load
    from .version_check import version_notes

    with Store() as store:
        project = load(path or None)
        result = check_parts(project, store, [p.model_dump() for p in parts])
    return "\n".join(version_notes() + format_check(result))


@tool
def project_update_part(
        mpn: Annotated[str, Field(description="The MPN of the project part now.")],
        manufacturer: Manufacturer = "",
        new_mpn: Annotated[str, Field(description=(
            "The new MPN, for example the concrete orderable MPN TMP112D0IDPWR in place of TMP112D0. "
            "Empty: no change."))] = "",
        base: Annotated[str, Field(description=(
            "The base part of the datasheet, for example TMP112D. Empty: no change."))] = "",
        category: Annotated[str | None, Field(description=(
            "The function of the part. None: no change; an empty text removes it."))] = None,
        new_manufacturer: Annotated[str, Field(description=(
            "The new manufacturer, any name of the manufacturer of the part in the store (TI or Texas "
            "Instruments). The project file and its layout use this name. Empty: no change."))] = "",
        path: ProjectPath = "") -> str:
    """Change a part of the user project in place: its MPN, base part, category
    or manufacturer. The part keeps its place in datasheet-lens.toml and its
    copies (project_remove_part and project_add_part would delete and copy them
    again). Then project_sync moves a copy when the layout gives it a different
    subfolder. Gives the part and its documents."""
    from .project import load, update_part

    with _add_lock, Store() as store:
        project = load(path or None)
        part = update_part(project, store, mpn, manufacturer, new_mpn, base, category, new_manufacturer)
    return "\n".join([f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'})"
                      + (f", base part {part['base']}" if part.get("base") else "")
                      + f": {len(part['documents'])} documents"]
                     + [f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}"
                        for d in part["documents"]])


def _removal(result: dict, folder: str, dry_run: bool, what: str) -> str:
    """The result of a removal from the project as lines."""
    lines = [f"{'would remove' if dry_run else 'removed'}: {what}"]
    lines += [f"  {'would delete' if dry_run else 'deleted'}: {folder}/{rel}" for rel in result["deleted"]]
    lines += [f"  kept: {folder}/{text}" for text in result["kept"]]
    return "\n".join(lines)


@tool
def project_remove_part(
        mpn: Annotated[str, Field(description="The MPN of the project part.")],
        manufacturer: Manufacturer = "",
        delete_pinned: DeletePinned = False,
        dry_run: DryRun = False,
        path: ProjectPath = "") -> str:
    """Remove a part from the user project, and delete its copies that no other
    part uses. A file that the user put into the folder (pinned) stays, unless
    delete_pinned is true. The central store does not change, because other
    projects can use the part. To change the MPN of a part, use
    project_update_part. Gives the removed part and the deleted and kept
    files."""
    from .project import load, remove_part

    with _add_lock, Store() as store:
        project = load(path or None)
        result = remove_part(project, mpn, manufacturer, delete_pinned, not dry_run, store)
    return _removal(result, project.folder, dry_run, f"part {result['part']}")


@tool
def project_remove_document(
        document: Annotated[str, Field(description=(
            "The document: its reference, a SHA-256 prefix, its document ID, or its file name in the "
            "project folder."))],
        mpn: Annotated[str, Field(description="Only from this part. Empty: from each part that has it.")] = "",
        manufacturer: Manufacturer = "",
        delete_pinned: DeletePinned = False,
        dry_run: DryRun = False,
        path: ProjectPath = "") -> str:
    """Remove a document from the parts of the user project; the parts stay. The
    project remembers it (exclude), so that project_sync does not add it again;
    project_add_part with this document adds it again. The copy is deleted when
    no part uses it. The central store does not change."""
    from .project import load, remove_document

    with _add_lock, Store() as store:
        if document.strip().isdigit() and len(document.strip()) < 8:  # a document ID
            document = _doc(store, document.strip()).sha256
        project = load(path or None)
        result = remove_document(project, document, mpn, manufacturer, delete_pinned, not dry_run, store)
    return _removal(result, project.folder, dry_run,
                    f"document {result['document']} from {', '.join(result['parts'])}")


def main() -> None:
    """Run the MCP server on stdin and stdout. A background thread asks PyPI for
    a newer release (see version_check)."""
    from .version_check import start_update_check

    start_update_check()
    mcp.run("stdio")


__all__ = ["mcp", "main", "__version__"]
