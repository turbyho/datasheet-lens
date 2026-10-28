"""Element map: the elements of each page with type, text, bbox and a stable ID.

Two backends:
  docling  the docling layout model (optional, "datasheet-lens[layout]"): headings,
           paragraphs, tables, figures, formulas with their regions.
  text     the text layer only (always available): headings, text blocks and
           captions. It has no regions of tables and figures; the model asks for a
           region near the caption.

IDs: p<page>.<code><n> in reading order (top to bottom, left to right). Codes:
H heading, P paragraph, L list item, T table, F figure, Q formula, C caption
(text backend), N footnote, K code, R page header or footer.
"""

from __future__ import annotations

import importlib.util
import re

import pypdfium2 as pdfium

from . import __version__
from .errors import LensError
from .indexer import CAPS_RE, CAPTION_RE, HEADING_RE, TOC_RE
from .pdflock import pdfium_locked
from .pdftext import Line, page_lines
from .store import Store, now

STRUCTURE = ("H", "T", "F", "Q", "C")   # the codes that page_map shows by default
DOCLING_CODE = {"section_header": "H", "title": "H", "text": "P", "paragraph": "P",
                "list_item": "L", "table": "T", "picture": "F", "chart": "F", "formula": "Q",
                "caption": "C", "footnote": "N", "page_header": "R", "page_footer": "R",
                "code": "K", "document_index": "P"}


def docling_available() -> bool:
    return importlib.util.find_spec("docling") is not None


def build_elements(store: Store, doc_id: int, backend: str = "auto", chunk: int = 50,
                   force: bool = False) -> dict:
    """Build the element map of a document, and replace the old one. A map of the
    same tool version and backend is kept, unless force is true."""
    if backend == "auto":
        backend = "docling" if docling_available() else "text"
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    if not force and store.build_current(doc.id, "elements", __version__, backend):
        count = store.db.execute("SELECT count(*) FROM elements WHERE document_id = ?", (doc.id,)).fetchone()[0]
        return {"elements": count, "backend": backend, "skipped": True}
    if backend == "docling":
        if not docling_available():
            raise LensError("the docling backend is not installed (run: datasheet-lens init)")
        raw = _docling_elements(str(doc.path), doc.pages or 0, chunk)
    elif backend == "text":
        raw = _text_elements(str(doc.path))
    else:
        raise LensError(f"unknown element backend: {backend}")
    rows = _number(raw)
    with store.db:
        store.db.execute("DELETE FROM elements WHERE document_id = ?", (doc.id,))
        store.db.executemany(
            "INSERT INTO elements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(doc.id, e["id"], e["page"], e["type"], e["code"], e["order"], *e["bbox"], e["text"])
             for e in rows])
        store.db.execute("INSERT OR REPLACE INTO builds VALUES (?, 'elements', ?, ?, ?)",
                         (doc.id, __version__, backend, now()))
    return {"elements": len(rows), "backend": backend, "skipped": False}


def _number(raw: list[dict]) -> list[dict]:
    """Give IDs in reading order, and to a table or figure with no caption the
    heading above it."""
    by_page: dict[int, list[dict]] = {}
    for e in raw:
        by_page.setdefault(e["page"], []).append(e)
    out, heading = [], ""
    for page in sorted(by_page):
        counters: dict[str, int] = {}
        items = sorted(by_page[page], key=lambda e: (round(e["bbox"][1] / 10), e["bbox"][0]))
        for order, e in enumerate(items, start=1):
            if e["code"] == "H":
                heading = e["text"]
            elif e["code"] in ("T", "F") and not e["text"] and heading:
                e["text"] = f"(under heading: {heading[:80]})"
            counters[e["code"]] = counters.get(e["code"], 0) + 1
            e["id"] = f"p{page}.{e['code']}{counters[e['code']]}"
            e["order"] = order
            out.append(e)
    return out


def _docling_elements(path: str, pages: int, chunk: int) -> list[dict]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # Layout only: no table structure (the model reads tables from the image), no OCR.
    opts = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    out = []
    for start in range(1, pages + 1, chunk):
        end = min(start + chunk - 1, pages)
        doc = conv.convert(path, page_range=(start, end)).document
        # A caption of a detected table or figure is given with it; a caption that
        # no table or figure refers to stays a caption element.
        referenced = {ref.cref for t in list(doc.tables) + list(doc.pictures)
                      for ref in (getattr(t, "captions", None) or [])}
        for item, _level in doc.iterate_items():
            if not getattr(item, "prov", None):
                continue
            label = str(getattr(item.label, "value", item.label))
            code = DOCLING_CODE.get(label, "P")
            if code == "C" and item.self_ref in referenced:
                continue
            text = (item.caption_text(doc) or "") if code in ("T", "F") else (getattr(item, "text", "") or "")
            for prov in item.prov:
                h = doc.pages[prov.page_no].size.height
                bb = prov.bbox.to_top_left_origin(page_height=h)
                box = [bb.l, bb.t, bb.r, bb.b]
                if code in ("T", "F"):
                    # The region of a table or figure includes its caption.
                    for ref in getattr(item, "captions", None) or []:
                        for cp in ref.resolve(doc).prov:
                            if cp.page_no == prov.page_no:
                                cb = cp.bbox.to_top_left_origin(page_height=h)
                                box = [min(box[0], cb.l), min(box[1], cb.t), max(box[2], cb.r), max(box[3], cb.b)]
                out.append({"page": prov.page_no, "type": label, "code": code,
                            "text": re.sub(r"\s+", " ", text).strip(), "bbox": [round(v, 1) for v in box]})
    return out


