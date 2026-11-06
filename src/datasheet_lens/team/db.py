"""The PostgreSQL database of the team server: accounts, tokens, files.

A token is "dsl_" and 64 hex digits (256 bits). The database keeps only the
SHA-256 of a token: the plain token is shown one time, when it is made.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .config import ROLES
from .sync import SCHEMA as SYNC_SCHEMA

# 2: the shared tables (sync.SCHEMA); 3: the bundles of derived data; 4: the purchase
# sources of the parts (sync.SCHEMA makes the new table on the next start).
SCHEMA_VERSION = 4
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    f"""CREATE TABLE IF NOT EXISTS accounts (
        id      SERIAL PRIMARY KEY,
        name    TEXT NOT NULL UNIQUE,
        role    TEXT NOT NULL CHECK (role IN ({", ".join(f"'{r}'" for r in ROLES)})),
        active  BOOLEAN NOT NULL DEFAULT true,
        note    TEXT NOT NULL DEFAULT '',
        created TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS tokens (
        id          SERIAL PRIMARY KEY,
        account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        token_hash  BYTEA NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        created     TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires     TIMESTAMPTZ,
        last_used   TIMESTAMPTZ,
        revoked     TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS tokens_account ON tokens (account_id)",
    # The files in the object store (key blobs/<sha256>).
    """CREATE TABLE IF NOT EXISTS blobs (
        sha256     TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        size       BIGINT NOT NULL,
        media_type TEXT NOT NULL,
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        uploaded   TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # The multipart uploads that are not complete (key staging/<id>).
    """CREATE TABLE IF NOT EXISTS uploads (
        id         TEXT PRIMARY KEY,
        s3_upload  TEXT NOT NULL,
        sha256     TEXT NOT NULL,
        size       BIGINT NOT NULL,
        media_type TEXT NOT NULL,
        part_size  BIGINT NOT NULL,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        started    TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
] + SYNC_SCHEMA
TOKEN_PREFIX = "dsl_"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class Database:
    """A pool of connections to the database of the server."""

    def __init__(self, url: str, min_size: int = 1, max_size: int = 10):
        self.url, self.min_size, self.max_size = url, min_size, max_size
        self.pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.url, min_size=self.min_size, max_size=self.max_size,
                                              command_timeout=60)
        async with self.pool.acquire() as conn:  # the pool connects lazily: find a wrong URL now
            await conn.fetchval("SELECT 1")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def init_schema(self) -> None:
        """Make the tables (a second run changes nothing)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('dslens schema'))")
            for statement in SCHEMA:
                await conn.execute(statement)
            version = await conn.fetchval("SELECT value FROM meta WHERE key = 'schema_version'")
            if version is not None and int(version) > SCHEMA_VERSION:
                raise RuntimeError(f"database schema version {version} is newer than this server "
                                   f"({SCHEMA_VERSION}): install a newer version")
            await conn.execute("INSERT INTO meta VALUES ('schema_version', $1) ON CONFLICT (key) DO UPDATE"
                               " SET value = excluded.value", str(SCHEMA_VERSION))

    # Accounts and tokens

    async def account_for_token(self, token: str) -> dict | None:
        """The active account of a valid token (not revoked, not expired), or None."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT a.id, a.name, a.role, t.id AS token_id FROM tokens t JOIN accounts a ON a.id = t.account_id"
                " WHERE t.token_hash = $1 AND t.revoked IS NULL AND (t.expires IS NULL OR t.expires > now())"
                " AND a.active", token_hash(token))
            if row is None:
                return None
            # Not a write for each request: one time in a minute is sufficient.
            await conn.execute("UPDATE tokens SET last_used = now() WHERE id = $1"
                               " AND (last_used IS NULL OR last_used < now() - interval '1 minute')",
                               row["token_id"])
            return dict(row)

    async def create_account(self, name: str, role: str, note: str = "") -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("INSERT INTO accounts (name, role, note) VALUES ($1, $2, $3) RETURNING id",
                                       name, role, note)

    async def account(self, name: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM accounts WHERE name = $1", name)
            return dict(row) if row else None

    async def update_account(self, name: str, *, role: str | None = None, active: bool | None = None) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE accounts SET role = coalesce($2, role), active = coalesce($3, active) WHERE name = $1",
                name, role, active)
            return result.endswith(" 1")

    async def accounts(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT a.*, count(t.id) FILTER (WHERE t.revoked IS NULL) AS tokens, max(t.last_used) AS last_used"
                " FROM accounts a LEFT JOIN tokens t ON t.account_id = a.id GROUP BY a.id ORDER BY a.name")
            return [dict(r) for r in rows]

    async def create_token(self, account: str, description: str = "", days: int | None = None) -> tuple[int, str]:
        """Make a token for an account. Give its ID and the plain token."""
        token = TOKEN_PREFIX + secrets.token_hex(32)
        async with self.pool.acquire() as conn:
            token_id = await conn.fetchval(
                "INSERT INTO tokens (account_id, token_hash, description, expires)"
                " SELECT id, $2, $3, CASE WHEN $4::int IS NULL THEN NULL ELSE now() + make_interval(days => $4) END"
                " FROM accounts WHERE name = $1 RETURNING id",
                account, token_hash(token), description, days)
        if token_id is None:
            raise LookupError(f"no account {account}")
        return token_id, token

    async def revoke_token(self, token_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("UPDATE tokens SET revoked = now() WHERE id = $1 AND revoked IS NULL",
                                        token_id)
            return result.endswith(" 1")

    async def tokens(self, account: str | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT t.id, a.name AS account, t.description, t.created, t.expires, t.last_used, t.revoked"
                " FROM tokens t JOIN accounts a ON a.id = t.account_id WHERE $1::text IS NULL OR a.name = $1"
                " ORDER BY a.name, t.id", account)
            return [dict(r) for r in rows]

    # Files

    async def blob(self, sha256: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM blobs WHERE sha256 = $1", sha256)
            return dict(row) if row else None

    async def add_blob(self, sha256: str, size: int, media_type: str, account_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO blobs (sha256, size, media_type, account_id) VALUES ($1, $2, $3, $4)"
                               " ON CONFLICT (sha256) DO NOTHING", sha256, size, media_type, account_id)

    async def remove_blob(self, sha256: str) -> bool:
        async with self.pool.acquire() as conn:
            return (await conn.execute("DELETE FROM blobs WHERE sha256 = $1", sha256)).endswith(" 1")

    async def blob_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT count(*) AS files, coalesce(sum(size), 0) AS bytes FROM blobs")
            return dict(row)

    async def add_upload(self, upload_id: str, s3_upload: str, sha256: str, size: int, media_type: str,
                         part_size: int, account_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO uploads (id, s3_upload, sha256, size, media_type, part_size, account_id)"
                               " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                               upload_id, s3_upload, sha256, size, media_type, part_size, account_id)

    async def upload(self, upload_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM uploads WHERE id = $1", upload_id)
            return dict(row) if row else None

    async def remove_upload(self, upload_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM uploads WHERE id = $1", upload_id)

    async def upload_ids(self) -> set[str]:
        async with self.pool.acquire() as conn:
            return {r["id"] for r in await conn.fetch("SELECT id FROM uploads")}

    async def stale_uploads(self, hours: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM uploads WHERE started < now() - make_interval(hours => $1)",
                                    hours)
            return [dict(r) for r in rows]
