"""Location of the central store."""

import os
from pathlib import Path

ENV_HOME = "DATASHEET_LENS_HOME"


def store_home() -> Path:
    """Give the directory of the central store: the env var DATASHEET_LENS_HOME,
    or ~/.datasheet-lens."""
    if os.environ.get(ENV_HOME):
        return Path(os.environ[ENV_HOME]).expanduser()
    return Path.home() / ".datasheet-lens"
