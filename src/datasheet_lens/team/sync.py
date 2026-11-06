"""The synchronization of the shared tables on the team server: pull and push.

The server has one table for each shared table of the local store, with global
keys (gid, sha256) in place of the local IDs. A trigger sets the transaction
(xid), the version and the time of each change. A delete is a tombstone. The
change feed gives the rows with cursor <= xid < xmin (see plans of the team
server: a cursor on a sequence number loses changes).

A pull gives a row after the rows that it refers to, with one exception:
documents.supersedes can refer to a document that comes later. The client sets
that reference at the end of the pull.

The rules of a push are in push(). The registry of manufacturers on the server
comes from the built-in names and the shared relations; a merge of parts keeps
the older part (created, then gid), as store.merge_duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import makers
from ..bundles import BUNDLE_BACKENDS, canonical_key
from ..store import _VERSION_TABLES as VERSIONED

# The tables in the order of a pull: a row comes after the rows that it refers to.
# A new table comes at the end: a page position of a pull has the index of its table.
TABLES = ("documents", "bundles", "maker_relations", "parts", "part_documents", "part_aliases", "knowledge",
          "part_sources")
# The derived data of a document (index, element map) as a bundle file. A client
# adds a bundle with POST /v1/bundles (register_bundle), not with a push. For a
# document, a kind and a format, the canonical bundle comes first in
# bundles.canonical_key: the same order on the server and the clients.
BUNDLE_KINDS = tuple(BUNDLE_BACKENDS)
PAGE = 1000
MAX_PUSH = 500

_COMMON = """
    version    BIGINT NOT NULL DEFAULT 1,
    xid        xid8 NOT NULL DEFAULT pg_current_xact_id(),
    deleted    BOOLEAN NOT NULL DEFAULT false,
    changed    TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by INTEGER REFERENCES accounts(id) ON DELETE SET NULL"""

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS documents (
        sha256        TEXT PRIMARY KEY REFERENCES blobs(sha256),
        filename      TEXT NOT NULL,
        media_type    TEXT NOT NULL,
        size          BIGINT NOT NULL,
        pages         INTEGER,
        title         TEXT NOT NULL DEFAULT '',
        manufacturer  TEXT NOT NULL DEFAULT '',
        doc_number    TEXT NOT NULL DEFAULT '',
        revision      TEXT NOT NULL DEFAULT '',
        revision_date TEXT NOT NULL DEFAULT '',
        supersedes    TEXT REFERENCES documents(sha256),
        source_url    TEXT NOT NULL DEFAULT '',
        added         TEXT NOT NULL,{_COMMON})""",
    f"""CREATE TABLE IF NOT EXISTS bundles (
        document_sha TEXT NOT NULL REFERENCES documents(sha256),
        kind         TEXT NOT NULL CHECK (kind IN ('index', 'elements')),
        format       TEXT NOT NULL,
        backend      TEXT NOT NULL CHECK (backend IN ('pdfium', 'text', 'docling')),
        blob_sha     TEXT NOT NULL REFERENCES blobs(sha256),
        versions     TEXT NOT NULL DEFAULT '{{}}',
        created      TEXT NOT NULL,{_COMMON},
        PRIMARY KEY (document_sha, kind, format, backend))""",
    f"""CREATE TABLE IF NOT EXISTS maker_relations (
        gid      TEXT PRIMARY KEY,
        relation TEXT NOT NULL CHECK (relation IN ('alias', 'distinct')),
        name     TEXT NOT NULL,
        other    TEXT NOT NULL,
        created  TEXT NOT NULL,{_COMMON})""",
    f"""CREATE TABLE IF NOT EXISTS parts (
        gid          TEXT PRIMARY KEY,
        mpn          TEXT NOT NULL,
        manufacturer TEXT NOT NULL DEFAULT '',
        family       TEXT NOT NULL DEFAULT '',
        created      TEXT NOT NULL,
        base_gid     TEXT REFERENCES parts(gid),
        merged_into  TEXT REFERENCES parts(gid),{_COMMON})""",
    "CREATE INDEX IF NOT EXISTS parts_mpn ON parts (lower(mpn)) WHERE NOT deleted",
    f"""CREATE TABLE IF NOT EXISTS part_documents (
        part_gid     TEXT NOT NULL REFERENCES parts(gid),
        document_sha TEXT NOT NULL REFERENCES documents(sha256),
        role         TEXT NOT NULL DEFAULT 'datasheet',{_COMMON},
        PRIMARY KEY (part_gid, document_sha))""",
    f"""CREATE TABLE IF NOT EXISTS part_aliases (
        part_gid TEXT NOT NULL REFERENCES parts(gid),
        alias    TEXT NOT NULL,{_COMMON},
        PRIMARY KEY (part_gid, alias))""",
    f"""CREATE TABLE IF NOT EXISTS part_sources (
        part_gid TEXT NOT NULL REFERENCES parts(gid),
        source   TEXT NOT NULL,
        number   TEXT NOT NULL,
        url      TEXT NOT NULL DEFAULT '',
        library  TEXT NOT NULL DEFAULT '',
        found    TEXT NOT NULL,{_COMMON},
        PRIMARY KEY (part_gid, source, number))""",
    f"""CREATE TABLE IF NOT EXISTS knowledge (
        gid            TEXT PRIMARY KEY,
        part_gid       TEXT REFERENCES parts(gid),
        document_sha   TEXT NOT NULL REFERENCES documents(sha256),
        page           INTEGER NOT NULL,
        element_id     TEXT NOT NULL DEFAULT '',
        bbox           TEXT NOT NULL DEFAULT '',
        subject        TEXT NOT NULL,
        value          TEXT NOT NULL,
        unit           TEXT NOT NULL DEFAULT '',
        conditions     TEXT NOT NULL DEFAULT '',
        column_name    TEXT NOT NULL DEFAULT '',
        note           TEXT NOT NULL DEFAULT '',
        source_kind    TEXT NOT NULL CHECK (source_kind IN ('model', 'person')),
        model          TEXT NOT NULL DEFAULT '',
        prompt_version TEXT NOT NULL DEFAULT '',
        author         TEXT NOT NULL DEFAULT '',
        created        TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'unchecked' CHECK (status IN ('unchecked', 'checked', 'disputed')),{_COMMON})""",
    # The gid of a merged part -> the gid of the kept part (pushed rows can refer to an old gid).
    "CREATE TABLE IF NOT EXISTS gid_map (old_gid TEXT PRIMARY KEY, new_gid TEXT NOT NULL REFERENCES parts(gid))",
    """CREATE TABLE IF NOT EXISTS audit (
        id         BIGSERIAL PRIMARY KEY,
        at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        tbl        TEXT NOT NULL,
        key        TEXT NOT NULL,
        op         TEXT NOT NULL,
        old        JSONB,
        new        JSONB)""",
    """CREATE OR REPLACE FUNCTION dslens_touch() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        NEW.xid := pg_current_xact_id();
        NEW.changed := now();
        IF TG_OP = 'UPDATE' THEN
            NEW.version := OLD.version + 1;
        END IF;
        RETURN NEW;
    END $$""",
] + [statement for table in TABLES for statement in (
    f"CREATE OR REPLACE TRIGGER {table}_touch BEFORE INSERT OR UPDATE ON {table}"
    f" FOR EACH ROW EXECUTE FUNCTION dslens_touch()",
    f"CREATE INDEX IF NOT EXISTS {table}_xid ON {table} (xid)")]

