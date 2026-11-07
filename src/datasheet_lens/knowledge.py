"""Knowledge: values that a model or a person read from a document, with their source.

A fact keeps the document, page, element and region it comes from, who read it
(model and prompt version, or person), and a verification state. A fact is never
the source of truth: the original page is. The state tells how much to trust it.
"""

from __future__ import annotations

import json
import math

from .errors import LensError
from .store import Store, now

STATUSES = ("unchecked", "checked", "disputed")


def save_fact(store: Store, doc_id: int, page: int, subject: str, value: str, *, unit: str = "",
              conditions: str = "", column: str = "", element_id: str = "", bbox=None, note: str = "",
              source_kind: str = "model", model: str = "", prompt_version: str = "", author: str = "",
              part: str = "") -> int:
    """Save a fact (a value that a model or a person read from the original) with its
    source, and give its ID. A LensError for a wrong page, element, region or part."""
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    if not 1 <= page <= (doc.pages or 0):
        raise LensError(f"page {page} is not in the document (1 to {doc.pages})")
    if not subject.strip() or not value.strip():
        raise LensError("a fact needs a subject and a value")
    if source_kind not in ("model", "person"):
        raise LensError("source_kind must be 'model' or 'person'")
    if bbox is not None:
        try:
            bbox = [float(v) for v in bbox]
        except (TypeError, ValueError):
            bbox = []
        if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise LensError("bbox needs four numbers x0,y0,x1,y1 (pt), with x0 < x1 and y0 < y1")
    if element_id:
        row = store.db.execute("SELECT page, x0, y0, x1, y1 FROM elements WHERE document_id = ? AND element_id = ?",
                               (doc.id, element_id)).fetchone()
        if row is None:
            raise LensError(f"element not found: {element_id}")
        if row["page"] != page:
            raise LensError(f"element {element_id} is on page {row['page']}, not on page {page}")
        if bbox is None:  # the region is the reference of the fact; the element ID follows the element map
            bbox = [row["x0"], row["y0"], row["x1"], row["y1"]]
    part_id = store.find_part(part) if part else None
    cur = store.db.execute(
        "INSERT INTO knowledge (part_id, document_id, page, element_id, bbox, subject, value, unit,"
        " conditions, column_name, note, source_kind, model, prompt_version, author, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (part_id, doc.id, page, element_id, json.dumps(list(bbox)) if bbox else "", subject.strip(),
         value.strip(), unit, conditions, column, note, source_kind, model, prompt_version, author, now()))
    store.db.commit()
    return cur.lastrowid


def find_facts(store: Store, query: str = "", doc_ids: list[int] | None = None, limit: int = 50) -> list:
    """Facts whose subject or value holds the query (case-insensitive substring),
    in the given documents (all documents if None)."""
    sql = ("SELECT k.*, d.sha256, d.filename, p.mpn FROM knowledge k JOIN documents d ON d.id = k.document_id"
           " LEFT JOIN parts p ON p.id = k.part_id WHERE 1 = 1")
    args: list = []
    if query.strip():
        sql += " AND (instr(ulower(k.subject), ?) > 0 OR instr(ulower(k.value), ?) > 0)"
        args += [query.strip().lower()] * 2
    if doc_ids is not None:
        if not doc_ids:
            return []
        sql += f" AND k.document_id IN ({','.join('?' * len(doc_ids))})"
        args += doc_ids
    sql += " ORDER BY k.document_id, k.page, k.id LIMIT ?"
    return store.db.execute(sql, (*args, limit)).fetchall()


def page_facts(store: Store, doc_id: int, page: int) -> list:
    """The facts of one page of a document: id, subject, element_id."""
    return store.db.execute("SELECT id, subject, element_id FROM knowledge WHERE document_id = ? AND page = ?"
                            " ORDER BY id", (doc_id, page)).fetchall()


