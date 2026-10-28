"""Index build: page text, outline, captions, page quality, index.md.

The index only finds the content. The model reads the content from the original
pages (see pages.py).
"""

from __future__ import annotations

import hashlib
import json
import re

import pypdfium2 as pdfium

from . import __version__
from .errors import LensError
from .pdftext import PageText, page_lines, raster_min_dpi
from .store import Store, now

# A numbered heading: "3.2.1. CONTROL_1 (address 00h)" or "5.1 Absolute Maximum Ratings".
# The space after the number can be missing in the text layer ("3.2.1.CONTROL_1").
# Number: a chapter of 1 or 2 digits, then up to 4 levels of up to 3 digits
# ("8.27.5.143 DOEPCTL3"); no level starts with 0. Values such as "105.0 Tj Max",
# "125.0 MHz", "192.168.1.1" and "1.00 Au80" are not headings.
# The first word of the title: two or more letters; digits after 3 or more capital
# letters ("DOEPCTL3", "UART0") or after "_" ("CONTROL_1"). "6.91 Co: 7440-48-4"
# and "5.0 V" are not headings.
# A title can hold "…" ("(address 00h…bits description)"), but no sentence end.
HEADING_RE = re.compile(r"^([1-9][0-9]?\.(?:[1-9][0-9]{0,2}\.){0,4}(?:[1-9][0-9]{0,2})?)\s*"
                        r"((?:[A-Z]{3,}[0-9]*|[A-Z][A-Za-z]+)(?:_[A-Za-z0-9]+)*(?![A-Za-z0-9:])"
                        r"(?:[^.]|\.(?!\s|$)){0,100}?)\s*$")
# A table of contents line: a dot leader, or a page number after a column gap. A
# heading can end with a number ("4.2 Timer 2"): one space is not a column gap.
TOC_RE = re.compile(r"\.{4,}|\s{3,}[0-9]{1,4}\s*$")
# A caption at the start of a line or after a column gap. The text starts with an
# upper-case letter or a digit: "Figure 6-1 shows ..." in body text is not a caption.
# The text layer can have no space after the label ("Figure 4-1.OPA134: ...").
CAPTION_RE = re.compile(r"(?:^|\s{3,})((?:Table|Tab\.|Figure|Fig\.)\s*[0-9]+(?:-[0-9]+)?)"
                        r"(?:[.:]\s*|\s+)([A-Z0-9][^\s].{1,150}?)(?=\s{3,}|$)")
# A short upper-case line: a heading of a page with no numbered headings.
CAPS_RE = re.compile(r"^([A-Z][A-Z0-9 ,/&()+\-]{3,60}[A-Z0-9)])$")

MAX_OUTLINE = 300       # more entries: index.md shows only the top 2 levels
LOW_DPI = 300           # raster images below this dpi are marked
SHORT_DOCUMENT = 10     # a page map is in index.md for documents up to this length