# For each table: the key columns, the value columns (the JSON of a row, as the
# outbox of the client: see store._ROW_JSON) with their column on the server,
# and the order of a page.
KEY = {
    "documents": ("sha256",), "bundles": ("document_sha", "kind", "format", "backend"),
    "maker_relations": ("gid",), "parts": ("gid",),
    "part_documents": ("part_gid", "document_sha"), "part_aliases": ("part_gid", "alias"), "knowledge": ("gid",),
    "part_sources": ("part_gid", "source", "number"),
}
VALUES = {
    "documents": {c: c for c in ("filename", "media_type", "size", "pages", "title", "manufacturer", "doc_number",
                                 "revision", "revision_date", "source_url", "added")} | {"supersedes": "supersedes"},
    "bundles": {c: c for c in ("blob_sha", "versions", "created")},
    "maker_relations": {c: c for c in ("relation", "name", "other", "created")},
    "parts": {"mpn": "mpn", "manufacturer": "manufacturer", "family": "family", "created": "created",
              "base": "base_gid"},
    "part_documents": {"role": "role"},
    "part_aliases": {},
    "knowledge": {c: c for c in ("page", "element_id", "bbox", "subject", "value", "unit", "conditions",
                                 "column_name", "note", "source_kind", "model", "prompt_version", "author",
                                 "created", "status")} | {"part": "part_gid", "document": "document_sha"},
    "part_sources": {c: c for c in ("url", "library", "found")},
}
ORDER = {table: list(KEY[table]) for table in TABLES}
ORDER["parts"] = ["(base_gid IS NOT NULL)", "gid"]  # the base parts first (two levels only)
# The references of a row to parts (translated through gid_map) and to documents.
PART_REFS = {"parts": ("base_gid",), "part_documents": ("part_gid",), "part_aliases": ("part_gid",),
             "knowledge": ("part_gid",), "part_sources": ("part_gid",)}
