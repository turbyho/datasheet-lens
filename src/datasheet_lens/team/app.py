"""The HTTP API of the team server (FastAPI).

Files: a client asks for an upload with the SHA-256, the size and the media
type of the file. The server answers that it has the file, or gives a
presigned URL. The client sends the file directly to the object store, then
tells the server (done). For a slow or unstable connection, the client uses a
multipart upload: it can continue after a stop (GET of the upload gives the
parts that the object store has).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .auth import AuthMiddleware, require
from .blobs import BlobError, BlobStore, WrongContent, blob_key, staging_key
from .config import Settings
from ..bundles import BACKEND_RANK
from ..makers import SEED_VERSION
from . import sync
from .db import Database

log = logging.getLogger("dslens.server")
MEDIA_TYPES = ("application/pdf", "application/gzip")  # a document; a bundle of derived data
MAX_BODY = 1 << 20       # bytes of a request to the API (the files go to the object store)
STALE_HOURS = 24         # a multipart upload with no completion after this time is removed
ORPHAN_HOURS = 1         # an upload or staging file with no row: removed after this time
CLEANUP_SECONDS = 3600
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

try:
    VERSION = version("datasheet-lens")
except PackageNotFoundError:
    VERSION = "unknown"


class NotFound(Exception):
    """A file or an upload that the server does not have (HTTP 404)."""


def _sha(value: str) -> str:
    if not SHA_RE.match(value):
        raise ValueError("sha256 must be 64 lowercase hex digits")
    return value


class FileInfo(BaseModel):
    sha256: str
    size: int = Field(gt=0)
    media_type: str

    @field_validator("sha256")
    @classmethod
    def _check_sha(cls, v: str) -> str:
        return _sha(v)

    @field_validator("media_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in MEDIA_TYPES:
            raise ValueError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
        return v


class PartRequest(BaseModel):
    number: int = Field(ge=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _check_sha(cls, v: str) -> str:
        return _sha(v)


class PartUrls(BaseModel):
    parts: list[PartRequest] = Field(min_length=1, max_length=1000)


class Push(BaseModel):
    changes: list[dict] = Field(min_length=1, max_length=sync.MAX_PUSH)


class Bundle(BaseModel):
    document_sha: str
    kind: str
    format: str = Field(min_length=1, max_length=20)
    backend: str
    blob_sha: str
    versions: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("document_sha", "blob_sha")
    @classmethod
    def _check_sha(cls, v: str) -> str:
        return _sha(v)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in sync.BUNDLE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(sync.BUNDLE_KINDS)}")
        return v

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, v: str) -> str:
        if v not in BACKEND_RANK:  # the kind and its backends: register_bundle
            raise ValueError(f"backend must be one of {', '.join(BACKEND_RANK)}")
        return v


async def cleanup_uploads(db: Database, blobs: BlobStore) -> int:
    """Remove the multipart uploads with no completion after STALE_HOURS, and
    the uploads and staging files of the object store that the database does not
    know (after ORPHAN_HOURS). Give the number."""
    removed = 0
    for upload in await db.stale_uploads(STALE_HOURS):
        await run_in_threadpool(blobs.abort, upload["id"], upload["s3_upload"])
        await db.remove_upload(upload["id"])
        removed += 1
    # The known IDs come after the time limit: an upload that starts now is
    # newer than the limit, also when its row is not there yet.
    limit = datetime.now(timezone.utc) - timedelta(hours=ORPHAN_HOURS)
    known = await db.upload_ids()
    for upload_id, s3_upload in await run_in_threadpool(blobs.orphans, known, limit):
        if s3_upload:
            await run_in_threadpool(blobs.abort, upload_id, s3_upload)
        else:
            await run_in_threadpool(blobs.delete, staging_key(upload_id))
        removed += 1
    return removed


def create_app(settings: Settings, *, db: Database | None = None, blobs: BlobStore | None = None,
               cleanup: bool = True) -> FastAPI:
    db = db or Database(settings.db_url)
    blobs = blobs or BlobStore(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        await db.init_schema()
        task = None
        if cleanup:
            async def run_cleanup():
                while True:
                    try:
                        if count := await cleanup_uploads(db, blobs):
                            log.info("removed %d multipart uploads with no completion", count)
                    except Exception:  # the next run tries again; the API stays up
                        log.exception("cleanup of the multipart uploads failed")
                    await asyncio.sleep(CLEANUP_SECONDS)
            task = asyncio.create_task(run_cleanup())
        try:
            yield
        finally:
            if task:
                task.cancel()
            await db.close()

    app = FastAPI(title="datasheet-lens team server", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.blobs, app.state.settings = db, blobs, settings
    app.add_middleware(AuthMiddleware)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is None and request.method in ("POST", "PUT", "PATCH", "DELETE") and (
                "transfer-encoding" in request.headers):
            return JSONResponse({"detail": "a request with a body needs a Content-Length"}, status_code=411)
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return JSONResponse({"detail": f"the request body must be at most {MAX_BODY} bytes"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(BlobError)
    async def bad_file(request: Request, exc: BlobError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def failure(request: Request, exc: Exception):
        log.exception("request %s %s failed", request.method, request.url.path)
        return JSONResponse({"detail": "internal error of the server (see its log)"}, status_code=500)

    def check_size(size: int) -> None:
        if size > settings.max_blob_size:
            raise BlobError(f"the file has {size} bytes; the server takes at most {settings.max_blob_size}")

    async def known(info: FileInfo, account: dict) -> bool:
        """True when the server has the file. A file that the object store has
        with no row (the client did not tell done) gets its row now."""
        if await db.blob(info.sha256):
            return True
        if await run_in_threadpool(blobs.size, blob_key(info.sha256)) == info.size:
            await db.add_blob(info.sha256, info.size, info.media_type, account["id"])
            return True
        return False

    async def own_upload(request: Request, upload_id: str) -> dict:
        account = require(request, "contributor")
        upload = await db.upload(upload_id)
        if upload is None:
            raise NotFound(f"no upload {upload_id}")
        if upload["account_id"] != account["id"] and account["role"] != "admin":
            raise PermissionError("the upload belongs to a different account")
        return upload

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": VERSION}

    @app.get("/v1/account")
    async def whoami(request: Request):
        account = require(request, "reader")
        # The version of the built-in manufacturer names: the clients compare it
        # with their own (the same registry on the server and the clients).
        return {"name": account["name"], "role": account["role"], "server_version": VERSION,
                "makers_version": SEED_VERSION, "features": ["sync", "bundles"]}

    @app.post("/v1/blobs")
    async def blob_upload(request: Request, info: FileInfo):
        account = require(request, "contributor")
        if await known(info, account):
            return {"exists": True}
        check_size(info.size)
        return {"exists": False, **blobs.put_url(info.sha256, info.size)}

    @app.post("/v1/blobs/{sha256}/done")
    async def blob_done(request: Request, sha256: str, info: FileInfo):
        account = require(request, "contributor")
        if sha256 != info.sha256:
            raise BlobError("the SHA-256 of the path and of the body are not the same")
        size = await run_in_threadpool(blobs.size, blob_key(sha256))
        if size is None:
            raise NotFound(f"the object store does not have the file {sha256}")
        if size != info.size or size > settings.max_blob_size:
            await run_in_threadpool(blobs.delete, blob_key(sha256))
            raise BlobError(f"the stored file has {size} bytes, not {info.size} (at most {settings.max_blob_size}):"
                            " it is removed")
        await db.add_blob(sha256, size, info.media_type, account["id"])
        return {"stored": True}

    @app.get("/v1/blobs/{sha256}")
    async def blob_download(request: Request, sha256: str):
        require(request, "reader")
        row = await db.blob(sha256) if SHA_RE.match(sha256) else None
        if row is None:
            raise NotFound(f"the server does not have the file {sha256}")
        return {"url": blobs.get_url(sha256), "size": row["size"], "media_type": row["media_type"],
                "expires_in": settings.url_expiry}

    @app.post("/v1/uploads")
    async def upload_start(request: Request, info: FileInfo):
        account = require(request, "contributor")
        if await known(info, account):
            return {"exists": True}
        check_size(info.size)
        upload_id = uuid.uuid4().hex
        part_size = blobs.part_size_for(info.size)
        s3_upload = await run_in_threadpool(blobs.start, upload_id)
        await db.add_upload(upload_id, s3_upload, info.sha256, info.size, info.media_type, part_size, account["id"])
        return {"exists": False, "upload_id": upload_id, "part_size": part_size,
                "parts": blobs.part_count(info.size, part_size)}

    @app.post("/v1/uploads/{upload_id}/urls")
    async def upload_urls(request: Request, upload_id: str, body: PartUrls):
        upload = await own_upload(request, upload_id)
        count = blobs.part_count(upload["size"], upload["part_size"])
        if any(p.number > count for p in body.parts):
            raise BlobError(f"the upload has the parts 1 to {count}")
        return {"parts": [blobs.part_url(upload_id, upload["s3_upload"], p.number, p.sha256,
                                         blobs.part_length(upload["size"], upload["part_size"], p.number))
                          for p in body.parts]}

    @app.get("/v1/uploads/{upload_id}")
    async def upload_state(request: Request, upload_id: str):
        upload = await own_upload(request, upload_id)
        parts = await run_in_threadpool(blobs.parts, upload_id, upload["s3_upload"])
        # No multipart upload any more: it was completed, and the copy did not end.
        # The client calls complete again.
        return {"sha256": upload["sha256"], "size": upload["size"], "part_size": upload["part_size"],
                "parts": blobs.part_count(upload["size"], upload["part_size"]), "completed": parts is None,
                "uploaded": [{"number": p["number"], "size": p["size"]} for p in parts or []]}

    @app.post("/v1/uploads/{upload_id}/complete")
    async def upload_complete(request: Request, upload_id: str):
        upload = await own_upload(request, upload_id)
        try:
            await run_in_threadpool(blobs.complete, upload_id, upload["s3_upload"], upload["sha256"],
                                    upload["size"], upload["part_size"])
        except WrongContent:  # the staging file is removed: the upload ends
            await db.remove_upload(upload_id)
            raise
        await db.add_blob(upload["sha256"], upload["size"], upload["media_type"], upload["account_id"])
        await db.remove_upload(upload_id)
        return {"stored": True}

    @app.delete("/v1/uploads/{upload_id}")
    async def upload_abort(request: Request, upload_id: str):
        upload = await own_upload(request, upload_id)
        await run_in_threadpool(blobs.abort, upload_id, upload["s3_upload"])
        await db.remove_upload(upload_id)
        return {"aborted": True}

    @app.get("/v1/sync/pull")
    async def sync_pull(request: Request, cursor: str | None = None, after: str | None = None):
        require(request, "reader")
        if cursor is not None and not (cursor.isascii() and cursor.isdigit()):
            raise HTTPException(422, "cursor must be a number")
        try:
            position = sync.check_after(json.loads(after)) if after else None
            return await sync.pull(db.pool, int(cursor) if cursor is not None else None, position)
        except (sync.BadPosition, json.JSONDecodeError) as e:
            raise HTTPException(422, f"after must be the value of the page before: {e}") from None

    @app.post("/v1/bundles")
    async def bundle_register(request: Request, body: Bundle):
        account = require(request, "contributor")
        try:
            return {"canonical": await sync.register_bundle(db.pool, account, body.model_dump())}
        except sync.Rejected as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/v1/sync/push")
    async def sync_push(request: Request, body: Push):
        account = require(request, "reader")
        return {"results": await sync.push(db.pool, account, body.changes)}

    return app