def build_index(store: Store, doc_id: int, force: bool = False) -> dict:
    """Build the index of a document. An index of the same tool version is kept,
    unless force is true."""
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    if not force and store.build_current(doc.id, "index", __version__, "pdfium"):
        count = lambda t: store.db.execute(f"SELECT count(*) FROM {t} WHERE document_id = ?",  # noqa: E731
                                           (doc.id,)).fetchone()[0]
        return {"pages": count("pages"), "outline": count("outline"), "captions": count("captions"),
                "skipped": True}
    pdf = pdfium.PdfDocument(str(doc.path))
    pages: list[PageText] = []
    quality = []
    for i in range(len(pdf)):
        page = pdf[i]
        pt = page_lines(page, i + 1)
        pages.append(pt)
        q = {}
        if sum(len(line.text) for line in pt.lines) < 20:
            q["no_text_layer"] = True
        dpi = raster_min_dpi(page)
        if dpi is not None and dpi < LOW_DPI:
            q["raster_dpi"] = round(dpi)
        quality.append(q)
        page.close()
    outline = _bookmarks(pdf) or _headings(pages)
    pdf.close()
    captions = _captions(pages)

    db = store.db
    with db:
        for table in ("pages", "outline", "captions"):
            db.execute(f"DELETE FROM {table} WHERE document_id = ?", (doc.id,))
        for pt, q in zip(pages, quality):
            text = pt.text
            db.execute("INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (doc.id, pt.page, pt.width, pt.height, len(text), text, json.dumps(q),
                        page_fingerprint(pt)))
        db.executemany("INSERT INTO outline VALUES (?, ?, ?, ?, ?, ?)",
                       [(doc.id, n, o["level"], o["title"], o["page"], o["source"])
                        for n, o in enumerate(outline)])
        db.executemany("INSERT INTO captions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(doc.id, n, c["kind"], c["label"], c["caption"], c["page"], c["x"], c["y"],
                         c["order"], c["count"]) for n, c in enumerate(captions)])
        db.execute("INSERT OR REPLACE INTO builds VALUES (?, 'index', ?, 'pdfium', ?)",
                   (doc.id, __version__, now()))
    return {"pages": len(pages), "outline": len(outline), "captions": len(captions), "skipped": False}


MARGIN_PT = 50  # header and footer band: distributor stamps, page numbers, dates


def page_fingerprint(pt: PageText) -> str:
    """SHA-256 of the page text without white space and without the lines in the
    header and footer bands. Empty for a page with no text."""
    body = "".join(line.text for line in pt.lines
                   if line.y0 > MARGIN_PT and line.y1 < pt.height - MARGIN_PT)
    body = re.sub(r"\s+", "", body)
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if body else ""


def _bookmarks(pdf: pdfium.PdfDocument) -> list[dict]:
    out = []
    for item in pdf.get_toc():
        dest = item.get_dest()
        index = dest.get_index() if dest else None
        if index is not None:
            out.append({"level": item.level, "title": item.get_title().strip(), "page": index + 1,
                        "source": "bookmark"})
    return out


def _headings(pages: list[PageText]) -> list[dict]:
    """Numbered headings from the text layer, for a PDF with no bookmarks."""
    out, seen = [], set()
    for pt in pages:
        for line in pt.lines:
            m = HEADING_RE.match(line.text.strip())
            if not m or TOC_RE.search(line.text):
                continue
            number = m.group(1).rstrip(".")
            if number in seen:
                continue
            seen.add(number)
            out.append({"level": number.count("."), "title": f"{number} {m.group(2).strip()}",
                        "page": pt.page, "source": "text"})
    return out


def _captions(pages: list[PageText]) -> list[dict]:
    """Table and figure captions with their position on the page. A caption that
    repeats (a "continued" table) is kept on its first page only."""
    out, seen = [], set()
    for pt in pages:
        found = []
        for line in pt.lines:
            for m in CAPTION_RE.finditer(line.text):
                label = m.group(1).rstrip(".:")
                key = (label, m.group(2).strip())
                if key in seen:
                    continue
                seen.add(key)
                found.append({"kind": "table" if label.lower().startswith("tab") else "figure",
                              "label": label, "caption": m.group(2).strip(), "page": pt.page,
                              "x": round(line.x_at(m.start(1))), "y": round(line.y0)})
        found.sort(key=lambda c: (round(c["y"] / 20), c["x"]))
        for k, c in enumerate(found, start=1):
            c["order"], c["count"] = k, len(found)
        out.extend(found)
    return out