# The type of each value of the JSON of a row (a tuple: also None), and the values
# with a fixed set. A change with a wrong value is rejected, before the database.
_TEXT = str
TYPES = {
    "documents": {"filename": _TEXT, "media_type": _TEXT, "size": int, "pages": (int, type(None)), "title": _TEXT,
                  "manufacturer": _TEXT, "doc_number": _TEXT, "revision": _TEXT, "revision_date": _TEXT,
                  "source_url": _TEXT, "added": _TEXT, "supersedes": (_TEXT, type(None))},
    "maker_relations": {"relation": _TEXT, "name": _TEXT, "other": _TEXT, "created": _TEXT},
    "parts": {"mpn": _TEXT, "manufacturer": _TEXT, "family": _TEXT, "created": _TEXT, "base": (_TEXT, type(None))},
    "part_documents": {"role": _TEXT},
    "part_aliases": {},
    "knowledge": {"page": int, "element_id": _TEXT, "bbox": _TEXT, "subject": _TEXT, "value": _TEXT, "unit": _TEXT,
                  "conditions": _TEXT, "column_name": _TEXT, "note": _TEXT, "source_kind": _TEXT, "model": _TEXT,
                  "prompt_version": _TEXT, "author": _TEXT, "created": _TEXT, "status": _TEXT,
                  "part": (_TEXT, type(None)), "document": _TEXT},
    "part_sources": {"url": _TEXT, "library": _TEXT, "found": _TEXT},
}
CHOICES = {"relation": ("alias", "distinct"), "source_kind": ("model", "person"),
           "status": ("unchecked", "checked", "disputed"), "role": ("datasheet", "manual", "errata", "appnote", "other"),
           "library": ("", "Basic", "Preferred", "Extended")}
# The tables with a version in the local store (VERSIONED): an update of a different
# table has no base version (the last change is kept).
ROLE_DELETE = {"documents": "admin", "bundles": "admin", "parts": "admin", "knowledge": "admin",
               "maker_relations": "admin",
               "part_documents": "contributor", "part_aliases": "contributor", "part_sources": "contributor"}
# The links of a part: a delete is a tombstone, and an insert of the same key makes
# the row live again.
LINKS = ("part_documents", "part_aliases", "part_sources")


class Rejected(Exception):
    """A pushed change that the server does not apply (the reason goes to the client).
    retry: the change refers to a row that the server does not have yet; the client
    sends it again later. Else the client removes it."""

    def __init__(self, reason: str, retry: bool = False):
        super().__init__(reason)
        self.retry = retry


class Conflict(Exception):
    def __init__(self, row: dict):
        self.row = row


# Registry of manufacturers

class Registry:
    """The manufacturer registry of the server: the built-in names (makers.seed)
    and the shared relations, in the order of created and gid. An SQLite
    database in memory with the tables of the local store: the same code
    (makers) as the clients."""

    def __init__(self):
        from ..store import SCHEMA

        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        makers.seed(self.db)

    @classmethod
    async def load(cls, conn) -> "Registry":
        """The registry of the relations that conn sees (also the uncommitted ones of
        its transaction). A push loads it in its transaction: a push that fails
        leaves no relation in a registry."""
        from ..errors import LensError

        registry = cls()
        for r in await conn.fetch("SELECT relation, name, other FROM maker_relations WHERE NOT deleted"
                                  " ORDER BY created, gid"):
            try:
                makers.relate(registry.db, r["relation"], r["name"], r["other"])
            except LensError:  # not possible: the server stores only relations that the registry takes
                pass
        return registry

    def check(self, relation: str, name: str, other: str) -> None:
        """Record a relation; a Rejected when the registry refuses it (a relation
        before it says the opposite)."""
        from ..errors import LensError

        try:
            makers.relate(self.db, relation, name, other)
        except LensError as e:
            raise Rejected(f"the registry of the server refuses the relation: {e}") from None

    def group(self, name: str):
        return makers.group(self.db, name)


# Rows

def _key_text(table: str, row) -> str:
    """The key of a row as the client has it: the gid or sha256, else a JSON list."""
    values = [row[c] for c in KEY[table]]
    return values[0] if len(values) == 1 else json.dumps(values, separators=(",", ":"))


def _row_out(table: str, row) -> dict:
    out = {"tbl": table, "key": _key_text(table, row), "version": row["version"], "deleted": row["deleted"],
           "data": {name: row[column] for name, column in VALUES[table].items()}}
    if table == "parts" and row["merged_into"]:
        out["merged_into"] = row["merged_into"]
    return out


