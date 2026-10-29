"""Central store: SQLite database and content-addressed file store.

Layout of the store directory (default ~/.datasheet-lens):
  db/lens.db                    SQLite database (WAL mode)
  blobs/<sha[:2]>/<sha>.<ext>   the files (PDF and other documents)

Large files are never in the database. The database keeps the hash, the size, the
type and the path of each file.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import store_home
from .errors import LensError
from .pdflock import PDFIUM
from .fileutil import temp_path

SCHEMA_VERSION = 2
# Changes of a store of an older version, in sequence: {version: SQL}.
MIGRATIONS = {
    # A concrete orderable part (TLV75533PDBVR) refers to the base part of its
    # datasheet (TLV755P).
    2: "ALTER TABLE parts ADD COLUMN base_id INTEGER REFERENCES parts(id) ON DELETE SET NULL",
}

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
    base_id      INTEGER REFERENCES parts(id) ON DELETE SET NULL,
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
ROLES = ("datasheet", "manual", "errata", "appnote", "other")


def _ulower(text):
    """lower() for all of Unicode (the lower() of SQLite changes only ASCII)."""
    return text.lower() if isinstance(text, str) else text


def now() -> str:
    """The time now in UTC (ISO 8601, seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """The SHA-256 of a file (hex)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Document:
    """A document in the store: ID, SHA-256, file name, media type, size, pages, title
    and the path of its file."""
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
        self.db.create_function("ulower", 1, _ulower, deterministic=True)
        try:
            self._prepare()
        except BaseException:
            self.db.close()  # no open connection is left for an error of the schema
            raise

    def _prepare(self) -> None:
        """Make the schema, or bring a store of an older version up to date."""
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
            return
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"store schema version {version} is not supported "
                               f"(this version of datasheet-lens knows {SCHEMA_VERSION}: install a newer one)")
        if version == SCHEMA_VERSION:
            return
        # A store of an older version. One write transaction (BEGIN IMMEDIATE) for
        # all changes: a second process that opens the store at the same time
        # waits, then reads the new version and changes nothing. SQLite can undo
        # ALTER TABLE in a transaction.
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            version = int(self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
            for target in range(version + 1, SCHEMA_VERSION + 1):
                self.db.execute(MIGRATIONS[target])
                self.db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def build(self, doc_id: int, kind: str) -> sqlite3.Row | None:
        """The last build of the derived data of this kind, or None."""
        return self.db.execute("SELECT * FROM builds WHERE document_id = ? AND kind = ?",
                               (doc_id, kind)).fetchone()

    def build_current(self, doc_id: int, kind: str, tool_version: str, backend: str) -> bool:
        """True if the derived data of this kind was built with this tool version
        and backend."""
        row = self.build(doc_id, kind)
        return row is not None and row["tool_version"] == tool_version and row["backend"] == backend

    # Files

    def blob_path(self, sha: str, suffix: str) -> Path:
        """The path of the file of a document in the store (from its SHA-256)."""
        return self.home / "blobs" / sha[:2] / f"{sha}{suffix}"

    # Documents

    def add_document(self, path: Path, title: str = "", source_url: str = "", move: bool = False) -> Document:
        """Copy a file into the store and record it. A file that is already in the
        store (same SHA-256) is not copied again; it gets the source URL if it has
        none. move: move the file (a temporary file on the disk of the store)."""
        path = Path(path)
        suffix = path.suffix.lower()
        media_type = MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise LensError(f"file type {suffix or '(none)'} is not supported: {path}")
        sha = sha256_file(path)
        existing = self.document(sha)
        if existing:
            if source_url:
                self.db.execute("UPDATE documents SET source_url = ? WHERE id = ? AND source_url = ''",
                                (source_url, existing.id))
                self.db.commit()
            return existing
        pages = None
        if media_type == "application/pdf":
            # Open the source first: a PDF that pdfium cannot read (encrypted,
            # damaged) must not leave a file in the store.
            import pypdfium2 as pdfium

            # The lock only for pdfium: not for the hash, the copy and the database.
            with PDFIUM:
                try:
                    pdf = pdfium.PdfDocument(str(path))
                except pdfium.PdfiumError as e:
                    raise LensError(f"cannot read the PDF (encrypted or damaged?): {path}: {e}") from e
                try:
                    pages = len(pdf)
                    if not title:
                        title = (pdf.get_metadata_dict().get("Title") or "").strip()
                finally:
                    pdf.close()
        target = self.blob_path(sha, suffix)
        tmp = temp_path(target)
        try:
            if move:
                path.replace(tmp)
            else:
                shutil.copyfile(path, tmp)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        try:
            self.db.execute(
                "INSERT INTO documents (sha256, filename, media_type, size, pages, title, source_url, added)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, path.name, media_type, target.stat().st_size, pages, title, source_url, now()),
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            # A different process added the same file after the check above.
            self.db.rollback()
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
        """All documents, with the parts that they are linked to."""
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
        """Link a document to a part with a role. A second link changes the role."""
        self.db.execute(
            "INSERT INTO part_documents (part_id, document_id, role) VALUES (?, ?, ?)"
            " ON CONFLICT (part_id, document_id) DO UPDATE SET role = excluded.role",
            (part_id, document_id, role))
        self.db.commit()

    def parts(self) -> list[sqlite3.Row]:
        """All parts, with the number of their documents."""
        return self.db.execute(
            "SELECT p.*, count(pd.document_id) AS documents FROM parts p"
            " LEFT JOIN part_documents pd ON pd.part_id = p.id GROUP BY p.id ORDER BY p.mpn").fetchall()

    def match_parts(self, mpn: str, manufacturer: str = "") -> list[sqlite3.Row]:
        """The parts with this MPN or alias (case is ignored). With a manufacturer:
        the parts of this manufacturer, or else the parts with no manufacturer."""
        rows = self.db.execute(
            "SELECT id, mpn, manufacturer FROM parts WHERE mpn = ? COLLATE NOCASE OR id IN"
            " (SELECT part_id FROM part_aliases WHERE alias = ? COLLATE NOCASE)", (mpn, mpn)).fetchall()
        if manufacturer:
            same = [r for r in rows if r["manufacturer"].lower() == manufacturer.lower()]
            rows = same or [r for r in rows if not r["manufacturer"]]
        return rows

    def find_part(self, mpn: str, manufacturer: str = "", create: bool = False) -> int:
        """The ID of the part (see match_parts). If no part matches and create is
        true, make the part."""
        rows = self.match_parts(mpn, manufacturer)
        if not rows and create:
            return self.add_part(mpn, manufacturer)
        if not rows:
            raise LensError(f"part not found: {mpn}" + (f" ({manufacturer})" if manufacturer else ""))
        if len(rows) > 1:
            names = ", ".join(f"{r['mpn']} ({r['manufacturer'] or 'manufacturer unknown'})" for r in rows)
            raise LensError(f"part {mpn} is not unique: {names}")
        return rows[0]["id"]

    def link_parts(self, doc_id: int, mpn: str, variants: list[str], manufacturer: str = "",
                   role: str = "datasheet", category: str = "") -> tuple[int | None, list[str]]:
        """Link a document to the part mpn (an MPN or an alias), and then to the
        other variants that it covers. Give the ID of the part mpn (None if it
        cannot be linked: then no variant is linked) and the failure messages."""
        names = [mpn.strip()] + [v.strip() for v in variants if v.strip() and v.strip().lower() != mpn.strip().lower()]
        failures: list[str] = []
        first = None
        for name in names:
            try:
                part_id = self.find_part(name, manufacturer, create=True)
            except LensError as e:  # for example an MPN of two manufacturers
                failures.append(f"part {name}: not linked: {e}")
                if first is None:
                    return None, failures
                continue
            if category:
                self.set_category(part_id, category)
            self.link(part_id, doc_id, role)
            first = part_id if first is None else first
        return first, failures

    def variants(self, part_id: int) -> list[str]:
        """The MPNs of the parts that share a datasheet with the part (the part
        too), when there are more than one: the variants of one datasheet."""
        rows = self.db.execute(
            "SELECT DISTINCT p.mpn FROM part_documents a JOIN part_documents b ON b.document_id = a.document_id"
            " JOIN parts p ON p.id = b.part_id WHERE a.part_id = ? AND a.role = 'datasheet'"
            " AND b.role = 'datasheet' ORDER BY p.mpn", (part_id,)).fetchall()
        return [r[0] for r in rows] if len(rows) > 1 else []

    def set_category(self, part_id: int, category: str) -> None:
        """The function of the part (for example ADC). The column is "family"."""
        self.db.execute("UPDATE parts SET family = ? WHERE id = ?", (category, part_id))
        self.db.commit()

    def part(self, part_id: int) -> sqlite3.Row | None:
        """The part with this ID, or None."""
        return self.db.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()

    def fill_manufacturer(self, part_id: int, manufacturer: str) -> None:
        """Give a part with no manufacturer this manufacturer (a part with one keeps it)."""
        self.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ? AND manufacturer = ''",
                        (manufacturer, part_id))
        self.db.commit()

    def set_base(self, part_id: int, base_id: int) -> None:
        """A concrete orderable part (TLV75533PDBVR) refers to the base part of its
        datasheet (TLV755P). It then has the documents of the base part too."""
        base = self.part(base_id)
        if base is None:
            raise LensError(f"part not found: {base_id}")
        if base_id == part_id:
            raise LensError("a part cannot be its own base part")
        # Two levels only: documents_of_part follows one step.
        if base["base_id"] is not None:
            raise LensError(f"the base part {base['mpn']} is a concrete part (it has a base part)")
        if self.db.execute("SELECT 1 FROM parts WHERE base_id = ? LIMIT 1", (part_id,)).fetchone():
            raise LensError(f"{self.part(part_id)['mpn']} is the base part of other parts: it cannot get a base part")
        self.db.execute("UPDATE parts SET base_id = ? WHERE id = ?", (base_id, part_id))
        # A concrete part with no manufacturer gets the one of its base part (not
        # when a part with this MPN and manufacturer is there: the pair is unique).
        with contextlib.suppress(sqlite3.IntegrityError):
            self.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ? AND manufacturer = ''",
                            (base["manufacturer"], part_id))
        self.db.commit()

    def base_of(self, part_id: int) -> sqlite3.Row | None:
        """The base part of a concrete part, or None."""
        return self.db.execute("SELECT b.* FROM parts p JOIN parts b ON b.id = p.base_id WHERE p.id = ?",
                               (part_id,)).fetchone()

    def documents_of_part(self, part_id: int) -> list[sqlite3.Row]:
        """The documents of the part, and of its base part. The role of the link
        of the part wins over the role of the link of the base part."""
        return self.db.execute(
            "SELECT d.*, coalesce((SELECT role FROM part_documents WHERE part_id = :p AND document_id = d.id),"
            " (SELECT role FROM part_documents WHERE part_id = (SELECT base_id FROM parts WHERE id = :p)"
            " AND document_id = d.id)) AS role FROM documents d WHERE d.id IN (SELECT document_id FROM"
            " part_documents WHERE part_id = :p OR part_id = (SELECT base_id FROM parts WHERE id = :p))"
            " ORDER BY d.id", {"p": part_id}).fetchall()

    def parts_of_document(self, doc_id: int) -> list[sqlite3.Row]:
        """The parts that a document is linked to, with the role of each link."""
        return self.db.execute(
            "SELECT p.*, pd.role FROM part_documents pd JOIN parts p ON p.id = pd.part_id"
            " WHERE pd.document_id = ? ORDER BY p.id", (doc_id,)).fetchall()

    def part_names(self) -> list[tuple[int, str]]:
        """(part ID, name) for each MPN and each alias."""
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT id, mpn FROM parts UNION SELECT part_id, alias FROM part_aliases").fetchall()]

    def part_documents(self, mpn: str) -> list[sqlite3.Row]:
        """The documents of the parts with this MPN or alias, and of their base
        parts (a concrete orderable MPN finds the datasheet of its base part)."""
        return self.db.execute(
            "WITH found AS (SELECT id, base_id FROM parts WHERE mpn = ? COLLATE NOCASE OR id IN"
            " (SELECT part_id FROM part_aliases WHERE alias = ? COLLATE NOCASE)),"
            " ids AS (SELECT id FROM found UNION SELECT base_id FROM found WHERE base_id IS NOT NULL)"
            " SELECT d.*, pd.role, p.mpn, p.manufacturer FROM parts p"
            " JOIN part_documents pd ON pd.part_id = p.id JOIN documents d ON d.id = pd.document_id"
            " WHERE p.id IN (SELECT id FROM ids) ORDER BY d.id",
            (mpn, mpn)).fetchall()

    def prefix_parts(self, mpn: str, least: int = 4) -> list[str]:
        """The MPNs and aliases (least characters or more) that start mpn, the
        longest first: probably its base part (TMP112D of TMP112D0IDPWR)."""
        rows = self.db.execute(
            "SELECT mpn AS name FROM parts UNION SELECT alias FROM part_aliases", ()).fetchall()
        key = mpn.upper()
        return sorted({r["name"] for r in rows if len(r["name"]) >= least and r["name"].upper() != key
                       and key.startswith(r["name"].upper())}, key=len, reverse=True)
