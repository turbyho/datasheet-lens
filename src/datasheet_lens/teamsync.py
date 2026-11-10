"""The client of the team server: one shared store for the datasheets of a team.

The local store stays the store that the MCP server and the CLI read. The
triggers of the shared tables put each local change into the outbox (store.py).
A sync pushes the outbox, then pulls the changes of the server:

- push: the outbox in its order, at most PUSH_BATCH rows in one request. The
  file of a new document goes to the server first. The answer has a result for
  each row (applied, conflict, merged, rejected); the client removes the row.
- pull: the changes after the cursor, in pages. The client applies a page in one
  transaction, in store.not_recorded (no outbox rows).

The token of the account comes from keys ("datasheet-lens key team"). Only the
standard library: the base package has no HTTP client.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import os
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import makers
from .errors import LensError
from .fileutil import temp_path
from .store import (_VERSION_TABLES, FULL_PULL, Document, Store, _merge_part, merge_duplicates, not_recorded, now,
                    record_all)

PUSH_BATCH = 500
SINGLE_PUT_MAX = 32 << 20  # bytes: a larger file goes as a multipart upload (it can continue after a stop)
RETRY_LIMIT = 5            # pushes of a row that refers to a row that the server does not have
LEASE = 900.0              # seconds: one sync at a time in all processes of a store (renewed at each step)
TIMEOUT = 60
SYNC_INTERVAL = 600        # seconds between two syncs in the background (MCP server)
NOTES_KEPT = 50
VERSIONED = _VERSION_TABLES  # the tables whose rows have a version of the server
# The shared tables of a team server that does not give its tables in /v1/account
# (an older version): the tables before part_sources.
BASE_TABLES = ("documents", "bundles", "maker_relations", "parts", "part_documents", "part_aliases", "knowledge")


class Offline(LensError):
    """No connection to the team server: the tools work with the local store."""


@dataclass
class Report:
    pushed: int = 0
    pulled: int = 0
    notes: list[str] = field(default_factory=list)
    bundles: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"pushed {self.pushed} changes, pulled {self.pulled} rows" + (
            f", {len(self.bundles)} bundles of derived data" if self.bundles else "") + (
            f"; {len(self.notes)} notes (datasheet-lens server status)" if self.notes else "")


# The state of the store

def state(store: Store) -> dict:
    row = store.db.execute("SELECT * FROM sync_state").fetchone()
    waiting = store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
    return {**dict(row), "waiting": waiting}


def connected(store: Store) -> bool:
    return bool(store.db.execute("SELECT server FROM sync_state").fetchone()[0])


def note(db: sqlite3.Connection, text: str) -> str:
    """Keep a note for the user (the last NOTES_KEPT). A note that is there already
    is not a second note: it comes again to the model, with the new time. Give the
    text."""
    if db.execute("UPDATE sync_notes SET created = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), shown = 0"
                  " WHERE text = ?", (text,)).rowcount:
        return text
    db.execute("INSERT INTO sync_notes (text) VALUES (?)", (text,))
    db.execute("DELETE FROM sync_notes WHERE id NOT IN (SELECT id FROM sync_notes ORDER BY id DESC LIMIT ?)",
               (NOTES_KEPT,))
    return text


def notes(store: Store, limit: int = 10) -> list[str]:
    return [f"{r[0]} {r[1]}" for r in store.db.execute(
        "SELECT created, text FROM sync_notes ORDER BY id DESC LIMIT ?", (limit,))]


def model_notes(store: Store) -> list[str]:
    """The lines for the model in a status tool: an error of the last sync, and
    the notes that no tool gave before (conflicts, refused changes)."""
    if not connected(store):
        return []
    s = state(store)
    lines = []
    if s["last_error"]:
        lines.append(f"team server: the last sync failed ({s['last_error']}); the tools use the local store "
                     f"(last complete sync: {s['last_sync'] or 'never'}, {s['waiting']} local changes wait). "
                     "Tell the user.")
    rows = store.db.execute("SELECT id, text FROM sync_notes WHERE NOT shown ORDER BY id").fetchall()
    if rows:
        lines += [f"team server: {r['text']}. Tell the user." for r in rows]
        with store.db:
            store.db.execute(f"UPDATE sync_notes SET shown = 1 WHERE id IN ({', '.join('?' * len(rows))})",
                             [r["id"] for r in rows])
    return lines


def sync_before_answer(store: Store) -> list[str]:
    """A sync before a tool tells that a part has no datasheet: a document that a
    different person added a short time before is then found. With no connection,
    a line tells that the server was not asked."""
    if not connected(store):
        return []
    try:
        sync(store, wait=120, send=False)  # only the rows of the server: no uploads in a tool call
        return []
    except Offline as e:
        return [f"team server: not asked ({e}); last complete sync: {state(store)['last_sync'] or 'never'}"]
    except LensError as e:
        return [f"team server: the sync failed ({e})"]


# HTTP

class Remote:
    """The API of the team server."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body=None, params: dict | None = None, timeout: int = TIMEOUT) -> dict:
        url = self.url + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                try:
                    return json.load(r)
                except ValueError:  # for example the page of a proxy or of a captive portal
                    raise LensError(f"team server {self.url}: {method} {path}: the answer is not JSON") from None
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("detail", "")
            except (ValueError, AttributeError):
                detail = ""
            raise LensError(f"team server {self.url}: {method} {path}: HTTP {e.code} {detail}".rstrip()) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Offline(f"no connection to the team server {self.url}: {getattr(e, 'reason', e)}") from None

    @staticmethod
    def transfer(method: str, url: str, headers: dict, body=None, length: int | None = None,
                 timeout: int = 600):
        """A request to a presigned URL of the object store (no token). Give the response."""
        headers = dict(headers)
        if length is not None:
            headers["Content-Length"] = str(length)
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise LensError(f"object store of the team server: {method}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Offline(f"no connection to the object store of the team server: {getattr(e, 'reason', e)}") \
                from None

    # Files

    def upload(self, store: Store, path: Path, sha256: str, media_type: str) -> None:
        """Put a file on the server: one PUT, or a multipart upload that continues
        after a stop (sync_uploads keeps its ID)."""
        info = {"sha256": sha256, "size": path.stat().st_size, "media_type": media_type}
        if info["size"] <= SINGLE_PUT_MAX:
            answer = self.call("POST", "/v1/blobs", info)
            if answer["exists"]:
                return
            with path.open("rb") as f:
                self.transfer("PUT", answer["url"], answer["headers"], f, info["size"]).close()
            self.call("POST", f"/v1/blobs/{sha256}/done", info)
            return
        row = store.db.execute("SELECT upload_id FROM sync_uploads WHERE sha256 = ?", (sha256,)).fetchone()
        upload = None
        if row:
            try:
                upload = self.call("GET", f"/v1/uploads/{row[0]}")
                upload["upload_id"] = row[0]
            except Offline:
                raise
            except LensError:  # the server removed it (24 hours): a new upload
                upload = None
        if upload is None:
            answer = self.call("POST", "/v1/uploads", info)
            if answer["exists"]:
                return
            upload = {**answer, "uploaded": [], "completed": False}
            store.db.execute("INSERT OR REPLACE INTO sync_uploads VALUES (?, ?)", (sha256, answer["upload_id"]))
            store.db.commit()
        upload_id, part_size = upload["upload_id"], upload["part_size"]
        if not upload.get("completed"):
            done = {p["number"] for p in upload["uploaded"]}
            with path.open("rb") as f:
                for number in range(1, upload["parts"] + 1):
                    if number in done:
                        continue
                    f.seek((number - 1) * part_size)
                    chunk = f.read(part_size)
                    part = self.call("POST", f"/v1/uploads/{upload_id}/urls", {
                        "parts": [{"number": number, "sha256": hashlib.sha256(chunk).hexdigest()}]})["parts"][0]
                    self.transfer("PUT", part["url"], part["headers"], chunk, len(chunk)).close()
        self.call("POST", f"/v1/uploads/{upload_id}/complete", timeout=600)
        store.db.execute("DELETE FROM sync_uploads WHERE sha256 = ?", (sha256,))
        store.db.commit()

    def fetch_blob(self, sha256: str) -> bytes:
        """A file of the server in memory (a bundle). The SHA-256 must agree."""
        answer = self.call("GET", f"/v1/blobs/{sha256}")
        with self.transfer("GET", answer["url"], {}) as response:
            raw = response.read()
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise LensError(f"the file {sha256[:12]} from the team server has a different SHA-256")
        return raw

    def download(self, doc: Document) -> Path:
        """Get the file of a document from the server into the store. The SHA-256
        must agree; else no file."""
        answer = self.call("GET", f"/v1/blobs/{doc.sha256}")
        tmp = temp_path(doc.path)
        digest = hashlib.sha256()
        try:
            with self.transfer("GET", answer["url"], {}) as response, tmp.open("wb") as f:
                for block in iter(lambda: response.read(1 << 20), b""):
                    digest.update(block)
                    f.write(block)
            if digest.hexdigest() != doc.sha256:
                raise LensError(f"the file from the team server has the SHA-256 {digest.hexdigest()}, "
                                f"not {doc.sha256}: not stored")
            tmp.replace(doc.path)
        finally:
            tmp.unlink(missing_ok=True)
        return doc.path


def _token() -> str:
    from .keys import get_key

    token = get_key("team")
    if not token:
        raise LensError("no token for the team server: save it with \"datasheet-lens key team\"")
    return token


def remote(store: Store) -> Remote:
    server = store.db.execute("SELECT server FROM sync_state").fetchone()[0]
    if not server:
        raise LensError("the store is not connected to a team server (datasheet-lens server connect URL)")
    return Remote(server, _token())


def download(store: Store, doc: Document) -> Path:
    """The file of a document that is only on the team server (Store.local_file)."""
    if not connected(store):
        raise LensError(f"the file of the document {doc.sha256[:12]} {doc.filename} is not in the store")
    return remote(store).download(doc)


# Connect

def connect(store: Store, url: str) -> list[str]:
    """Connect the store to a team server: the first push sends all shared rows
    and files, then the first pull. Give lines for the user."""
    url = url.strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise LensError("the URL of the team server must start with https://")
    account = Remote(url, _token()).call("GET", "/v1/account")
    current = store.db.execute("SELECT server FROM sync_state").fetchone()[0]
    if current and current != url:
        raise LensError(f"the store is connected to {current}: disconnect first (datasheet-lens server disconnect)")
    lines = [f"team server {url}: account {account['name']}, role {account['role']}"]
    if account.get("makers_version") != makers.SEED_VERSION:
        lines.append("note: the server has a different version of the built-in manufacturer names: "
                     "install the same version of datasheet-lens on the server and the clients")
    if not current:
        with store.db:
            # The full state of the store: an older outbox has only a part of it.
            store.db.execute("DELETE FROM outbox")
            count = record_all(store.db)
            store.db.execute("UPDATE sync_state SET server = ?, cursor = NULL, recording = 1, last_error = NULL",
                             (url,))
        lines.append(f"{count} local rows go to the server")
    lines.append(sync(store).line())
    return lines


def disconnect(store: Store) -> int:
    """Stop the synchronization. The local rows stay; the changes that the server
    did not get are removed. Give their number."""
    with store.db:
        count = store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        store.db.execute("DELETE FROM outbox")
        store.db.execute("DELETE FROM sync_uploads")
        store.db.execute("UPDATE sync_state SET server = NULL, cursor = NULL, recording = 0, last_sync = NULL,"
                         " last_error = NULL")
    return count


# Sync

_local = threading.Lock()


class Lease:
    """One sync of a store at a time, in all processes: a time limit and an owner
    in sync_state. The sync renews it at each step; a sync that lost it stops."""

    def __init__(self, store: Store):
        self.db, self.owner = store.db, f"{os.getpid()}-{uuid.uuid4().hex}"

    def take(self) -> bool:
        with self.db:
            return self.db.execute("UPDATE sync_state SET lease_until = ?, lease_owner = ? WHERE lease_until < ?",
                                   (time.time() + LEASE, self.owner, time.time())).rowcount == 1

    def renew(self) -> None:
        with self.db:
            if self.db.execute("UPDATE sync_state SET lease_until = ? WHERE lease_owner = ?",
                               (time.time() + LEASE, self.owner)).rowcount != 1:
                raise LensError("the sync lost its lease: a different process syncs the store")

    def release(self) -> None:
        with self.db:
            self.db.execute("UPDATE sync_state SET lease_until = 0, lease_owner = NULL WHERE lease_owner = ?",
                            (self.owner,))


def sync(store: Store, wait: float = 0, send: bool = True) -> Report:
    """Push the outbox (send), then pull. One sync at a time (Lease). wait:
    seconds to wait for a sync of this process that runs (then this sync also
    gets the newest rows). Each error is in last_error, and goes to the caller."""
    if not (_local.acquire(timeout=wait) if wait else _local.acquire(blocking=False)):
        raise LensError("a sync runs in this process")
    try:
        lease = Lease(store)
        if not lease.take():
            raise LensError("a sync of this store runs in a different process")
        try:
            client = remote(store)
            report = Report()
            account = client.call("GET", "/v1/account") if send else {}
            if send:
                push(store, client, report, lease, set(account.get("tables", BASE_TABLES)))
            pull(store, client, report, lease)
            if send:  # the bundles of derived data: uploads and downloads, not in a tool call
                from .bundles import reconcile

                features = account.get("features", [])
                lines = reconcile(store, client, lease) if "bundles" in features else [
                    "error: the team server has no bundles of derived data: update the server"]
                for line in lines:
                    report.bundles.append(line)
                    if line.startswith("error:"):
                        with store.db:
                            report.notes.append(note(store.db, line))
            with store.db:
                store.db.execute("UPDATE sync_state SET last_sync = ?, last_error = NULL", (now(),))
            return report
        except Exception as e:  # each error: the user sees the reason in the status
            store.db.rollback()
            with store.db:
                store.db.execute("UPDATE sync_state SET last_error = ?",
                                 (str(e) if isinstance(e, LensError) else f"{type(e).__name__}: {e}",))
            raise
        finally:
            lease.release()
    finally:
        _local.release()


def _local_id(db: sqlite3.Connection, table: str, key: str) -> int | None:
    column = "sha256" if table == "documents" else "gid"
    row = db.execute(f"SELECT id FROM {table} WHERE {column} = ?", (key,)).fetchone()
    return row[0] if row else None


def _remap(db: sqlite3.Connection, old: str, new: str) -> None:
    """The outbox rows that refer to the part old refer to the part new. A gid is
    a UUID: a text replace cannot change a different value."""
    db.execute("UPDATE outbox SET key = replace(key, ?, ?), data = replace(data, ?, ?)"
               " WHERE instr(key, ?) OR instr(coalesce(data, ''), ?)", (old, new, old, new, old, old))


def _batch(db: sqlite3.Connection, skip: set[int]) -> list[sqlite3.Row]:
    """The next outbox rows to push: at most PUSH_BATCH, in order, with at most one
    row of each versioned row. A second change of a row waits for the result of
    the first: it needs the new version of the server as its base version. The
    rows of skip wait for the next sync, and the later changes of their rows too."""
    rows, keys = [], set()
    waiting = {(r["tbl"], r["key"]) for r in db.execute(
        f"SELECT tbl, key FROM outbox WHERE seq IN ({', '.join('?' * len(skip))})", list(skip))} if skip else set()
    for r in db.execute("SELECT * FROM outbox ORDER BY seq").fetchall():
        if r["seq"] in skip or (r["tbl"], r["key"]) in waiting:
            continue
        if r["tbl"] in VERSIONED:
            if (r["tbl"], r["key"]) in keys:
                break
            keys.add((r["tbl"], r["key"]))
        rows.append(r)
        if len(rows) == PUSH_BATCH:
            break
    return rows


def push(store: Store, client: Remote, report: Report, lease: Lease, tables: set[str]) -> None:
    """Send the outbox to a server that knows the shared tables tables (/v1/account).
    A change of a different table stays in the outbox, is not sent and gets no
    attempt: the next push after an upgrade of the server sends it. One note for
    each such table."""
    db = store.db
    unknown = sorted({r[0] for r in db.execute("SELECT DISTINCT tbl FROM outbox")} - tables)
    # The rows that wait (see _batch): one attempt in each sync, and the rows of the unknown tables.
    retried = {r[0] for r in db.execute(f"SELECT seq FROM outbox WHERE tbl IN ({', '.join('?' * len(unknown))})",
                                        unknown)} if unknown else set()
    while rows := _batch(db, retried):
        lease.renew()
        for r in rows:  # the files first: the server takes a document only with its file
            if r["tbl"] == "documents" and r["op"] == "insert":
                doc = store.document(r["key"])
                if doc is not None and doc.path.is_file():
                    client.upload(store, doc.path, doc.sha256, doc.media_type)
                    lease.renew()
        changes = [{"seq": r["seq"], "tbl": r["tbl"], "op": r["op"], "key": r["key"],
                    "base_version": r["base_version"], "data": json.loads(r["data"]) if r["data"] else None}
                   for r in rows]
        results = client.call("POST", "/v1/sync/push", {"changes": changes}, timeout=300)["results"]
        if len(results) != len(rows):
            raise LensError("the team server gave a wrong number of results")
        supersedes: dict[str, str] = {}
        with db, not_recorded(db):
            for r, result in zip(rows, results):
                kind = result["result"]
                if kind == "rejected" and result.get("retry") and r["attempts"] + 1 < RETRY_LIMIT:
                    db.execute("UPDATE outbox SET attempts = attempts + 1 WHERE seq = ?", (r["seq"],))
                    retried.add(r["seq"])
                    continue
                db.execute("DELETE FROM outbox WHERE seq = ?", (r["seq"],))
                report.pushed += 1
                if kind == "applied" and r["tbl"] in VERSIONED and r["op"] != "delete":
                    column = "sha256" if r["tbl"] == "documents" else "gid"
                    db.execute(f"UPDATE {r['tbl']} SET version = ? WHERE {column} = ?", (result["version"], r["key"]))
                    # The next local changes of the row start from this version.
                    db.execute("UPDATE outbox SET base_version = ? WHERE tbl = ? AND key = ? AND op != 'insert'",
                               (result["version"], r["tbl"], r["key"]))
                elif kind == "merged":
                    _adopt_part(db, r["key"], result["gid"])
                    report.notes.append(f"part {r['key'][:8]} is the part {result['gid'][:8]} of the server")
                elif kind == "conflict":
                    _apply(db, [result["row"]], supersedes)
                    if r["tbl"] in VERSIONED:
                        db.execute("UPDATE outbox SET base_version = ? WHERE tbl = ? AND key = ? AND op != 'insert'",
                                   (result["row"]["version"], r["tbl"], r["key"]))
                    report.notes.append(note(db, f"conflict: {r['tbl']} {r['key']}: the server has a newer "
                                                  "version; it is kept"))
                elif kind == "rejected":
                    report.notes.append(note(db, f"refused by the server: {r['tbl']} {r['op']} {r['key']}: "
                                                  f"{result.get('reason', '')}"))
            _set_supersedes(db, supersedes)
    with db:
        for table in unknown:
            # One text for each table: note() keeps one note for one text, and a count in
            # the text would give a new note at each push.
            report.notes.append(note(db, f"the team server does not know the table {table}: upgrade the team "
                                         "server; the changes of this table wait in the outbox"))


def _adopt_part(db: sqlite3.Connection, local_gid: str, server_gid: str) -> None:
    """The server merged the local part into its part server_gid: the local part
    takes that gid, or merges into the local copy of the server part."""
    local = _local_id(db, "parts", local_gid)
    kept = _local_id(db, "parts", server_gid)
    if local is not None and kept is not None and local != kept:
        _merge_part(db, kept, local)
    elif local is not None and kept is None:
        db.execute("UPDATE parts SET gid = ? WHERE id = ?", (server_gid, local))
    _remap(db, local_gid, server_gid)


def pull(store: Store, client: Remote, report: Report, lease: Lease) -> None:
    """Get the changes after the cursor of the store. A store with the meta key
    FULL_PULL then gets all live rows of the server again, with no change of the
    cursor (an older version ignored the rows of a table that it did not know)."""
    db = store.db
    cursor = db.execute("SELECT cursor FROM sync_state").fetchone()[0]
    _pull(db, client, report, lease, cursor, record_cursor=True)
    if db.execute("SELECT 1 FROM meta WHERE key = ?", (FULL_PULL,)).fetchone():
        if cursor is not None:  # with no cursor, the pull above was a full pull
            _pull(db, client, report, lease, None, record_cursor=False)
        with db:
            db.execute("DELETE FROM meta WHERE key = ?", (FULL_PULL,))


def _pull(db: sqlite3.Connection, client: Remote, report: Report, lease: Lease, cursor: int | None,
          record_cursor: bool) -> None:
    """All pages of a pull from cursor (None: all live rows). record_cursor: record
    the cursor of the last page as the cursor of the store."""
    after = None
    supersedes: dict[str, str] = {}
    while True:
        params = {}
        if cursor is not None:
            params["cursor"] = cursor
        if after is not None:
            params["after"] = json.dumps(after)
        lease.renew()
        page = client.call("GET", "/v1/sync/pull", params=params, timeout=300)
        with db, not_recorded(db):
            relations = _apply(db, page["rows"], supersedes)
            if relations:
                merge_duplicates(db)
            if not page["more"]:
                _set_supersedes(db, supersedes)
                if record_cursor:
                    db.execute("UPDATE sync_state SET cursor = ?", (page["cursor"],))
        report.pulled += len(page["rows"])
        if not page["more"]:
            return
        after = page["after"]


def _set_supersedes(db: sqlite3.Connection, supersedes: dict[str, str]) -> None:
    """documents.supersedes at the end of a pull: the document that it refers to
    can come later in the pull."""
    for sha, old in supersedes.items():
        db.execute("UPDATE documents SET supersedes_id = (SELECT id FROM documents WHERE sha256 = ?)"
                   " WHERE sha256 = ?", (old, sha))


def _apply(db: sqlite3.Connection, rows: list[dict], supersedes: dict[str, str]) -> bool:
    """Apply rows of the server to the local store (in not_recorded). True when
    a relation of manufacturer names came (the caller merges the parts)."""
    relations = False
    for row in rows:
        table, key, data = row["tbl"], row["key"], row.get("data") or {}
        if table == "documents":
            _apply_document(db, key, row, data, supersedes)
        elif table == "bundles":
            _apply_bundle(db, key, row, data)
        elif table == "maker_relations":
            relations |= _apply_relation(db, key, row, data)
        elif table == "parts":
            _apply_part(db, key, row, data)
        elif table in ("part_documents", "part_aliases"):
            _apply_link(db, table, key, row, data)
        elif table == "knowledge":
            _apply_fact(db, key, row, data)
        elif table == "part_sources":
            _apply_source(db, key, row, data)
    return relations


def _apply_document(db, sha, row, data, supersedes) -> None:
    if row["deleted"]:
        db.execute("DELETE FROM documents WHERE sha256 = ?", (sha,))
        return
    values = {c: data[c] for c in ("filename", "media_type", "size", "pages", "title", "manufacturer", "doc_number",
                                   "revision", "revision_date", "source_url", "added")}
    values["version"] = row["version"]
    if _local_id(db, "documents", sha) is None:
        db.execute(f"INSERT INTO documents (sha256, {', '.join(values)}) VALUES (?{', ?' * len(values)})",
                   (sha, *values.values()))
    else:
        db.execute(f"UPDATE documents SET {', '.join(f'{c} = ?' for c in values)} WHERE sha256 = ?",
                   (*values.values(), sha))
    if data.get("supersedes"):
        supersedes[sha] = data["supersedes"]
    else:
        db.execute("UPDATE documents SET supersedes_id = NULL WHERE sha256 = ?", (sha,))


def _apply_bundle(db, key, row, data) -> None:
    """A bundle of derived data of the server (bundles.py uses it when a tool needs
    the derived data, or in reconcile)."""
    sha, kind, fmt, backend = json.loads(key)
    if row["deleted"]:
        db.execute("DELETE FROM bundles WHERE document_sha = ? AND kind = ? AND format = ? AND backend = ?",
                   (sha, kind, fmt, backend))
    else:
        db.execute("INSERT OR REPLACE INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (sha, kind, fmt, backend, data["blob_sha"], data["versions"], data["created"]))


def _apply_relation(db, gid, row, data) -> bool:
    if row["deleted"] or _local_id(db, "maker_relations", gid) is not None:
        return False
    try:
        makers.relate(db, data["relation"], data["name"], data["other"])
    except LensError as e:  # a local decision says the opposite
        note(db, f"the relation {data['relation']} {data['name']!r} {data['other']!r} of the team server is "
                  f"not in the local registry: {e}")
    db.execute("INSERT INTO maker_relations (relation, name, other, created, gid) VALUES (?, ?, ?, ?, ?)",
               (data["relation"], data["name"], data["other"], data["created"], gid))
    return True


def _apply_part(db, gid, row, data) -> None:
    local = _local_id(db, "parts", gid)
    if row["deleted"]:
        if local is None:
            return
        into = _local_id(db, "parts", row["merged_into"]) if row.get("merged_into") else None
        if into is not None and into != local:
            _merge_part(db, into, local)
        else:
            db.execute("DELETE FROM parts WHERE id = ?", (local,))
        return
    base = _local_id(db, "parts", data["base"]) if data.get("base") else None
    values = (data["mpn"], data["manufacturer"], data["family"], data["created"], base, row["version"])
    if local is None:
        # A local part with the same MPN and manufacturer: the server has only this
        # part. The local part is not on the server (version 0: for example a part
        # of a reader), or the server merged it into this part (its tombstone comes
        # in this pull, before or after). The local row takes the identity of the
        # server part: no second part, and no conflict with the unique MPN and
        # manufacturer of the store.
        twin = _twin(db, data["mpn"], data["manufacturer"], gid)
        if twin is not None:
            old_gid = db.execute("SELECT gid FROM parts WHERE id = ?", (twin,)).fetchone()[0]
            db.execute("DELETE FROM outbox WHERE tbl = 'parts' AND op = 'insert' AND key = ?", (old_gid,))
            db.execute("UPDATE parts SET gid = ? WHERE id = ?", (gid, twin))
            _remap(db, old_gid, gid)
            local = twin
    if local is None:
        db.execute("INSERT INTO parts (mpn, manufacturer, family, created, base_id, version, gid)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)", (*values, gid))
        return
    # A changed MPN or manufacturer can be the MPN and manufacturer of a different
    # local part: the server has only this part, thus the other local part goes
    # into it (no second part, no conflict with the unique MPN and manufacturer).
    other = _twin(db, data["mpn"], data["manufacturer"], gid)
    if other is not None and other != local:
        old_gid = db.execute("SELECT gid FROM parts WHERE id = ?", (other,)).fetchone()[0]
        db.execute("DELETE FROM outbox WHERE tbl = 'parts' AND op = 'insert' AND key = ?", (old_gid,))
        _merge_part(db, local, other)
        _remap(db, old_gid, gid)
    db.execute("UPDATE parts SET mpn = ?, manufacturer = ?, family = ?, created = ?, base_id = ?, version = ?"
               " WHERE id = ?", (*values, local))


def _twin(db, mpn: str, manufacturer: str, gid: str) -> int | None:
    """A local part with the MPN and the manufacturer (registry) of a part of the
    server, and a different gid."""
    group = makers.group(db, manufacturer)
    for r in db.execute("SELECT id, manufacturer FROM parts WHERE mpn = ? AND gid != ?", (mpn, gid)).fetchall():
        if makers.group(db, r["manufacturer"]) == group:
            return r["id"]
    return None


def _apply_link(db, table, key, row, data) -> None:
    gid, second = json.loads(key)
    part = _local_id(db, "parts", gid)
    if table == "part_documents":
        doc = _local_id(db, "documents", second)
        if part is None or doc is None:
            return  # deleted with its part or document
        if row["deleted"]:
            db.execute("DELETE FROM part_documents WHERE part_id = ? AND document_id = ?", (part, doc))
        else:
            db.execute("INSERT INTO part_documents (part_id, document_id, role) VALUES (?, ?, ?)"
                       " ON CONFLICT (part_id, document_id) DO UPDATE SET role = excluded.role",
                       (part, doc, data["role"]))
    else:
        if part is None:
            return
        if row["deleted"]:
            db.execute("DELETE FROM part_aliases WHERE part_id = ? AND alias = ?", (part, second))
        else:
            db.execute("INSERT OR IGNORE INTO part_aliases (part_id, alias) VALUES (?, ?)", (part, second))


def _apply_source(db, key, row, data) -> None:
    """A purchase source of the server: the server row replaces the values of the
    local row (a table with no version: the last change is kept)."""
    gid, source, number = json.loads(key)
    part = _local_id(db, "parts", gid)
    if part is None:
        return  # deleted with its part
    if row["deleted"]:
        db.execute("DELETE FROM part_sources WHERE part_id = ? AND source = ? AND number = ?", (part, source, number))
    else:
        db.execute("INSERT INTO part_sources (part_id, source, number, url, library, found) VALUES (?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (part_id, source, number) DO UPDATE SET url = excluded.url,"
                   " library = excluded.library, found = excluded.found",
                   (part, source, number, data["url"], data["library"], data["found"]))


FACT_COLUMNS = ("page", "element_id", "bbox", "subject", "value", "unit", "conditions", "column_name", "note",
                "source_kind", "model", "prompt_version", "author", "created", "status")


def _apply_fact(db, gid, row, data) -> None:
    local = _local_id(db, "knowledge", gid)
    if row["deleted"]:
        if local is not None:
            db.execute("DELETE FROM knowledge WHERE id = ?", (local,))
        return
    doc = _local_id(db, "documents", data["document"])
    if doc is None:
        return
    part = _local_id(db, "parts", data["part"]) if data.get("part") else None
    values = {"part_id": part, "document_id": doc, **{c: data[c] for c in FACT_COLUMNS}, "version": row["version"]}
    if local is None:
        local = db.execute(f"INSERT INTO knowledge ({', '.join(values)}, gid) VALUES ({', '.join('?' * len(values))},"
                           " ?)", (*values.values(), gid)).lastrowid
    else:
        db.execute(f"UPDATE knowledge SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                   (*values.values(), local))
    # The element ID of the fact follows the local element map (bundles.relink).
    from .bundles import relink

    relink(db, doc, [local])


# The background sync of the MCP server

def background(stop: threading.Event, open_store) -> None:
    """Sync at the start and then each SYNC_INTERVAL seconds, until stop. An error
    is in last_error (the tools show it); the loop continues."""
    while not stop.is_set():
        try:
            with open_store() as store:
                if connected(store):
                    sync(store)
        except LensError:
            pass  # last_error has it; a sync of a different process is also not an error here
        except Exception as e:  # the loop must continue; sync() put the error into last_error
            print(f"datasheet-lens: team sync: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        stop.wait(SYNC_INTERVAL)