def index_markdown(store: Store, doc_id: int) -> str:
    """The index of one document as Markdown, for the model."""
    doc = store.document(doc_id)
    db = store.db
    pages = db.execute("SELECT page, width_pt, height_pt, text, quality FROM pages"
                       " WHERE document_id = ? ORDER BY page", (doc.id,)).fetchall()
    outline = db.execute("SELECT level, title, page FROM outline WHERE document_id = ? ORDER BY seq",
                         (doc.id,)).fetchall()
    captions = db.execute("SELECT * FROM captions WHERE document_id = ? ORDER BY seq",
                          (doc.id,)).fetchall()
    lines = [f"# {doc.filename}", "", f"document: {doc.sha256[:12]}  pages: {doc.pages}"]
    if doc.title:
        lines.append(f"title: {doc.title}")
    lines.append("")
    image_only = [str(p["page"]) for p in pages if json.loads(p["quality"]).get("no_text_layer")]
    low_dpi = [f"{p['page']} ({json.loads(p['quality'])['raster_dpi']} dpi)" for p in pages
               if "raster_dpi" in json.loads(p["quality"])]
    if image_only:
        lines += ["Pages that are only an image (no text layer; read them as images): "
                  + ", ".join(image_only), ""]
    if low_dpi:
        lines += ["Pages with a raster image below 300 dpi (a higher render adds no information): "
                  + ", ".join(low_dpi), ""]
    lines += ["## Outline", ""]
    shown = outline
    if len(outline) > MAX_OUTLINE:
        shown = [o for o in outline if o["level"] <= 1]
        lines += [f"Only the top 2 levels of {len(outline)} entries. Ask for the outline under "
                  "a section number for the deeper entries.", ""]
    lines += [f"{'  ' * o['level']}- {o['title']} (p.{o['page']})" for o in shown]
    if not outline:
        lines.append("(no bookmarks and no numbered headings)")
    lines += ["", "## Tables and figures", ""]
    sizes = {p["page"]: (p["width_pt"], p["height_pt"]) for p in pages}
    for c in captions:
        w, h = sizes[c["page"]]
        where = f"{'top' if c['y_pt'] < h / 2 else 'bottom'} {'left' if c['x_pt'] < w / 2 else 'right'}"
        lines.append(f"- {c['label']}. {c['caption']} (p.{c['page']}, {c['page_order']}/"
                     f"{c['page_count']}, {where}, caption at y={round(c['y_pt'])} pt)")
    if not captions:
        lines.append("(no captions found)")
    if not outline or len(pages) <= SHORT_DOCUMENT:
        lines += ["", "## Page map", ""]
        for p in pages:
            heads = []
            for t in p["text"].splitlines():
                m = CAPS_RE.match(t.strip())
                if m and m.group(1) not in heads:
                    heads.append(m.group(1))
            lines.append(f"- p.{p['page']}: {'; '.join(heads[:12]) or '(no headings)'}")
    return "\n".join(lines) + "\n"


def outline_under(store: Store, doc_id: int, section: str) -> list:
    return store.db.execute(
        "SELECT level, title, page FROM outline WHERE document_id = ? AND (title LIKE ? OR title LIKE ?)"
        " ORDER BY seq", (doc_id, section + ".%", section + " %")).fetchall()


def search(store: Store, doc_ids: list[int], query: str, limit: int = 30) -> list[tuple]:
    """Give (document_id, page, line) for the lines that hold the query text. The
    match is a case-insensitive substring ("VDD" finds "AVDD"), the same on each
    platform (no FTS5: its token match differs, and not every SQLite has it)."""
    needle = query.strip().lower()
    if not needle:
        raise LensError("the search text is empty")
    if not doc_ids:
        return []
    out = []
    rows = store.db.execute(
        f"SELECT document_id, page, text FROM pages WHERE document_id IN ({','.join('?' * len(doc_ids))})"
        " AND instr(lower(text), ?) > 0 ORDER BY document_id, page", (*doc_ids, needle)).fetchall()
    for row in rows:
        doc_id, page, text = row["document_id"], row["page"], row["text"]
        for line in text.splitlines():
            if needle in line.lower():
                out.append((doc_id, page, re.sub(r"\s{2,}", "  ", line.strip())[:160]))
                if len(out) >= limit:
                    return out
    return out
