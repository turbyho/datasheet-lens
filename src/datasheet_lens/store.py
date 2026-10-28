"""Central store: SQLite database and content-addressed file store.

Layout of the store directory (default ~/.datasheet-lens):
  db/lens.db                    SQLite database (WAL mode)
  blobs/<sha[:2]>/<sha>.<ext>   the files (PDF and other documents)

Large files are never in the database. The database keeps the hash, the size, the
type and the path of each file.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import store_home
from .errors import LensError
from .fileutil import temp_path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parts (
    id           INTEGER PRIMARY KEY,
    mpn          TEXT NOT NULL COLLATE NOCASE,
    manufacturer TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    family       TEXT NOT NULL DEFAULT '',
    created      TEXT NOT NULL,
    UNIQUE (mpn, manufacturer)
);
CREATE TABLE IF NOT EXISTS part_aliases (
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    alias   TEXT NOT NULL,
    PRIMARY KEY (part_id, alias)
);
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    sha256       TEXT NOT NULL UNIQUE,
    filename     TEXT NOT NULL,
    media_type   TEXT NOT NULL,
    size         INTEGER NOT NULL,
    pages        INTEGER,
    title        TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    -- Identity of the file: sha256. Identity of the document across revisions:
    -- doc_number (for example "SBOS058") with revision ("B") and revision_date.
    doc_number    TEXT NOT NULL DEFAULT '',
    revision      TEXT NOT NULL DEFAULT '',
    revision_date TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    source_url   TEXT NOT NULL DEFAULT '',
    added        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS part_documents (
    part_id     INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'datasheet',
    PRIMARY KEY (part_id, document_id)
);
-- One row for each derived data kind of a document ("index", "elements"), with the
-- tool version and the backend. A different version means: build again.
CREATE TABLE IF NOT EXISTS builds (
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    backend      TEXT NOT NULL,
    built        TEXT NOT NULL,
    PRIMARY KEY (document_id, kind)
);
CREATE TABLE IF NOT EXISTS pages (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page        INTEGER NOT NULL,
    width_pt    REAL NOT NULL,
    height_pt   REAL NOT NULL,
    chars       INTEGER NOT NULL,
    text        TEXT NOT NULL,
    quality     TEXT NOT NULL DEFAULT '{}',
    -- SHA-256 of the normalized page text (no white space, no header and footer
    -- lines). Equal values find the same page in a changed copy of a document (a
    -- distributor adds a cover page or a stamp).
    text_sha    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (document_id, page)
);
CREATE INDEX IF NOT EXISTS pages_text_sha ON pages (text_sha);
CREATE TABLE IF NOT EXISTS outline (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    level       INTEGER NOT NULL,
    title       TEXT NOT NULL,
    page        INTEGER NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (document_id, seq)
);
CREATE TABLE IF NOT EXISTS captions (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    caption     TEXT NOT NULL,
    page        INTEGER NOT NULL,
    x_pt        REAL,
    y_pt        REAL,
    page_order  INTEGER,
    page_count  INTEGER,
    PRIMARY KEY (document_id, seq)
);
CREATE TABLE IF NOT EXISTS elements (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    element_id  TEXT NOT NULL,
    page        INTEGER NOT NULL,
    type        TEXT NOT NULL,
    code        TEXT NOT NULL,
    page_order  INTEGER NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    text        TEXT NOT NULL,
    PRIMARY KEY (document_id, element_id)
);
CREATE INDEX IF NOT EXISTS elements_page ON elements (document_id, page);
-- A value that a model or a person read from a document, with its source.
CREATE TABLE IF NOT EXISTS knowledge (
    id            INTEGER PRIMARY KEY,
    part_id       INTEGER REFERENCES parts(id) ON DELETE SET NULL,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page          INTEGER NOT NULL,
    element_id    TEXT NOT NULL DEFAULT '',
    bbox          TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL,
    value         TEXT NOT NULL,
    unit          TEXT NOT NULL DEFAULT '',
    conditions    TEXT NOT NULL DEFAULT '',
    column_name   TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    source_kind   TEXT NOT NULL CHECK (source_kind IN ('model', 'person')),
    model         TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    created       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'unchecked'
                  CHECK (status IN ('unchecked', 'checked', 'disputed'))
);
CREATE INDEX IF NOT EXISTS knowledge_subject ON knowledge (subject);
"""

