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
from .pdflock import pdfium_locked
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


@pdfium_locked
def _read_pages(path: str) -> tuple[list[PageText], list[dict], list[dict]]:
    """The text, the quality and the outline of each page. The pdfium lock is
    held only here, not during the database writes."""
    pdf = pdfium.PdfDocument(path)
    try:
        pages: list[PageText] = []
        quality = []
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                pt = page_lines(page, i + 1)
                pages.append(pt)
                q = {}
                if sum(len(line.text) for line in pt.lines) < 20:
                    q["no_text_layer"] = True
                dpi = raster_min_dpi(page)
                if dpi is not None and dpi < LOW_DPI:
                    q["raster_dpi"] = round(dpi)
                quality.append(q)
            finally:
                page.close()
        return pages, quality, _bookmarks(pdf) or _headings(pages)
    finally:
        pdf.close()


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
    pages, quality, outline = _read_pages(str(doc.path))
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
    """The outline from the bookmarks of the PDF: level, title and page of each entry."""
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


def require_index(store: Store, doc_ids) -> None:
    """Raise a LensError if a document has no index. Without this check, an
    empty result of an outline or a search can look like "not in the document"."""
    for doc_id in [doc_ids] if isinstance(doc_ids, int) else doc_ids:
        if store.build(doc_id, "index") is None:
            doc = store.document(doc_id)
            name = f"{doc.sha256[:12]} {doc.filename}" if doc else str(doc_id)
            raise LensError(f"the document {name} has no index (the command datasheet-lens index "
                            "builds it)")


def index_markdown(store: Store, doc_id: int) -> str:
    """The index of one document as Markdown, for the model."""
    require_index(store, doc_id)
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
    """The outline entries under a section number (8.27 gives 8.27.1, 8.27.5.143 ...),
    in the sequence of the document. A LensError if the document has no index."""
    require_index(store, doc_id)
    return store.db.execute(
        "SELECT level, title, page FROM outline WHERE document_id = ? AND (title LIKE ? OR title LIKE ?)"
        " ORDER BY seq", (doc_id, section + ".%", section + " %")).fetchall()


SEARCH_MODES = ("text", "word", "regex")
UNIT_START = "µΩ°%"
# A line that repeats on this part of the pages (and on 3 pages or more) is a
# page header or footer.
REPEATED_PART = 0.5
# (SHA-256, time of the index build) -> the keys of the repeated lines.
_repeated: dict[tuple[str, str], set[str]] = {}
MAX_REPEATED_CACHE = 64


PAGE_OFFSET = 10  # a printed page number can differ from the page of the PDF


def _line_key(line: str, page: int) -> str:
    """The key of a line for the header check. The page number does not count:
    a number word near the page of the PDF ("GD25LE64E-Rev1.3  12  March 2022").
    Other numbers count: "Address offset: 0x504" and "0x508" differ."""
    return " ".join("#" if re.fullmatch(r"\d{1,4}", w) and abs(int(w) - page) <= PAGE_OFFSET else w
                    for w in line.strip().lower().split())


def repeated_lines(store: Store, doc_id: int) -> set[str]:
    """The keys of the lines that repeat on many pages of a document: the page
    headers and footers."""
    built = store.build(doc_id, "index")
    # Not the ID: a different store can use the same ID. A new index has a new time.
    key = (store.document(doc_id).sha256, built["built"] if built else "")
    if key not in _repeated:
        if len(_repeated) >= MAX_REPEATED_CACHE:
            _repeated.clear()
        counts: dict[str, int] = {}
        pages = 0
        for row in store.db.execute("SELECT page, text FROM pages WHERE document_id = ?", (doc_id,)):
            pages += 1
            for line_key in {_line_key(line, row["page"]) for line in row["text"].splitlines() if line.strip()}:
                counts[line_key] = counts.get(line_key, 0) + 1
        least = max(3, REPEATED_PART * pages)
        _repeated[key] = {k for k, n in counts.items() if n >= least}
    return _repeated[key]


def search_pattern(query: str, mode: str = "text") -> re.Pattern:
    """The pattern of a query. text: a substring, case is ignored; a space
    between a number and a unit is optional ("100 nF" finds "100nF" too), and a
    different space matches any space. word: the same, as whole words only.
    regex: a Python regular expression, case is ignored."""
    if mode not in SEARCH_MODES:
        raise LensError(f"mode must be one of {', '.join(SEARCH_MODES)}")
    q = re.sub(r"\s+", " ", query.strip())
    if not q:
        raise LensError("the search text is empty")
    if mode == "regex":  # as the user gives it: a space can be part of it
        try:
            return re.compile(query, re.I)
        except re.error as e:
            raise LensError(f"not a regular expression: {e}") from e
    out = []
    for i, ch in enumerate(q):
        unit = i > 0 and q[i - 1].isdigit()
        if ch == " ":
            nxt = q[i + 1] if i + 1 < len(q) else ""
            out.append(r"\s*" if unit and (nxt.isalpha() or nxt in UNIT_START) else r"\s+")
        else:
            if unit and (ch.isalpha() or ch in UNIT_START):
                out.append(r"\s*")
            out.append(re.escape(ch))
    pattern = "".join(out)
    if mode == "word":
        # A unit after a number is a word: "nF" finds "100nF", not "configuration".
        # A query that starts or ends with a sign ("-40", "I2C:") has no word
        # boundary on that side.
        before = r"(?<![^\W\d_])" if q[0].isalpha() else r"(?<!\w)" if q[0].isalnum() or q[0] == "_" else ""
        after = r"(?!\w)" if q[-1].isalnum() or q[-1] == "_" else ""
        pattern = f"{before}{pattern}{after}"
    return re.compile(pattern, re.I)


def search(store: Store, doc_ids: list[int], query: str, limit: int = 30, mode: str = "text",
           repeated: bool = False, hidden: list | None = None) -> list[tuple]:
    """Give (document_id, page, line) for the lines that match the query (see
    search_pattern). The same on each platform (no FTS5: its token match
    differs, and not every SQLite has it). A line that repeats on many pages (a
    page header or footer) is left out, unless repeated is true; hidden gets
    one item for each line that is left out."""
    rx = search_pattern(query, mode)
    if not doc_ids:
        return []
    require_index(store, doc_ids)
    sql = f"SELECT document_id, page, text FROM pages WHERE document_id IN ({','.join('?' * len(doc_ids))})"
    args: list = list(doc_ids)
    # A quick filter by the longest word of the query (not for a regex). A number
    # and its unit are two words: "100nF" can be "100 nF" in the text.
    words = re.findall(r"\d+|[^\W\d_]+", query.lower()) if mode != "regex" else []
    if words:
        sql += " AND instr(ulower(text), ?) > 0"
        args.append(max(words, key=len))
    out = []
    for row in store.db.execute(sql + " ORDER BY document_id, page", args).fetchall():
        doc_id, page, text = row["document_id"], row["page"], row["text"]
        skip = set() if repeated else repeated_lines(store, doc_id)
        for line in text.splitlines():
            if rx.search(line):
                if _line_key(line, page) in skip:
                    if hidden is not None:
                        hidden.append((doc_id, page))
                    continue
                out.append((doc_id, page, re.sub(r"\s{2,}", "  ", line.strip())[:160]))
                if len(out) >= limit:
                    return out
    return out
