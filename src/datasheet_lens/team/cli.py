"""dslens-server: install and run the team server.

setup: the wizard for a new server (PostgreSQL, Garage, nginx, TLS, systemd).
run:   the API (the systemd unit runs this command).
check: a test of the settings (database, object store).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..errors import LensError
from .config import load, load_for_admin

DEFAULT_PORT = 8100  # 8000: the cache server of fw-context


def _run(args) -> int:
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(load()), host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips, log_level=args.log_level)
    return 0


async def _check_async(settings) -> list[str]:
    from .blobs import BlobStore
    from .db import Database

    lines = []
    db = Database(settings.db_url, max_size=1)
    await db.connect()
    try:
        await db.init_schema()
        lines.append(f"database: ok, {len(await db.accounts())} accounts")
    finally:
        await db.close()
    blobs = BlobStore(settings)
    blobs.s3.head_bucket(Bucket=settings.s3_bucket)
    lines.append(f"object store: ok, bucket {settings.s3_bucket} at {settings.s3_endpoint}")
    lines.append(f"presigned URLs: {settings.s3_public_url}")
    return lines


def _check(args) -> int:
    for line in asyncio.run(_check_async(load_for_admin())):
        print(line)
    return 0


def _setup(args) -> int:
    from .setup import wizard

    return wizard(args)


def _test(args) -> int:
    import getpass
    import os

    from .setup import public_test

    # Not an argument: a token on the command line is in ps and in the shell history.
    token = os.environ.get("DSLENS_TOKEN") or getpass.getpass("Token (role contributor or higher): ")
    sha256 = public_test(args.url.rstrip("/"), token.strip())
    print(f"ok: upload, presigned PUT and GET through {args.url}")
    print(f"remove the test file on the server: sudo dslens-admin blob rm {sha256}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dslens-server", description="Install and run the team server of datasheet-lens.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="run the API (for the systemd unit)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--forwarded-allow-ips", default="127.0.0.1",
                   help="the proxies whose X-Forwarded-* headers the server uses")
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=_run)
    s = sub.add_parser("check", help="test the database and the object store")
    s.set_defaults(func=_check)
    s = sub.add_parser("setup", help="install the server on this machine (asks before each step)")
    s.add_argument("-y", "--yes", action="store_true", help="the default answer to each question")
    s.add_argument("-w", "--wheel", help="install datasheet-lens from this wheel (default: PyPI, same version)")
    s.add_argument("--api", help="the domain of the API (for example dslens.example.com)")
    s.add_argument("--s3", help="the domain of the S3 endpoint (default: s3.API)")
    s.add_argument("--email", help="the e-mail for Let's Encrypt")
    s.add_argument("--capacity", help="the capacity of Garage (for example 50G)")
    s.add_argument("--admin", default="admin", help="the name of the first admin account")
    s.set_defaults(func=_setup)
    s = sub.add_parser("test", help="test a server through its public URLs (upload and download of a test file); "
                                    "the token comes from DSLENS_TOKEN, or the command asks for it")
    s.add_argument("url", help="the URL of the API (https://dslens.example.com)")
    s.set_defaults(func=_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except LensError as e:
        print(f"dslens-server: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