MEDIA_TYPES = {".pdf": "application/pdf"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Document:
    id: int
    sha256: str
    filename: str
    media_type: str
    size: int
    pages: int | None
    title: str
    path: Path


class Store:
    """The local central store. One instance for each process."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else store_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "blobs").mkdir(exist_ok=True)
        (self.home / "db").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home / "db" / "lens.db")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(f"store schema version {row['value']} is not supported "
                               f"(this version of datasheet-lens needs {SCHEMA_VERSION})")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def build_current(self, doc_id: int, kind: str, tool_version: str, backend: str) -> bool:
        """True if the derived data of this kind was built with this tool version
        and backend."""
        row = self.db.execute("SELECT tool_version, backend FROM builds WHERE document_id = ? AND kind = ?",
                              (doc_id, kind)).fetchone()
        return row is not None and row["tool_version"] == tool_version and row["backend"] == backend

    # Files

    def blob_path(self, sha: str, suffix: str) -> Path:
        return self.home / "blobs" / sha[:2] / f"{sha}{suffix}"

    # Documents

    def add_document(self, path: Path, title: str = "", source_url: str = "") -> Document:
        """Copy a file into the store and record it. A file that is already in the
        store (same SHA-256) is not copied again."""
        path = Path(path)
        suffix = path.suffix.lower()
        media_type = MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise LensError(f"file type {suffix or '(none)'} is not supported: {path}")
        sha = sha256_file(path)
        existing = self.document(sha)
        if existing:
            return existing
        pages = None
        if media_type == "application/pdf":
            # Open the source first: a PDF that pdfium cannot read (encrypted,
            # damaged) must not leave a file in the store.
            import pypdfium2 as pdfium

            try:
                pdf = pdfium.PdfDocument(str(path))
            except pdfium.PdfiumError as e:
                raise LensError(f"cannot read the PDF (encrypted or damaged?): {path}: {e}") from e
            pages = len(pdf)
            if not title:
                title = (pdf.get_metadata_dict().get("Title") or "").strip()
            pdf.close()
        target = self.blob_path(sha, suffix)
        tmp = temp_path(target)
        try:
            shutil.copyfile(path, tmp)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        self.db.execute(
            "INSERT INTO documents (sha256, filename, media_type, size, pages, title, source_url, added)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sha, path.name, media_type, target.stat().st_size, pages, title, source_url, now()),
        )
        self.db.commit()
        return self.document(sha)

    def document(self, ref: str | int) -> Document | None:
        """Find a document by ID, by SHA-256 or by a unique SHA-256 prefix (8 or more
        characters). A string of digits with fewer than 8 characters is an ID; a
        longer one is a prefix (a SHA-256 prefix can have only digits)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit() and len(ref) < 8):
            row = self.db.execute("SELECT * FROM documents WHERE id = ?", (int(ref),)).fetchone()
        else:
            rows = self.db.execute("SELECT * FROM documents WHERE sha256 LIKE ?",
                                   (ref.lower() + "%",)).fetchall() if len(ref) >= 8 else []
            if len(rows) > 1:
                raise LensError(f"document prefix {ref} is not unique")
            row = rows[0] if rows else None
        if row is None:
            return None
        suffix = next((s for s, t in MEDIA_TYPES.items() if t == row["media_type"]), "")
        return Document(row["id"], row["sha256"], row["filename"], row["media_type"], row["size"],
                        row["pages"], row["title"], self.blob_path(row["sha256"], suffix))

    def documents(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT d.*, group_concat(p.mpn || ' (' || pd.role || ')', ', ') AS parts"
            " FROM documents d LEFT JOIN part_documents pd ON pd.document_id = d.id"
            " LEFT JOIN parts p ON p.id = pd.part_id GROUP BY d.id ORDER BY d.id").fetchall()

    # Parts

    def add_part(self, mpn: str, manufacturer: str = "", family: str = "") -> int:
        """Give the ID of the part, and make it if necessary. Case is ignored. A part
        with no manufacturer, or a part that has no manufacturer yet, is the same
        part when only one part has this MPN."""
        rows = self.db.execute("SELECT id, manufacturer FROM parts WHERE mpn = ?", (mpn,)).fetchall()
        for r in rows:
            if r["manufacturer"].lower() == manufacturer.lower():
                return r["id"]
        if len(rows) == 1 and (not manufacturer or not rows[0]["manufacturer"]):
            if manufacturer:
                self.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ?", (manufacturer, rows[0]["id"]))
                self.db.commit()
            return rows[0]["id"]
        cur = self.db.execute("INSERT INTO parts (mpn, manufacturer, family, created) VALUES (?, ?, ?, ?)",
                              (mpn, manufacturer, family, now()))
        self.db.commit()
        return cur.lastrowid

    def link(self, part_id: int, document_id: int, role: str = "datasheet") -> None:
        self.db.execute(
            "INSERT INTO part_documents (part_id, document_id, role) VALUES (?, ?, ?)"
            " ON CONFLICT (part_id, document_id) DO UPDATE SET role = excluded.role",
            (part_id, document_id, role))
        self.db.commit()

    def parts(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.*, count(pd.document_id) AS documents FROM parts p"
            " LEFT JOIN part_documents pd ON pd.part_id = p.id GROUP BY p.id ORDER BY p.mpn").fetchall()

    def part_documents(self, mpn: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT d.*, pd.role, p.mpn, p.manufacturer FROM parts p"
            " JOIN part_documents pd ON pd.part_id = p.id JOIN documents d ON d.id = pd.document_id"
            " WHERE p.mpn = ? COLLATE NOCASE OR p.id IN"
            " (SELECT part_id FROM part_aliases WHERE alias = ? COLLATE NOCASE) ORDER BY d.id",
            (mpn, mpn)).fetchall()
