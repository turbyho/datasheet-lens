"""Location of the central store, its configuration file and its data folder.

The store directory holds config.json, the API keys and the state of the update
check. The data folder holds db/, blobs/, render/ and tmp/. It is the store
directory, or the folder in the key "data" of config.json (a different disk).
"""

import json
import os
from pathlib import Path

from .errors import LensError

ENV_HOME = "DATASHEET_LENS_HOME"
DATA_KEY = "data"


def store_home() -> Path:
    """Give the directory of the central store: the env var DATASHEET_LENS_HOME,
    or ~/.datasheet-lens."""
    if os.environ.get(ENV_HOME):
        return Path(os.environ[ENV_HOME]).expanduser()
    return Path.home() / ".datasheet-lens"


def config_path() -> Path:
    """The configuration file of the store (config.json)."""
    return store_home() / "config.json"


def load_config(strict: bool = False) -> dict:
    """The configuration, or {} if there is none or it cannot be read. strict:
    a file that cannot be read gives a LensError (before a change of the file)."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            raise LensError(f"{path}: cannot read the configuration ({e}): correct or remove the file") from None
        return {}
    if not isinstance(data, dict):
        if strict:
            raise LensError(f"{path}: the configuration is not a JSON object: correct or remove the file")
        return {}
    return data


def data_home() -> Path:
    """Give the data folder of the store (db/, blobs/, render/, tmp/): the key
    "data" of config.json, or the store directory. A configuration that cannot
    be read, or a data folder that does not exist (a disk that is not mounted),
    gives a LensError: the data must not go to a different folder."""
    value = load_config(strict=True).get(DATA_KEY)
    if value is None:
        return store_home()
    if not isinstance(value, str) or not value.strip():
        raise LensError(f'{config_path()}: "{DATA_KEY}" must be the path of a folder')
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise LensError(f'{config_path()}: "{DATA_KEY}" must be an absolute path: {value}')
    if not path.is_dir():
        raise LensError(f'the data folder {path} ("{DATA_KEY}" in {config_path()}) does not exist: '
                        "mount its disk, or make the folder")
    return path


def _note(data: Path, home: Path) -> str | None:
    """The note of data_note for these two folders."""
    if data.resolve() == home.resolve() or not (home / "db" / "lens.db").exists():
        return None
    return (f"note: the store uses the data folder {data}, not the database in {home}. "
            f"Move db/, blobs/ and render/ from {home} to {data}, "
            f'or remove the key "{DATA_KEY}" from {config_path()}.')


def data_note() -> str | None:
    """A note when the key "data" names a different folder, but the store
    directory still has a database: the tool does not use that database."""
    return _note(data_home(), store_home())


def data_lines() -> list[str]:
    """The lines of the commands info and init: the data folder, if it is not
    the store directory, and the note of data_note."""
    data, home = data_home(), store_home()
    if data.resolve() == home.resolve():
        return []
    note = _note(data, home)
    return [f"data: {data}"] + ([note] if note else [])