async def _row(conn, table: str, key: list) -> dict | None:
    where = " AND ".join(f"{c} = ${i + 1}" for i, c in enumerate(KEY[table]))
    row = await conn.fetchrow(f"SELECT * FROM {table} WHERE {where} FOR UPDATE", *key)
    return dict(row) if row else None


# Pull

class BadPosition(ValueError):
    """A wrong cursor or page position of a pull (HTTP 422)."""


def check_after(after) -> list:
    """The page position of a client: [table index, order values, upper bound]."""
    if not (isinstance(after, list) and len(after) == 3 and type(after[0]) is int and 0 <= after[0] < len(TABLES)
            and isinstance(after[1], list) and len(after[1]) == len(ORDER[TABLES[after[0]]])
            and all(isinstance(v, (str, bool)) for v in after[1]) and type(after[2]) is int and after[2] >= 0):
        raise BadPosition("after is not the value of the page before")
    return after


async def pull(pool, cursor: int | None, after: list | None) -> dict:
    """A page of the change feed (see the module docstring). cursor None: all
    rows that are not deleted (a new client). after: [table index, order values,
    upper bound] of the last row of the page before. All pages of one pull use
    the upper bound of the first page: a row that changes between two pages
    comes in the next pull, not never. The cursor of the last page is that bound."""
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        now = await conn.fetchval("SELECT pg_snapshot_xmin(pg_current_snapshot())")
        if (after and after[2] > now) or (cursor is not None and cursor > now):
            raise BadPosition("the cursor or the upper bound is after the current state of the server")
        xmin = after[2] if after else now
        rows: list[dict] = []
        position = after
        for index in range(after[0] if after else 0, len(TABLES)):
            table = TABLES[index]
            order = ORDER[table]
            where = ["xid < $1"]
            args: list[Any] = [xmin]
            if cursor is None:
                where.append("NOT deleted")
            else:
                args.append(cursor)
                where.append(f"xid >= ${len(args)}")
            if position and position[0] == index:
                marks = []
                for value in position[1]:
                    args.append(value)
                    marks.append(f"${len(args)}")
                where.append(f"({', '.join(order)}) > ({', '.join(marks)})")
            limit = PAGE - len(rows)
            found = await conn.fetch(f"SELECT *, {', '.join(f'{o} AS _o{i}' for i, o in enumerate(order))}"
                                     f" FROM {table} WHERE {' AND '.join(where)}"
                                     f" ORDER BY {', '.join(order)} LIMIT {limit + 1}", *args)
            for r in found[:limit]:
                rows.append(_row_out(table, r))
                position = [index, [r[f"_o{i}"] for i in range(len(order))], xmin]
            if len(found) > limit:  # more rows: the next page starts after the last row that this page has
                return {"rows": rows, "more": True, "after": position, "cursor": cursor}
        return {"rows": rows, "more": False, "after": None, "cursor": xmin}


# Push