COLUMN_TOLERANCE = 30.0  # pt: a line belongs to a block if its left edge is this near


@pdfium_locked
def _text_elements(path: str) -> list[dict]:
    """Headings, captions and text blocks from the text layer. A line with column
    gaps is split into its columns; each column builds its own blocks."""
    pdf = pdfium.PdfDocument(path)
    out = []
    try:
        for i in range(len(pdf)):
            pt = page_lines(pdf[i], i + 1)
            open_blocks: list[list[Line]] = []
            for line in pt.lines:
                parts = line.columns()
                for part in parts:
                    # An upper-case heading is a full line; a part of a line with
                    # column gaps is a table cell ("RW DMAADDR" of a register table).
                    kind = _line_kind(part, caps=len(parts) == 1)
                    if kind:
                        out.append(_element(i + 1, *kind, [part]))
                        # A heading or caption ends the block of its column.
                        open_blocks = [b for b in open_blocks if not _same_column(b, part)]
                        continue
                    block = next((b for b in open_blocks if _same_column(b, part)
                                  and part.y0 - b[-1].y1 <= (b[-1].y1 - b[-1].y0)), None)
                    if block is None:
                        open_blocks = [b for b in open_blocks if not _same_column(b, part)]
                        block = []
                        open_blocks.append(block)
                        out.append({"lines": block, "page": i + 1})
                    block.append(part)
    finally:
        pdf.close()
    return [_element(e["page"], "text", "P", e["lines"]) if "lines" in e else e for e in out]


def _same_column(block: list[Line], line: Line) -> bool:
    return abs(block[0].x0 - line.x0) <= COLUMN_TOLERANCE


def _line_kind(line: Line, caps: bool = True) -> tuple[str, str] | None:
    t = line.text.strip()
    if CAPTION_RE.match(t):
        return ("caption", "C")
    if HEADING_RE.match(t) and not TOC_RE.search(line.text):
        return ("section_header", "H")
    if caps and CAPS_RE.match(t) and _is_caps_heading(t):
        return ("section_header", "H")
    return None


def _is_caps_heading(text: str) -> bool:
    """An upper-case line is a heading if it has a word of 3 or more letters, and
    single letters are not the most words ("ID L K J I H G" of a bit layout is not)."""
    words = text.split()
    singles = sum(1 for w in words if len(w) == 1)
    return any(len(w) >= 3 for w in words) and singles * 2 < len(words)


def _element(page: int, type_: str, code: str, lines: list[Line]) -> dict:
    return {"page": page, "type": type_, "code": code,
            "text": re.sub(r"\s+", " ", " ".join(l.text for l in lines)).strip(),
            "bbox": [round(min(l.x0 for l in lines), 1), round(min(l.y0 for l in lines), 1),
                     round(max(l.x1 for l in lines), 1), round(max(l.y1 for l in lines), 1)]}


def page_map(store: Store, doc_id: int, page: int, everything: bool = False) -> list[str]:
    """The structure of a page (headings, tables, figures, formulas, captions); the
    other elements only as a count, unless everything is true."""
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    if not 1 <= page <= (doc.pages or 0):
        raise LensError(f"page {page} is not in the document (1 to {doc.pages})")
    rows = store.db.execute("SELECT * FROM elements WHERE document_id = ? AND page = ? ORDER BY page_order",
                            (doc_id, page)).fetchall()
    if not rows and not has_elements(store, doc_id):
        raise LensError("the document has no element map (build it with the command: datasheet-lens elements)")
    lines, rest = [], []
    for r in rows:
        if r["code"] == "R":
            continue
        if everything or r["code"] in STRUCTURE:
            text = r["text"] if r["code"] in ("H", "T", "F", "C") else r["text"][:60]
            lines.append(f"{r['element_id']:9} {r['type']:14} [{r['x0']:.0f}, {r['y0']:.0f}, "
                         f"{r['x1']:.0f}, {r['y1']:.0f}]  {text}")
        else:
            rest.append(r["element_id"])
    if rest:
        lines.append(f"+ {len(rest)} text elements ({rest[0]} … {rest[-1]}); "
                     "ask for all elements of the page, or for the text of an element")
    return lines


def has_elements(store: Store, doc_id: int) -> bool:
    """True if the document has an element map (it can have no elements)."""
    return store.build(doc_id, "elements") is not None


def element(store: Store, doc_id: int, element_id: str):
    row = store.db.execute("SELECT * FROM elements WHERE document_id = ? AND element_id = ?",
                           (doc_id, element_id)).fetchone()
    if row is None:
        raise LensError(f"element not found: {element_id}")
    return row


def list_items(store: Store, doc_id: int, kind: str | None = None) -> list:
    codes = {"table": ("T",), "figure": ("F",), "caption": ("C",), None: ("T", "F", "C")}.get(kind)
    if codes is None:
        raise LensError(f"unknown item type: {kind} (table, figure, caption)")
    return store.db.execute(
        f"SELECT * FROM elements WHERE document_id = ? AND code IN ({','.join('?' * len(codes))})"
        " ORDER BY page, page_order", (doc_id, *codes)).fetchall()
