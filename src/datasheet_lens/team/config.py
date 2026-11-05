"""The settings of the team server: environment variables. The systemd unit
reads them from ENV_FILE (only root and the server user can read it)."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..errors import LensError

ENV_FILE = Path("/var/lib/dslens-server/server.env")
ROLES = ("reader", "contributor", "reviewer", "admin")  # each role has the permissions of the roles before it

_REQUIRED = {
    "db_url": "DSLENS_DB_URL",              # postgresql://user:password@host:port/database
    "s3_endpoint": "DSLENS_S3_ENDPOINT",    # the object store for the server (http://127.0.0.1:3900)
    "s3_public_url": "DSLENS_S3_PUBLIC_URL",  # the object store for the clients (presigned URLs)
    "s3_key_id": "DSLENS_S3_KEY_ID",
    "s3_secret": "DSLENS_S3_SECRET",
}
_OPTIONAL = {
    "s3_region": ("DSLENS_S3_REGION", "garage"),
    "s3_bucket": ("DSLENS_S3_BUCKET", "dslens"),
    "max_blob_size": ("DSLENS_MAX_BLOB_SIZE", str(2 << 30)),  # bytes; S3 CopyObject takes at most 5 GiB
    "url_expiry": ("DSLENS_URL_EXPIRY", "3600"),              # seconds of a presigned URL
}


@dataclass(frozen=True)
class Settings:
    db_url: str
    s3_endpoint: str
    s3_public_url: str
    s3_key_id: str
    s3_secret: str
    s3_region: str = "garage"
    s3_bucket: str = "dslens"
    max_blob_size: int = 2 << 30
    url_expiry: int = 3600


def read_env_file(path: Path) -> dict[str, str]:
    """The variables of an env file (KEY=value, a value can have shell quotes)."""
    return parse_env(path.read_text(encoding="utf-8"))


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        parts = shlex.split(value, comments=False)
        values[name.strip()] = parts[0] if parts else ""
    return values


def load(env: dict[str, str] | None = None) -> Settings:
    """The settings from env (default: os.environ). A missing variable gives a
    LensError with its name."""
    env = os.environ if env is None else env
    missing = [name for name in _REQUIRED.values() if not env.get(name)]
    if missing:
        raise LensError(f"the team server needs the environment variables {', '.join(missing)} "
                        f"(see {ENV_FILE})")
    values: dict[str, object] = {field: env[name] for field, name in _REQUIRED.items()}
    for field, (name, default) in _OPTIONAL.items():
        values[field] = env.get(name) or default
    for field in ("max_blob_size", "url_expiry"):
        try:
            values[field] = int(values[field])
        except ValueError:
            raise LensError(f"{_OPTIONAL[field][0]} must be an integer: {values[field]}") from None
    return Settings(**values)


def load_for_admin() -> Settings:
    """The settings for a command on the server: the environment, else ENV_FILE."""
    if os.environ.get(_REQUIRED["db_url"]):
        return load()
    try:
        values = read_env_file(ENV_FILE)
    except FileNotFoundError:
        return load()
    except PermissionError:
        raise LensError(f"cannot read {ENV_FILE}: run the command with sudo") from None
    return load({**values, **os.environ})