class Pusher:
    """Applies the pushed changes of one account (one request: one transaction,
    one savepoint for each change)."""

    def __init__(self, conn, account: dict, registry: Registry):
        self.conn, self.account, self.registry = conn, account, registry
        self.own_registry = False  # True: a copy of the cached registry, for the relations of this push

    def require(self, role: str) -> None:
        from .config import ROLES

        if ROLES.index(self.account["role"]) < ROLES.index(role):
            raise Rejected(f"the account has the role {self.account['role']}: this change needs the role {role}")

    async def audit(self, table: str, key: str, op: str, old: dict | None, new: dict | None) -> None:
        await self.conn.execute("INSERT INTO audit (account_id, tbl, key, op, old, new) VALUES ($1, $2, $3, $4, $5,"
                                " $6)", self.account["id"], table, key, op,
                                json.dumps(old, default=str) if old else None,
                                json.dumps(new, default=str) if new else None)

    async def mapped(self, gid: str | None) -> str | None:
        """The kept part of a merged gid (gid_map), else the gid."""
        if gid is None:
            return None
        new = await self.conn.fetchval("SELECT new_gid FROM gid_map WHERE old_gid = $1", gid)
        return new or gid

    async def columns(self, table: str, data: dict) -> dict:
        """The server columns of the JSON of a row, with the part references translated."""
        values = {}
        for name, column in VALUES[table].items():
            if name in data:
                values[column] = data[name]
        for column in PART_REFS.get(table, ()):
            if column in values:
                values[column] = await self.mapped(values[column])
        return values

    async def key(self, table: str, key: str) -> list:
        try:
            values = [key] if len(KEY[table]) == 1 else json.loads(key)
        except json.JSONDecodeError:
            raise Rejected(f"wrong key for {table}") from None
        if not isinstance(values, list) or len(values) != len(KEY[table]) or not all(
                isinstance(v, str) and v for v in values):
            raise Rejected(f"wrong key for {table}")
        if KEY[table][0] == "part_gid":
            values[0] = await self.mapped(values[0])
        return values

    async def check_refs(self, table: str, values: dict) -> None:
        """A reference to a part or a document must be a row of the server that is not deleted."""
        for column in PART_REFS.get(table, ()):
            gid = values.get(column)
            if gid and not await self.conn.fetchval("SELECT 1 FROM parts WHERE gid = $1 AND NOT deleted", gid):
                raise Rejected(f"the part {gid} is not on the server (push it first, or pull)", retry=True)
        for column in ("document_sha", "supersedes"):
            sha = values.get(column)
            if sha and not await self.conn.fetchval("SELECT 1 FROM documents WHERE sha256 = $1 AND NOT deleted", sha):
                raise Rejected(f"the document {sha} is not on the server (push it first)", retry=True)

    @staticmethod
    def check_data(table: str, data) -> dict:
        """The JSON of a row: each value of the table, with its type."""
        if not isinstance(data, dict):
            raise Rejected("the data of the change is not an object")
        for name, kind in TYPES[table].items():
            value = data.get(name, ...)
            if value is ... or isinstance(value, bool) or not isinstance(value, kind):
                raise Rejected(f"{table}.{name}: missing or wrong type")
            if name in CHOICES and value not in CHOICES[name]:
                raise Rejected(f"{table}.{name} must be one of {', '.join(CHOICES[name])}")
        if table == "parts" and not data["mpn"].strip():
            raise Rejected("parts.mpn is empty")
        return data

    async def apply(self, change: dict) -> dict:
        table, op = change.get("tbl"), change.get("op")
        if table not in TABLES or op not in ("insert", "update", "delete"):
            raise Rejected(f"unknown change {table} {op}")
        if table == "bundles":
            raise Rejected("a bundle comes with POST /v1/bundles, not with a push")
        if not isinstance(change.get("key"), str):
            raise Rejected(f"wrong key for {table}")
        if op != "delete":
            change["data"] = self.check_data(table, change.get("data"))
        key = await self.key(table, change["key"])
        if op == "insert":
            return await self.insert(table, key, change.get("data") or {})
        if op == "update":
            return await self.update(table, key, change.get("data") or {}, change.get("base_version"))
        return await self.delete(table, key, change.get("base_version"))

    async def insert(self, table: str, key: list, data: dict) -> dict:
        self.require("contributor")
        values = await self.columns(table, data)
        values.update(dict(zip(KEY[table], key)))
        duplicate = None
        if table == "parts":
            if (kept := await self.mapped(key[0])) != key[0]:  # a part that the server merged before
                return {"result": "merged", "gid": kept}
        old = await _row(self.conn, table, key)
        if old is not None:
            if table == "parts" and old["merged_into"]:
                return {"result": "merged", "gid": await self.mapped(old["gid"])}
            # A link with values (the role of a document link, the values of a
            # purchase source): an insert of its key sets them, as an update (no
            # version: the last change is kept).
            changed = {c: v for c, v in values.items() if old.get(c) != v} if table in LINKS else {}
            if (old["deleted"] and table in LINKS) or changed:
                await self.check_refs(table, values)  # a link of a deleted part or document stays deleted
                sets = "".join(f", {c} = ${i + 2 + len(key)}" for i, c in enumerate(changed))
                await self.conn.execute(f"UPDATE {table} SET deleted = false{sets}, changed_by = $1"
                                        f" WHERE {self.where(table)}", self.account["id"], *key, *changed.values())
                await self.audit(table, _key_text(table, old), "insert" if old["deleted"] else "update", old, values)
                return {"result": "applied", "version": old["version"] + 1}
            if old["deleted"]:
                raise Rejected(f"the row of {table} is deleted on the server")
            return {"result": "applied", "version": old["version"]}  # the same insert again
        if table == "documents":
            size = await self.conn.fetchval("SELECT size FROM blobs WHERE sha256 = $1", key[0])
            if size is None:
                raise Rejected("the server does not have the file of the document: upload it first", retry=True)
            if values.get("size") != size:
                raise Rejected(f"the document has the size {values.get('size')}, its file {size}")
        if table == "maker_relations":
            if not self.own_registry:  # a copy for this push: the cache stays correct when the push fails
                self.registry, self.own_registry = await Registry.load(self.conn), True
            try:
                self.registry.check(values["relation"], values["name"], values["other"])
            except Rejected:
                self.registry = await Registry.load(self.conn)  # check() can have changed it
                raise
        if table == "knowledge" and data.get("status", "unchecked") != "unchecked":
            self.require("reviewer")
        await self.check_refs(table, values)
        if table == "parts":
            duplicate = await self.duplicate(values)
            if duplicate is not None and (duplicate["created"], duplicate["gid"]) < (values["created"], key[0]):
                await self.conn.execute("INSERT INTO gid_map (old_gid, new_gid) VALUES ($1, $2)"
                                        " ON CONFLICT (old_gid) DO NOTHING", key[0], duplicate["gid"])
                await self.audit(table, key[0], "merged", None, {"into": duplicate["gid"]})
                return {"result": "merged", "gid": duplicate["gid"]}
        columns = list(values)
        await self.conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}, changed_by) VALUES"
            f" ({', '.join(f'${i + 1}' for i in range(len(columns)))}, ${len(columns) + 1})",
            *values.values(), self.account["id"])
        await self.audit(table, _key_text(table, values), "insert", None, values)
        if table == "parts" and duplicate is not None:  # the pushed part is older: it is kept
            await merge_part(self.conn, keep=key[0], dup=duplicate["gid"], account_id=self.account["id"])
        if table == "maker_relations":
            await merge_duplicates(self.conn, self.registry, self.account["id"])
        return {"result": "applied", "version": 1}

    def where(self, table: str) -> str:
        return " AND ".join(f"{c} = ${i + 2}" for i, c in enumerate(KEY[table]))

    async def update(self, table: str, key: list, data: dict, base_version) -> dict:
        self.require("contributor")
        old = await _row(self.conn, table, key)
        if old is None or old["deleted"]:
            raise Rejected(f"the row of {table} is not on the server")
        if table in VERSIONED and base_version != old["version"]:
            raise Conflict(_row_out(table, old))
        values = await self.columns(table, data)
        changed = {c: v for c, v in values.items() if old.get(c) != v}
        if table == "knowledge" and changed and not (old["status"] == values["status"] == "unchecked"):
            # A fact that a reviewer checked or disputed, or a new status: only a reviewer.
            self.require("reviewer")
        if table == "documents" and {"size", "media_type"} & set(changed):
            raise Rejected("the size and the media type are the values of the file: they do not change")
        if table == "maker_relations" and changed:
            raise Rejected("a relation of manufacturer names does not change: record a new relation")
        if not changed:
            return {"result": "applied", "version": old["version"]}
        await self.check_refs(table, changed)
        sets = ", ".join(f"{c} = ${i + 2 + len(key)}" for i, c in enumerate(changed))
        await self.conn.execute(f"UPDATE {table} SET {sets}, changed_by = $1 WHERE {self.where(table)}",
                                self.account["id"], *key, *changed.values())
        await self.audit(table, _key_text(table, old), "update", {c: old[c] for c in changed}, changed)
        if table == "parts" and ({"mpn", "manufacturer"} & set(changed)):
            await merge_duplicates(self.conn, self.registry, self.account["id"], mpn=old["mpn"])
            await merge_duplicates(self.conn, self.registry, self.account["id"], mpn=changed.get("mpn", old["mpn"]))
        return {"result": "applied", "version": old["version"] + 1}

    async def delete(self, table: str, key: list, base_version) -> dict:
        self.require(ROLE_DELETE[table])
        old = await _row(self.conn, table, key)
        if old is None or old["deleted"]:
            return {"result": "applied", "version": old["version"] if old else 0}
        if base_version is not None and base_version != old["version"]:
            raise Conflict(_row_out(table, old))
        await tombstone(self.conn, table, key, self.account["id"])
        await self.audit(table, _key_text(table, old), "delete", old, None)
        return {"result": "applied", "version": old["version"] + 1}

    async def duplicate(self, values: dict) -> dict | None:
        """The oldest part of the server with the MPN and the manufacturer group of values."""
        group = self.registry.group(values.get("manufacturer", ""))
        for r in await self.conn.fetch("SELECT gid, manufacturer, created FROM parts WHERE lower(mpn) = lower($1)"
                                       " AND NOT deleted ORDER BY created, gid", values.get("mpn", "")):
            if self.registry.group(r["manufacturer"]) == group:
                return dict(r)
        return None


