"""The bundles of derived data: the index and the element map of a document as
one file on the team server.

The first client that builds the derived data of a document uploads it as a
bundle; the other clients download the canonical bundle and do not build. Thus
the team has the same element IDs. For a document, a kind and a format, the
canonical bundle is the first one of the best backend (docling, then text), then
by created and blob_sha: the same order as on the server (team/sync.py).

A bundle is gzip JSON with no time in it: the same build gives the same file.
It needs no PDF: a store gets the index and the element map of a document with
no file.

A fact keeps the bbox of its element: the bbox is the reference of the fact. The
element ID and the note CHANGED_NOTE of a fact are results of its bbox and of the
local element map: each store computes them again (relink) after a new element map
and for each fact that it pulls, with no outbox rows. A fact that a store pushes can
have the values of its local map; each other store computes them again.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import sqlite3
from pathlib import Path

from .errors import LensError
from .fileutil import temp_path
from .store import Document, Store, not_recorded, now

BUNDLE_FORMAT = 1
# The order of the bundles (also on the team server, team/sync.py): the best backend
# first, then the first bundle that the server recorded (its time), then blob_sha.
BACKEND_RANK = {"docling": 0, "text": 1, "pdfium": 2}
BUNDLE_BACKENDS = {"index": ("pdfium",), "elements": ("docling", "text")}


def canonical_key(row) -> tuple:
    return (BACKEND_RANK[row["backend"]], row["created"], row["blob_sha"])
# The tables and columns of each kind (without document_id).
TABLES = {
    "index": {
        "pages": ("page", "width_pt", "height_pt", "chars", "text", "quality", "text_sha"),
        "outline": ("seq", "level", "title", "page", "source"),
        "captions": ("seq", "kind", "label", "caption", "page", "x_pt", "y_pt", "page_order", "page_count"),
    },
    "elements": {
        "elements": ("element_id", "page", "type", "code", "page_order", "x0", "y0", "x1", "y1", "text"),
    },
}
MATCH = 0.5        # the overlap (intersection over union) of the same element in two builds
CHANGED_NOTE = "element changed; check again"


def kind_format(kind: str) -> str:
    from .elements import ELEMENTS_FORMAT
    from .indexer import INDEX_FORMAT

    return {"index": INDEX_FORMAT, "elements": ELEMENTS_FORMAT}[kind]


def tool_versions(backend: str) -> str:
    """The versions of the tools of a build (JSON): a bundle tells them."""
    names = ["datasheet-lens", "pypdfium2"] + (["docling"] if backend == "docling" else [])
    found = {}
    for name in names:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = "unknown"
    return json.dumps(found, sort_keys=True)


# Export and import

def export(store: Store, doc: Document, kind: str) -> bytes:
    """The bundle of the current build of a kind (gzip JSON, the same bytes for
    the same build)."""
    build = store.build(doc.id, kind)
    if build is None:
        raise LensError(f"the document {doc.sha256[:12]} has no {kind} build")
    tables = {}
    for table, columns in TABLES[kind].items():
        order = "element_id" if table == "elements" else columns[0]
        rows = store.db.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE document_id = ? ORDER BY {order}",
                                (doc.id,)).fetchall()
        tables[table] = {"columns": list(columns), "rows": [list(r) for r in rows]}
    data = {"bundle_format": BUNDLE_FORMAT, "document": doc.sha256, "kind": kind, "format": build["tool_version"],
            "backend": build["backend"], "versions": json.loads(build["versions"] or "{}"), "tables": tables}
    return gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0)


def import_bundle(store: Store, doc: Document, kind: str, raw: bytes, blob_sha: str) -> dict:
    """Replace the build of a kind with a bundle. Give the numbers of the rows."""
    try:
        data = json.loads(gzip.decompress(raw))
    except (OSError, ValueError) as e:
        raise LensError(f"the bundle {blob_sha[:12]} is not gzip JSON: {e}") from None
    if data.get("bundle_format") != BUNDLE_FORMAT or data.get("document") != doc.sha256 or data.get("kind") != kind:
        raise LensError(f"the bundle {blob_sha[:12]} is not a {kind} bundle of the document {doc.sha256[:12]}")
    tables = data.get("tables") or {}
    if set(tables) != set(TABLES[kind]) or any(tables[t].get("columns") != list(c) for t, c in TABLES[kind].items()):
        raise LensError(f"the bundle {blob_sha[:12]} has different tables or columns (an other version?)")
    db = store.db
    with db, not_recorded(db):
        for table, columns in TABLES[kind].items():
            db.execute(f"DELETE FROM {table} WHERE document_id = ?", (doc.id,))
            db.executemany(f"INSERT INTO {table} (document_id, {', '.join(columns)})"
                           f" VALUES (?{', ?' * len(columns)})", [(doc.id, *r) for r in tables[table]["rows"]])
        db.execute("INSERT OR REPLACE INTO builds (document_id, kind, tool_version, backend, built, bundle, versions)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)", (doc.id, kind, data["format"], data["backend"], now(), blob_sha,
                                                     json.dumps(data.get("versions") or {}, sort_keys=True)))
        if kind == "elements":
            relink(db, doc.id)
    return {table: len(tables[table]["rows"]) for table in TABLES[kind]}


# The canonical bundle

def canonical(db: sqlite3.Connection, sha256: str, kind: str, fmt: str) -> sqlite3.Row | None:
    rows = db.execute("SELECT * FROM bundles WHERE document_sha = ? AND kind = ? AND format = ?",
                      (sha256, kind, fmt)).fetchall()
    rows = [r for r in rows if r["backend"] in BACKEND_RANK]  # a backend of a newer version: not known here
    return min(rows, key=canonical_key) if rows else None


def from_server(store: Store, doc: Document, kind: str, backend: str = "") -> dict | None:
    """The canonical bundle of the team server for the build of a kind: imported
    when the local build is not that bundle. None when the store is not
    connected, the server has no bundle, the bundle has a worse backend than the
    requested backend, or the bundle cannot come (the caller builds locally; a
    sync note tells why, when it is not a missing connection)."""
    from . import teamsync

    if not teamsync.connected(store):
        return None
    best = canonical(store.db, doc.sha256, kind, kind_format(kind))
    if best is None or (backend and BACKEND_RANK[best["backend"]] > BACKEND_RANK[backend]):
        return None
    build = store.build(doc.id, kind)
    if build is not None and build["bundle"] == best["blob_sha"]:
        return {"skipped": True, "backend": build["backend"], "bundle": best["blob_sha"]}
    try:
        raw = teamsync.remote(store).fetch_blob(best["blob_sha"])
        counts = import_bundle(store, doc, kind, raw, best["blob_sha"])
    except teamsync.Offline:
        return None
    except LensError as e:  # a wrong bundle of the server: the user sees it; the tool builds locally
        with store.db:
            teamsync.note(store.db, f"the {kind} bundle {best['blob_sha'][:12]} of the team server for "
                                    f"{doc.sha256[:12]} cannot be used ({e}); the store builds it locally")
        return None
    return {**counts, "skipped": False, "backend": best["backend"], "bundle": best["blob_sha"]}


def publish(store: Store, doc: Document, kind: str, client) -> str:
    """Upload the local build of a kind as a bundle, and record it. When the
    server has a better or an earlier bundle, import that one. Give the line of
    the result."""
    raw = export(store, doc, kind)
    sha = hashlib.sha256(raw).hexdigest()
    path = temp_path(store.home / "tmp" / f"bundle-{sha}.json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(raw)
        client.upload(store, path, sha, "application/gzip")
    finally:
        path.unlink(missing_ok=True)
    build = store.build(doc.id, kind)
    answer = client.call("POST", "/v1/bundles", {
        "document_sha": doc.sha256, "kind": kind, "format": build["tool_version"], "backend": build["backend"],
        "blob_sha": sha, "versions": json.loads(build["versions"] or "{}")})["canonical"]
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (answer["document_sha"], answer["kind"], answer["format"], answer["backend"],
                          answer["blob_sha"], answer["versions"], answer["created"]))
    if answer["blob_sha"] == sha:
        with store.db:
            store.db.execute("UPDATE builds SET bundle = ? WHERE document_id = ? AND kind = ?", (sha, doc.id, kind))
        return f"{kind} of {doc.sha256[:12]}: uploaded as the canonical bundle"
    import_bundle(store, doc, kind, client.fetch_blob(answer["blob_sha"]), answer["blob_sha"])
    return f"{kind} of {doc.sha256[:12]}: the server has a bundle ({answer['backend']}): it replaces the local build"


def reconcile(store: Store, client, lease=None) -> list[str]:
    """After a pull: each local build of a document of the server becomes its
    canonical bundle. A build that the server does not have goes to it; a build
    that is not the canonical bundle gets it. Give one line for each change."""
    from .teamsync import Offline

    lines = []
    rows = store.db.execute(
        "SELECT b.document_id, b.kind, b.tool_version, b.bundle, d.sha256 FROM builds b"
        " JOIN documents d ON d.id = b.document_id WHERE d.version > 0 AND b.kind IN ('index', 'elements')").fetchall()
    for r in rows:
        if r["tool_version"] != kind_format(r["kind"]):
            continue  # a build of an older format: the next build makes the current one
        doc = store.document(r["document_id"])
        best = canonical(store.db, r["sha256"], r["kind"], r["tool_version"])
        backend = store.build(doc.id, r["kind"])["backend"]
        if lease is not None:
            lease.renew()
        try:
            if best is None or (r["bundle"] is None and BACKEND_RANK.get(backend, 9) < BACKEND_RANK[best["backend"]]):
                # No bundle on the server, or the local build is better (docling): it becomes canonical.
                lines.append(publish(store, doc, r["kind"], client))
            elif best["blob_sha"] != r["bundle"]:
                import_bundle(store, doc, r["kind"], client.fetch_blob(best["blob_sha"]), best["blob_sha"])
                lines.append(f"{r['kind']} of {r['sha256'][:12]}: the canonical bundle ({best['backend']}) replaces "
                             "the local build")
        except Offline:
            raise
        except LensError as e:  # one document: the others continue; the next sync tries again
            lines.append(f"error: {r['kind']} bundle of {r['sha256'][:12]}: {e}")
    return lines


# Facts and elements

def _overlap(a: list[float], b: list[float]) -> float:
    """The intersection over union of two regions [x0, y0, x1, y1]."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def relink(db: sqlite3.Connection, doc_id: int, fact_ids: list[int] | None = None) -> int:
    """Link the facts of a document that have an element (or lost it in a relink
    before) to the elements of its current element map: the same element ID when
    its region agrees with the bbox of the fact, else the element of the page that
    covers the bbox best. A fact with only a region stays so. A fact with no such element keeps its page
    and bbox, gets no element ID and the note CHANGED_NOTE. Each store does the
    same with the same map, thus no outbox rows. Give the number of changed facts."""
    if db.execute("SELECT 1 FROM builds WHERE document_id = ? AND kind = 'elements'", (doc_id,)).fetchone() is None:
        return 0
    facts = db.execute("SELECT id, page, element_id, bbox, note FROM knowledge WHERE document_id = ? AND bbox != ''"
                       " AND (element_id != '' OR instr(note, ?))"
                       + (f" AND id IN ({', '.join('?' * len(fact_ids))})" if fact_ids else ""),
                       (doc_id, CHANGED_NOTE, *(fact_ids or []))).fetchall()
    changed = 0
    with not_recorded(db):
        for f in facts:
            try:
                box = [float(v) for v in json.loads(f["bbox"])]
            except (ValueError, TypeError):
                continue
            elements = db.execute("SELECT element_id, x0, y0, x1, y1 FROM elements WHERE document_id = ? AND page = ?",
                                  (doc_id, f["page"])).fetchall()
            scores = {e["element_id"]: _overlap(box, [e["x0"], e["y0"], e["x1"], e["y1"]]) for e in elements}
            best = max(scores.items(), key=lambda kv: (kv[1], kv[0]), default=(None, 0.0))
            if scores.get(f["element_id"], 0.0) >= MATCH or best[1] >= MATCH:
                element = f["element_id"] if scores.get(f["element_id"], 0.0) >= MATCH else best[0]
                # The fact has its element again: no note of a change.
                note = f["note"].replace(f"; {CHANGED_NOTE}", "").replace(CHANGED_NOTE, "")
            else:
                element = ""
                note = f["note"] if CHANGED_NOTE in f["note"] else (
                    f"{f['note']}; {CHANGED_NOTE}" if f["note"] else CHANGED_NOTE)
            if element == f["element_id"] and note == f["note"]:
                continue
            db.execute("UPDATE knowledge SET element_id = ?, note = ? WHERE id = ?", (element, note, f["id"]))
            changed += 1
    return changed
