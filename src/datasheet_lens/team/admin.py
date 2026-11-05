"""dslens-admin: the accounts and tokens of the team server.

Run it on the server. It connects directly to the database: the access to the
settings file (sudo) is the permission. A token is shown one time only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..errors import LensError
from .config import ROLES, load_for_admin
from .db import Database


def _date(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


async def _account_add(db: Database, args) -> int:
    if await db.account(args.name):
        raise LensError(f"the account {args.name} exists")
    await db.create_account(args.name, args.role, args.note)
    print(f"account {args.name} ({args.role})")
    if not args.no_token:
        return await _token_add(db, argparse.Namespace(account=args.name, description=args.token_description,
                                                       days=args.days))
    return 0


async def _account_ls(db: Database, args) -> int:
    for a in await db.accounts():
        state = "" if a["active"] else "  DISABLED"
        print(f"{a['name']:24} {a['role']:12} tokens {a['tokens']:<3} last use {_date(a['last_used'])}"
              f"{state}{'  ' + a['note'] if a['note'] else ''}")
    return 0


async def _account_set(db: Database, args) -> int:
    active = None if args.active is None else args.active == "on"
    if args.role is None and active is None:
        raise LensError("give --role, or --active on|off")
    if not await db.update_account(args.name, role=args.role, active=active):
        raise LensError(f"no account {args.name}")
    account = await db.account(args.name)
    print(f"account {account['name']}: {account['role']}, {'active' if account['active'] else 'disabled'}")
    return 0


async def _token_add(db: Database, args) -> int:
    try:
        token_id, token = await db.create_token(args.account, args.description, args.days)
    except LookupError as e:
        raise LensError(str(e)) from None
    print(f"token {token_id} of {args.account}{f' (expires in {args.days} days)' if args.days else ''}:")
    print(f"  {token}")
    print("Give it to the person through a safe channel. The server cannot show it again.")
    return 0


async def _token_ls(db: Database, args) -> int:
    for t in await db.tokens(args.account):
        state = f"revoked {_date(t['revoked'])}" if t["revoked"] else f"expires {_date(t['expires'])}"
        print(f"{t['id']:>5} {t['account']:24} created {_date(t['created'])}  last use {_date(t['last_used'])}"
              f"  {state}  {t['description']}")
    return 0


async def _token_rm(db: Database, args) -> int:
    if not await db.revoke_token(args.id):
        raise LensError(f"no active token {args.id}")
    print(f"token {args.id} revoked")
    return 0


async def _stats(db: Database, args) -> int:
    files = await db.blob_stats()
    accounts = await db.accounts()
    print(f"accounts {len(accounts)} ({sum(a['active'] for a in accounts)} active)")
    print(f"files {files['files']}, {files['bytes'] / (1 << 20):.1f} MiB")
    return 0


async def _blob_rm(db: Database, args) -> int:
    from .blobs import BlobStore, blob_key

    if not await db.remove_blob(args.sha256):
        raise LensError(f"no file {args.sha256}")
    BlobStore(args.settings).delete(blob_key(args.sha256))
    print(f"file {args.sha256} removed")
    return 0


async def _cleanup(db: Database, args) -> int:
    from .app import cleanup_uploads
    from .blobs import BlobStore

    print(f"removed {await cleanup_uploads(db, BlobStore(args.settings))} multipart uploads with no completion")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dslens-admin", description="The accounts and tokens of the team server.")
    sub = p.add_subparsers(dest="group", required=True)

    account = sub.add_parser("account", help="the accounts").add_subparsers(dest="cmd", required=True)
    a = account.add_parser("add", help="make an account, and its first token")
    a.add_argument("name")
    a.add_argument("-r", "--role", choices=ROLES, default="contributor")
    a.add_argument("-n", "--note", default="", help="a note (for example the name of the person)")
    a.add_argument("-d", "--token-description", default="", help="the description of the first token")
    a.add_argument("--days", type=int, help="the token expires after this number of days")
    a.add_argument("--no-token", action="store_true", help="make no token")
    a.set_defaults(func=_account_add)
    a = account.add_parser("ls", help="show the accounts")
    a.set_defaults(func=_account_ls)
    a = account.add_parser("set", help="change the role of an account, or disable it")
    a.add_argument("name")
    a.add_argument("-r", "--role", choices=ROLES)
    a.add_argument("--active", choices=("on", "off"))
    a.set_defaults(func=_account_set)

    token = sub.add_parser("token", help="the tokens").add_subparsers(dest="cmd", required=True)
    t = token.add_parser("add", help="make a token for an account (one for each computer)")
    t.add_argument("account")
    t.add_argument("-d", "--description", default="", help="for example the computer")
    t.add_argument("--days", type=int, help="the token expires after this number of days")
    t.set_defaults(func=_token_add)
    t = token.add_parser("ls", help="show the tokens")
    t.add_argument("account", nargs="?")
    t.set_defaults(func=_token_ls)
    t = token.add_parser("rm", help="revoke a token")
    t.add_argument("id", type=int)
    t.set_defaults(func=_token_rm)

    blob = sub.add_parser("blob", help="the files").add_subparsers(dest="cmd", required=True)
    b = blob.add_parser("rm", help="remove a file from the server (for example a wrong or test file)")
    b.add_argument("sha256")
    b.set_defaults(func=_blob_rm)

    sub.add_parser("stats", help="the numbers of the accounts and files").set_defaults(func=_stats)
    sub.add_parser("cleanup", help="remove the multipart uploads with no completion").set_defaults(func=_cleanup)
    return p


async def _run(args) -> int:
    db = Database(args.settings.db_url, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        return await args.func(db, args)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.settings = load_for_admin()
        return asyncio.run(_run(args))
    except LensError as e:
        print(f"dslens-admin: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