async def tombstone(conn, table: str, key: list, account_id: int | None) -> None:
    """Delete a row as the local store does, with its FK cascade (tombstones) and
    ON DELETE SET NULL."""
    where = " AND ".join(f"{c} = ${i + 2}" for i, c in enumerate(KEY[table]))
    await conn.execute(f"UPDATE {table} SET deleted = true, changed_by = $1 WHERE {where}", account_id, *key)
    if table == "parts":
        gid = key[0]
        for child in ("part_documents", "part_aliases", "part_sources"):
            await conn.execute(f"UPDATE {child} SET deleted = true, changed_by = $1 WHERE part_gid = $2"
                               " AND NOT deleted", account_id, gid)
        await conn.execute("UPDATE parts SET base_gid = NULL, changed_by = $1 WHERE base_gid = $2", account_id, gid)
        await conn.execute("UPDATE knowledge SET part_gid = NULL, changed_by = $1 WHERE part_gid = $2",
                           account_id, gid)
    elif table == "documents":
        sha = key[0]
        for child in ("part_documents", "knowledge"):
            await conn.execute(f"UPDATE {child} SET deleted = true, changed_by = $1 WHERE document_sha = $2"
                               " AND NOT deleted", account_id, sha)
        await conn.execute("UPDATE documents SET supersedes = NULL, changed_by = $1 WHERE supersedes = $2",
                           account_id, sha)


