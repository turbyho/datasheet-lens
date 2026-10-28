"""Location of the central store."""

import os
from pathlib import Path

ENV_HOME = "DATASHEET_LENS_HOME"


def store_home() -> Path:
    """Give the directory of the central store.

    The first of these that is set: the env var DATASHEET_LENS_HOME,
    $XDG_DATA_HOME/datasheet-lens, ~/.local/share/datasheet-lens.
    """
    if os.environ.get(ENV_HOME):
        return Path(os.environ[ENV_HOME]).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "datasheet-lens"