def facts_line(store: Store, doc_id: int, page: int, limit: int = 10) -> str:
    """One line with the facts of a page, so that a session sees them before it
    reads the page again, or "" when the page has none."""
    rows = page_facts(store, doc_id, page)
    if not rows:
        return ""
    shown = ", ".join(f"#{r['id']} {r['subject'][:40]}" for r in rows[:limit])
    more = f" and {len(rows) - limit} more" if len(rows) > limit else ""
    return f"known facts on this page (find_facts gives the values): {shown}{more}"


def _same_text(a: str, b: str) -> bool:
    """True for two texts that differ only in case and spaces ("TA = 25 °C" and
    "TA=25°C")."""
    return "".join((a or "").split()).casefold() == "".join((b or "").split()).casefold()


def similar_facts(store: Store, doc_id: int, subject: str, value: str = "", unit: str = "", conditions: str = "",
                  column: str = "", part: str = "", element_id: str = "") -> list:
    """The facts of the document that a new fact can be a copy of (case and
    spaces are ignored):
    - the same subject, conditions and column, also from a different page (a
      summary table and the full table);
    - the same element, value, unit and column: one cell of a table under a
      different subject ("VS max" and "Supply voltage max").
    A fact of the same table with a different subject and a different value is
    not a copy: one table holds many values. A fact with no part agrees with a
    fact of each part: the part is optional. An unknown part gives no fact
    (save_fact gives the error)."""
    try:
        part_id = store.find_part(part) if part.strip() else None
    except LensError:
        return []
    rows = store.db.execute("SELECT id, page, element_id, subject, value, unit, conditions, column_name, part_id"
                            " FROM knowledge WHERE document_id = ? ORDER BY id", (doc_id,)).fetchall()

    def same_part(r) -> bool:
        return part_id is None or r["part_id"] is None or r["part_id"] == part_id

    def same_value(r) -> bool:
        return (_same_text(r["subject"], subject) and _same_text(r["conditions"], conditions)) or (
            bool(element_id) and r["element_id"] == element_id and _same_text(r["value"], value)
            and _same_text(r["unit"], unit))

    return [r for r in rows if same_value(r) and _same_text(r["column_name"], column) and same_part(r)]


def set_status(store: Store, fact_id: int, status: str, note: str = "") -> None:
    """Set the state. A note is added to the existing note, with the state."""
    if status not in STATUSES:
        raise LensError(f"status must be one of {', '.join(STATUSES)}")
    note = f"{status}: {note.strip()}" if note.strip() else ""
    cur = store.db.execute(
        "UPDATE knowledge SET status = ?, note = CASE WHEN ? = '' THEN note WHEN note = '' THEN ?"
        " ELSE note || '; ' || ? END WHERE id = ?", (status, note, note, note, fact_id))
    if cur.rowcount == 0:
        raise LensError(f"fact not found: {fact_id}")
    store.db.commit()


def format_fact(r) -> str:
    """One line for a fact: number, subject = value and unit, column, conditions,
    source, state, author and note."""
    where = f"{r['sha256'][:12]} p.{r['page']}" + (f" {r['element_id']}" if r["element_id"] else "")
    if r["bbox"]:
        where += " [" + ", ".join(f"{v:.0f}" for v in json.loads(r["bbox"])) + "]"
    value = r["value"] + (f" {r['unit']}" if r["unit"] else "")
    part = r["mpn"] if "mpn" in r.keys() else ""
    extra = "; ".join(x for x in (f"part {part}" if part else "",
                                  f"column {r['column_name']}" if r["column_name"] else "",
                                  f"conditions: {r['conditions']}" if r["conditions"] else "") if x)
    who = r["model"] or r["author"] or r["source_kind"]
    return (f"#{r['id']} {r['subject']} = {value}" + (f" ({extra})" if extra else "")
            + f" — {where}, {r['status']}, by {who}" + (f", note: {r['note']}" if r["note"] else ""))