async def merge_part(conn, keep: str, dup: str, account_id: int | None) -> None:
    """Move the links, aliases, purchase sources, concrete parts and facts of the
    part dup to the part keep, then delete dup (merged_into keep): the steps of
    store._merge_part."""
    p = {"keep": keep, "dup": dup}
    await conn.execute("UPDATE parts k SET family = d.family, changed_by = $3 FROM parts d"
                       " WHERE k.gid = $1 AND d.gid = $2 AND k.family = '' AND d.family != ''", keep, dup, account_id)
    await conn.execute("UPDATE parts k SET base_gid = d.base_gid, changed_by = $3 FROM parts d"
                       " WHERE k.gid = $1 AND d.gid = $2 AND k.base_gid IS NULL AND d.base_gid IS NOT NULL"
                       " AND NOT EXISTS (SELECT 1 FROM parts c WHERE c.base_gid IN ($1, $2) AND NOT c.deleted)",
                       keep, dup, account_id)
    await conn.execute("INSERT INTO part_documents (part_gid, document_sha, role, changed_by)"
                       " SELECT $1, document_sha, role, $3 FROM part_documents WHERE part_gid = $2 AND NOT deleted"
                       " ON CONFLICT (part_gid, document_sha) DO UPDATE SET deleted = false, role = excluded.role,"
                       " changed_by = $3 WHERE part_documents.deleted", keep, dup, account_id)
    await conn.execute("INSERT INTO part_aliases (part_gid, alias, changed_by)"
                       " SELECT $1, alias, $3 FROM part_aliases WHERE part_gid = $2 AND NOT deleted"
                       " ON CONFLICT (part_gid, alias) DO UPDATE SET deleted = false, changed_by = $3"
                       " WHERE part_aliases.deleted", keep, dup, account_id)
    # A source that keep has stays as it is; a deleted one takes the values of dup.
    await conn.execute("INSERT INTO part_sources (part_gid, source, number, url, library, found, changed_by)"
                       " SELECT $1, source, number, url, library, found, $3 FROM part_sources"
                       " WHERE part_gid = $2 AND NOT deleted ON CONFLICT (part_gid, source, number) DO UPDATE"
                       " SET deleted = false, url = excluded.url, library = excluded.library, found = excluded.found,"
                       " changed_by = $3 WHERE part_sources.deleted", keep, dup, account_id)
    await conn.execute("UPDATE parts SET base_gid = coalesce((SELECT base_gid FROM parts WHERE gid = $1), $1),"
                       " changed_by = $3 WHERE base_gid = $2 AND NOT deleted", keep, dup, account_id)
    await conn.execute("UPDATE parts SET base_gid = NULL, changed_by = $1 WHERE base_gid = gid", account_id)
    await conn.execute("UPDATE knowledge SET part_gid = $1, changed_by = $3 WHERE part_gid = $2", keep, dup,
                       account_id)
    for child in ("part_documents", "part_aliases", "part_sources"):
        await conn.execute(f"UPDATE {child} SET deleted = true, changed_by = $2 WHERE part_gid = $1 AND NOT deleted",
                           dup, account_id)
    await conn.execute("UPDATE parts SET deleted = true, merged_into = $1, changed_by = $3 WHERE gid = $2",
                       keep, dup, account_id)
    await conn.execute("UPDATE gid_map SET new_gid = $1 WHERE new_gid = $2", keep, dup)
    await conn.execute("INSERT INTO gid_map (old_gid, new_gid) VALUES ($2, $1) ON CONFLICT (old_gid)"
                       " DO UPDATE SET new_gid = $1", keep, dup)
    await conn.execute("INSERT INTO audit (account_id, tbl, key, op, new) VALUES ($1, 'parts', $2, 'merge', $3)",
                       account_id, dup, json.dumps(p))


