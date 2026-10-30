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
import itertools
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import makers
from .config import store_home
from .errors import LensError
from .pdflock import PDFIUM
from .fileutil import temp_path

SCHEMA_VERSION = 2  # the manufacturer registry needs no change: see _update_registry
# A build records the version of its format (indexer.INDEX_FORMAT,
# elements.ELEMENTS_FORMAT), not the version of the package. The builds of the
# development versions recorded the package version 0.1.0.dev0: format 1. No
# schema change for this: a server of the old version must still open the store.
LEGACY_FORMATS = {"1": ("0.1.0",)}

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
-- The manufacturer registry (makers.py): the names of each manufacturer, and
-- the pairs that are two manufacturers.
CREATE TABLE IF NOT EXISTS manufacturers (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manufacturer_aliases (
    key             TEXT PRIMARY KEY,  -- makers.key() of the name
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    source          TEXT NOT NULL,     -- builtin or user
    created         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manufacturer_distinct (
    a INTEGER NOT NULL REFERENCES manufacturers(id) ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES manufacturers(id) ON DELETE CASCADE,
    PRIMARY KEY (a, b)
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


def _common_document(db: sqlite3.Connection, a: int, b: int) -> bool:
    """True if the two parts (or their base parts) have a document in common."""
    return db.execute(
        "SELECT 1 FROM part_documents x JOIN part_documents y ON y.document_id = x.document_id"
        " WHERE x.part_id IN (:a, (SELECT base_id FROM parts WHERE id = :a))"
        " AND y.part_id IN (:b, (SELECT base_id FROM parts WHERE id = :b)) LIMIT 1",
        {"a": a, "b": b}).fetchone() is not None


def _merge_part(db: sqlite3.Connection, keep: int, dup: int) -> None:
    """Move the document links, the aliases, the concrete parts and the facts of
    the part dup to the part keep, then delete dup. keep gets the category and
    the base part of dup when it has none. Two levels only: when keep has a
    base part, the concrete parts of dup go to that base part."""
    ids = {"keep": keep, "dup": dup}
    db.execute("UPDATE parts SET family = (SELECT family FROM parts WHERE id = :dup) WHERE id = :keep AND family = ''",
               ids)
    db.execute("UPDATE parts SET base_id = (SELECT base_id FROM parts WHERE id = :dup) WHERE id = :keep"
               " AND base_id IS NULL AND NOT EXISTS (SELECT 1 FROM parts WHERE base_id IN (:keep, :dup))", ids)
    db.execute("INSERT OR IGNORE INTO part_documents (part_id, document_id, role)"
               " SELECT :keep, document_id, role FROM part_documents WHERE part_id = :dup", ids)
    db.execute("INSERT OR IGNORE INTO part_aliases (part_id, alias)"
               " SELECT :keep, alias FROM part_aliases WHERE part_id = :dup", ids)
    db.execute("UPDATE parts SET base_id = coalesce((SELECT base_id FROM parts WHERE id = :keep), :keep)"
               " WHERE base_id = :dup", ids)
    db.execute("UPDATE parts SET base_id = NULL WHERE base_id = id")
    db.execute("UPDATE knowledge SET part_id = :keep WHERE part_id = :dup", ids)
    db.execute("DELETE FROM parts WHERE id = :dup", ids)


def _by_mpn(db: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """The parts, in groups of the same MPN (case is ignored), each by ID."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in db.execute("SELECT id, mpn, manufacturer FROM parts ORDER BY id").fetchall():
        groups.setdefault(row["mpn"].lower(), []).append(row)
    return list(groups.values())


def merge_duplicates(db: sqlite3.Connection) -> list[str]:
    """Merge the parts with the same MPN and the same manufacturer in the
    registry (see makers.same) into the part with the lowest ID. It keeps its
    manufacturer name. Give one line for each merge. No commit."""
    merged = []
    for parts in _by_mpn(db):
        groups: dict[tuple, list[sqlite3.Row]] = {}
        for row in parts:
            groups.setdefault(makers.group(db, row["manufacturer"]), []).append(row)
        for keep, *dups in groups.values():
            for dup in dups:
                _merge_part(db, keep["id"], dup["id"])
                merged.append(f"{keep['mpn']}: {dup['manufacturer']} into {keep['manufacturer']}")
    return merged


def _update_registry(db: sqlite3.Connection) -> None:
    """Put the names of makers.MANUFACTURERS into the registry. Then a base part
    with no manufacturer gets the manufacturer of its concrete parts, and the
    parts with the same MPN and the same manufacturer merge. The registry does
    not learn a name from the parts: two parts with one document can be a
    second source (see alias_candidates). No schema change: an older version
    of datasheet-lens still opens the store."""
    makers.seed(db)
    for base in db.execute("SELECT id, mpn FROM parts WHERE manufacturer = ''"
                           " AND id IN (SELECT base_id FROM parts)").fetchall():
        names = [r[0] for r in db.execute("SELECT DISTINCT manufacturer FROM parts WHERE base_id = ?"
                                          " AND manufacturer != ''", (base["id"],))]
        if (names and len({makers.group(db, n) for n in names}) == 1 and not db.execute(
                "SELECT 1 FROM parts WHERE mpn = ? AND manufacturer = ?", (base["mpn"], names[0])).fetchone()):
            db.execute("UPDATE parts SET manufacturer = ? WHERE id = ?", (names[0], base["id"]))
    merge_duplicates(db)


def alias_candidates(db: sqlite3.Connection) -> list[str]:
    """The pairs of manufacturer names that are probably one manufacturer: two
    parts with the same MPN and these names have a document in common, and the
    registry does not record the two names as one or as two manufacturers. Only
    the user can tell (a second source can share a datasheet). One line for
    each pair, with the MPNs."""
    pairs: dict[tuple[str, str], list[str]] = {}
    for parts in _by_mpn(db):
        for a, b in itertools.combinations(parts, 2):
            x, y = a["manufacturer"], b["manufacturer"]
            if (x and y and not makers.same(db, x, y) and not makers.distinct(db, x, y)
                    and _common_document(db, a["id"], b["id"])):
                pairs.setdefault(tuple(sorted((x, y), key=str.casefold)), []).append(a["mpn"])
    return [f"{x} and {y}: the parts {', '.join(mpns)} have the same documents. If the two names are one "
            f"manufacturer: alias \"{x}\" \"{y}\"; if not: distinct \"{x}\" \"{y}\""
            for (x, y), mpns in pairs.items()]


# Changes of a store of an older version, in sequence: {version: SQL}.
MIGRATIONS = {
    # A concrete orderable part (TLV75533PDBVR) refers to the base part of its
    # datasheet (TLV755P).
    2: "ALTER TABLE parts ADD COLUMN base_id INTEGER REFERENCES parts(id) ON DELETE SET NULL",
}


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
        """Make the schema, or bring a store of an older version up to date. Then
        bring the manufacturer registry up to date (see _update_registry)."""
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(f"store schema version {row['value']} is not supported "
                               f"(this version of datasheet-lens knows {SCHEMA_VERSION}: install a newer one)")
        elif int(row["value"]) < SCHEMA_VERSION:
            self._in_transaction(self._migrate)
        if not makers.seed_current(self.db):
            self._in_transaction(lambda: makers.seed_current(self.db) or _update_registry(self.db))

    def _in_transaction(self, func) -> None:
        """Run func in one write transaction (BEGIN IMMEDIATE): a second process
        that opens the store at the same time waits, then reads the result. func
        must check the state again. An error undoes all changes (SQLite can also
        undo ALTER TABLE)."""
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            func()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        """The changes of MIGRATIONS from the version of the store."""
        version = int(self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
        for target in range(version + 1, SCHEMA_VERSION + 1):
            self.db.execute(MIGRATIONS[target])
            self.db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),))

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

    def build_current(self, doc_id: int, kind: str, fmt: str, backend: str) -> bool:
        """True if the derived data of this kind has this format version (the
        column tool_version) and backend. A build of a development version has
        its format in LEGACY_FORMATS."""
        row = self.build(doc_id, kind)
        if row is None or row["backend"] != backend:
            return False
        stored = row["tool_version"]
        return stored == fmt or stored.startswith(LEGACY_FORMATS.get(fmt, ()))

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
        """Give the ID of the part, and make it if necessary. Case is ignored. The
        same part:
        - a part with this MPN and the same manufacturer in the registry (makers.same)
        - a part with no manufacturer, or a part that has no manufacturer yet, when
          only one part has this MPN
        A different manufacturer that the registry records as distinct is a second
        source: a new part. Else a LensError: the user must record the relation of
        the two names (maker_relation)."""
        manufacturer = manufacturer.strip()
        rows = self.db.execute("SELECT id, manufacturer FROM parts WHERE mpn = ?", (mpn,)).fetchall()
        for r in rows:
            if makers.same(self.db, r["manufacturer"], manufacturer):
                return r["id"]
        if len(rows) == 1 and (not manufacturer or not rows[0]["manufacturer"]):
            if manufacturer:
                self.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ?", (manufacturer, rows[0]["id"]))
                self.db.commit()
            return rows[0]["id"]
        named = [r for r in rows if r["manufacturer"]]
        if manufacturer and named and not all(makers.distinct(self.db, r["manufacturer"], manufacturer)
                                              for r in named):
            raise self._unknown_relation(mpn, manufacturer, named)
        cur = self.db.execute("INSERT INTO parts (mpn, manufacturer, family, created) VALUES (?, ?, ?, ?)",
                              (mpn, manufacturer, family, now()))
        self.db.commit()
        return cur.lastrowid

    @staticmethod
    def _unknown_relation(mpn: str, manufacturer: str, rows: list[sqlite3.Row]) -> LensError:
        """The error for a part with the MPN of rows and a manufacturer name that
        the registry does not relate to their names: it tells how to record it."""
        known = rows[0]["manufacturer"]
        return LensError(
            f"the store has {mpn} from {', '.join(r['manufacturer'] for r in rows)}, and the manufacturer registry "
            f"does not know if {manufacturer} is the same manufacturer. Ask the user, then record it with the CLI "
            f"command \"datasheet-lens maker RELATION NAME OTHER\" or the MCP tool manufacturer_relation: alias (one "
            f"manufacturer, two names: alias \"{manufacturer}\" \"{known}\") or distinct (two "
            f"manufacturers, a second source: distinct \"{manufacturer}\" \"{known}\"). Then do the step again.")

    def maker_relation(self, relation: str, name: str, other: str) -> list[str]:
        """Record a relation of two manufacturer names (makers.RELATIONS). Parts
        with the same MPN that are now of one manufacturer merge. Give one line
        for each merge."""
        try:
            makers.relate(self.db, relation, name, other)
            merged = merge_duplicates(self.db) if relation != "distinct" else []
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return merged

    def same_maker(self, a: str, b: str) -> bool:
        """True if the two names are the same manufacturer (makers.same)."""
        return makers.same(self.db, a, b)

    def known_maker(self, mpn: str, proposed: str) -> str:
        """The manufacturer of the part with this MPN (when the store has one
        part with a manufacturer, and the registry does not record the two names
        as two manufacturers), else proposed. A proposal from a PDF is only a
        guess: a different name of the same manufacturer must not stop a link."""
        rows = [r for r in self.match_parts(mpn) if r["manufacturer"]]
        if len(rows) == 1 and not (proposed and makers.distinct(self.db, rows[0]["manufacturer"], proposed)):
            return rows[0]["manufacturer"]
        return proposed

    def maker_listing(self) -> list[str]:
        """The manufacturer registry (makers.listing), then the pairs of names
        that are probably one manufacturer (alias_candidates)."""
        candidates = alias_candidates(self.db)
        return makers.listing(self.db) + (["", "To check (the user decides):"] + candidates if candidates else [])

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
            same = [r for r in rows if makers.same(self.db, r["manufacturer"], manufacturer)]
            rows = same or [r for r in rows if not r["manufacturer"]]
        return rows

    def find_part(self, mpn: str, manufacturer: str = "", create: bool = False) -> int:
        """The ID of the part (see match_parts). If no part matches and create is
        true, make the part (see add_part)."""
        rows = self.match_parts(mpn, manufacturer)
        if not rows and create:
            return self.add_part(mpn, manufacturer)
        if not rows:
            raise LensError(f"part not found: {mpn}" + (f" ({manufacturer})" if manufacturer else ""))
        if len(rows) > 1:
            names = ", ".join(f"{r['mpn']} ({r['manufacturer'] or 'manufacturer unknown'})" for r in rows)
            raise LensError(f"part {mpn} is not unique: {names}; give the manufacturer")
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
        # A concrete part with no manufacturer gets the one of its base part, and
        # a base part with no manufacturer the one of the concrete part (not when
        # a part with this MPN and manufacturer is there: the pair is unique).
        with contextlib.suppress(sqlite3.IntegrityError):
            self.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ? AND manufacturer = ''",
                            (base["manufacturer"], part_id))
        with contextlib.suppress(sqlite3.IntegrityError):
            self.db.execute("UPDATE parts SET manufacturer = (SELECT manufacturer FROM parts WHERE id = ?)"
                            " WHERE id = ? AND manufacturer = ''", (part_id, base_id))
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
