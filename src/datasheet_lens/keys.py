"""The personal API keys of the distributors.

The order of the sources of a key:
1. The environment variable (for example DATASHEET_LENS_MOUSER_KEY): for CI
   and for persons who want it.
2. The key store of the system (the package keyring): Windows Credential
   Manager, macOS Keychain, Secret Service on Linux.
3. The file keys.toml in the store folder, readable only by the user: for a
   system with no key store (a server with no desktop, a container).

The command "datasheet-lens key NAME" saves a key: in the key store of the
system, or in the file when the system has none. A key never goes into a
message, a log or the database.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
import tomllib
from pathlib import Path

from .config import store_home
from .errors import LensError

SERVICE = "datasheet-lens"
# The name of a key -> its environment variable.
KEYS = {"mouser": "DATASHEET_LENS_MOUSER_KEY"}
FILE = "keys.toml"


def _name(name: str) -> str:
    key = name.strip().lower()
    if key not in KEYS:
        raise LensError(f"unknown key {name!r}: {', '.join(KEYS)}")
    return key


def _keyring():
    """The keyring module with a usable backend, or None."""
    try:
        import keyring
        from keyring.backends import fail, null
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # a backend that does not load: as no key store
        return None
    # keyring gives the fail backend when the system has no key store.
    return None if isinstance(backend, (fail.Keyring, null.Keyring)) else keyring


def _file() -> Path:
    return store_home() / FILE


def _read_file(strict: bool = True) -> dict[str, str]:
    """The keys of the file. A file that cannot be read: a LensError, or {} when
    strict is false (a lookup then continues with no key)."""
    path = _file()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        if not strict:
            return {}
        raise LensError(f"{path}: cannot read the keys ({e}): correct or remove the file") from None
    return {k: str(v) for k, v in data.items() if isinstance(v, str)}


def _write_file(keys: dict[str, str]) -> None:
    """Write the file of the keys, readable and writable only by the user."""
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE}.{os.getpid()}.tmp")
    # Only the known names with a valid key: no text that makes the file invalid.
    text = "# API keys of datasheet-lens. Keep this file private.\n" + "".join(
        f'{k} = "{v}"\n' for k, v in sorted(keys.items()) if k in KEYS and KEY_RE.fullmatch(v))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _lookup(key: str) -> tuple[str, str]:
    """(the key or "", its source): the environment, the key store, the file."""
    env = KEYS[key]
    value = os.environ.get(env, "").strip()
    if value:
        return value, f"environment variable {env}"
    ring = _keyring()
    if ring is not None:
        value = _ring_get(ring, key)
        if value:
            return value, "the key store of the system"
    value = _read_file(strict=False).get(key, "").strip()
    return (value, str(_file())) if value else ("", "not set")


RING_TIMEOUT = 10.0


def _ring_get(ring, key: str) -> str:
    """The key from the key store, or "" when the key store gives an error or
    does not answer in RING_TIMEOUT seconds: a locked key store can wait for an
    unlock dialog, and the MCP server must not wait for it."""
    result: list[str] = []

    def get() -> None:
        with contextlib.suppress(Exception):
            result.append((ring.get_password(SERVICE, key) or "").strip())

    thread = threading.Thread(target=get, name="keyring-get", daemon=True)
    thread.start()
    thread.join(RING_TIMEOUT)
    return result[0] if result else ""


def get_key(name: str) -> str:
    """The key, or "": from the environment, the key store or the file."""
    return _lookup(_name(name))[0]


# The characters of an API key (a UUID, letters and digits). Not a control
# character: a terminal can add one to a pasted text.
KEY_RE = re.compile(r"[A-Za-z0-9._~+/=:-]+")


def set_key(name: str, value: str) -> str:
    """Save a key: in the key store of the system, else in the file. Gives where."""
    key = _name(name)
    value = value.strip()
    if not KEY_RE.fullmatch(value):
        raise LensError("the key is empty or has a character that an API key does not have (paste only the key)")
    ring = _keyring()
    if ring is not None:
        try:
            ring.set_password(SERVICE, key, value)
        except Exception:  # no usable key store: the file
            pass
        else:
            with contextlib.suppress(LensError, OSError):  # one place for a key; a key in the file is not used
                delete_file_key(key)
            return "the key store of the system"
    keys = _read_file()
    keys[key] = value
    _write_file(keys)
    return str(_file())


def delete_file_key(key: str) -> None:
    keys = _read_file()
    if keys.pop(key, None) is not None:
        _write_file(keys)


def delete_key(name: str) -> None:
    """Remove a saved key from the key store and from the file (not the
    environment variable)."""
    key = _name(name)
    ring = _keyring()
    if ring is not None:
        with contextlib.suppress(Exception):
            ring.delete_password(SERVICE, key)
    delete_file_key(key)


def key_sources() -> dict[str, str]:
    """For each key: where it is (environment, key store, file) or "not set". No values."""
    out = {key: _lookup(key)[1] for key in KEYS}
    try:
        _read_file()
    except LensError as e:
        out = {k: (f"{v} ({e})" if v == "not set" else v) for k, v in out.items()}
    return out


def key_source(name: str) -> str:
    """Where the key is (environment, key store, file), or "not set". No value."""
    return _lookup(_name(name))[1]