async def merge_duplicates(conn, registry: Registry, account_id: int | None, mpn: str | None = None) -> int:
    """Merge the parts with the same MPN and the same manufacturer (registry)
    into the oldest part (created, then gid), as store.merge_duplicates. Only
    the parts of mpn, when given. Give the number of merges."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('dslens registry'))")
    # Only the MPNs with more than one part can have a duplicate.
    rows = await conn.fetch("SELECT gid, mpn, manufacturer FROM parts WHERE NOT deleted AND lower(mpn) IN"
                            " (SELECT lower(mpn) FROM parts WHERE NOT deleted"
                            + (" AND lower(mpn) = lower($1)" if mpn else "")
                            + " GROUP BY 1 HAVING count(*) > 1) ORDER BY created, gid", *([mpn] if mpn else []))
    groups: dict[tuple, list[str]] = {}
    for r in rows:
        groups.setdefault((r["mpn"].lower(), registry.group(r["manufacturer"])), []).append(r["gid"])
    count = 0
    for keep, *dups in groups.values():
        for dup in dups:
            await merge_part(conn, keep, dup, account_id)
            count += 1
    return count


_registries: dict[int, tuple[tuple, Registry]] = {}  # id of the pool -> (state of the relations, registry)


async def cached_registry(conn, pool) -> Registry:
    """The registry of the relations of the server. The relations only grow (a
    relation does not change, a delete is a tombstone): their numbers and the last
    change identify a state."""
    state = tuple(await conn.fetchrow("SELECT count(*), count(*) FILTER (WHERE deleted), max(changed)"
                                      " FROM maker_relations"))
    cached = _registries.get(id(pool))
    if cached is None or cached[0] != state:
        cached = (state, await Registry.load(conn))
        _registries[id(pool)] = cached
    return cached[1]


async def push(pool, account: dict, changes: list[dict]) -> list[dict]:
    """Apply the changes in their order. One result for each change: applied
    (version), conflict (the row of the server), merged (gid of the kept part)
    or rejected (reason). One push at a time (a lock in the database): the
    search for a duplicate part and the merges of a relation see a registry
    and parts that no other push changes at the same time."""
    import asyncpg

    results = []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('dslens registry'))")
        pusher = Pusher(conn, account, await cached_registry(conn, pool))
        for change in changes:
            seq = change.get("seq") if isinstance(change, dict) else None
            try:
                if not isinstance(change, dict):
                    raise Rejected("a change is not an object")
                async with conn.transaction():  # a savepoint: a failed change does not undo the others
                    result = await pusher.apply(change)
            except Conflict as e:
                result = {"result": "conflict", "row": e.row}
            except Rejected as e:
                result = {"result": "rejected", "reason": str(e), "retry": e.retry}
            except (asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError) as e:
                # A value that the checks above do not find (for example a text that is too long).
                result = {"result": "rejected", "reason": f"the database refuses the change: {e}", "retry": False}
            results.append({"seq": seq, **result})
    return results


# Bundles

async def register_bundle(pool, account: dict, bundle: dict) -> dict:
    """Record a bundle of a client (its file is on the server). The first bundle
    of each backend stays; its time is the time of the server. Give the canonical
    bundle of the document, kind and format, which can be a different one."""
    from datetime import datetime, timezone

    if bundle["backend"] not in BUNDLE_BACKENDS[bundle["kind"]]:
        raise Rejected(f"a bundle of the kind {bundle['kind']} has the backend "
                       f"{' or '.join(BUNDLE_BACKENDS[bundle['kind']])}")
    created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT 1 FROM documents WHERE sha256 = $1 AND NOT deleted",
                                   bundle["document_sha"]):
            raise Rejected("the document of the bundle is not on the server")
        if not await conn.fetchval("SELECT 1 FROM blobs WHERE sha256 = $1 AND media_type = 'application/gzip'",
                                   bundle["blob_sha"]):
            raise Rejected("the server does not have the file of the bundle: upload it first")
        await conn.execute(
            "INSERT INTO bundles (document_sha, kind, format, backend, blob_sha, versions, created, changed_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (document_sha, kind, format, backend) DO NOTHING",
            bundle["document_sha"], bundle["kind"], bundle["format"], bundle["backend"], bundle["blob_sha"],
            json.dumps(bundle["versions"], sort_keys=True), created, account["id"])
        rows = [dict(r) for r in await conn.fetch(
            "SELECT document_sha, kind, format, backend, blob_sha, versions, created FROM bundles"
            " WHERE document_sha = $1 AND kind = $2 AND format = $3 AND NOT deleted",
            bundle["document_sha"], bundle["kind"], bundle["format"])]
    return min(rows, key=canonical_key)
