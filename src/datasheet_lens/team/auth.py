"""The authentication of the team server: a bearer token of an account.

The middleware finds the account of the token for each request (except
/health) and puts it into request.state.account. require() compares its role.
After MAX_FAILURES failed tokens in WINDOW seconds from one address, that
address gets 429 (the rate limit of nginx comes before this one).

The address is request.client: behind nginx, uvicorn sets it from
X-Forwarded-For, only for a connection from --forwarded-allow-ips, and it takes
the address that the trusted proxy added (not one that the client wrote).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import ROLES

PUBLIC_PATHS = ("/health",)
MAX_FAILURES, WINDOW = 20, 60.0
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    return token if 0 < len(token) <= 256 else None  # no hash work for a very long value


def client_address(request: Request) -> str:
    return request.client.host if request.client else ""


def _recent(now: float) -> None:
    """Keep only the failures of the last WINDOW seconds (also no growth with time)."""
    for address in list(_failures):
        times = [t for t in _failures[address] if now - t < WINDOW]
        if times:
            _failures[address] = times
        else:
            del _failures[address]


def _limited(address: str) -> bool:
    """True when the address had MAX_FAILURES failed tokens in the last WINDOW seconds."""
    with _lock:
        _recent(time.monotonic())
        return len(_failures.get(address, ())) >= MAX_FAILURES


def _failed(address: str) -> None:
    with _lock:
        _failures.setdefault(address, []).append(time.monotonic())


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "a bearer token is necessary"}, status_code=401)
        address = client_address(request)
        if _limited(address):
            return JSONResponse({"detail": "too many failed tokens: wait one minute"}, status_code=429)
        account = await request.app.state.db.account_for_token(token)
        if account is None:
            _failed(address)
            return JSONResponse({"detail": "the token is not valid (unknown, revoked or expired)"},
                                status_code=401)
        request.state.account = account
        return await call_next(request)


def require(request: Request, role: str) -> dict:
    """The account of the request when its role is role or a later role of
    ROLES; else a PermissionError."""
    account = request.state.account
    if ROLES.index(account["role"]) < ROLES.index(role):
        raise PermissionError(f"the account {account['name']} has the role {account['role']}: "
                              f"this needs the role {role}")
    return account
