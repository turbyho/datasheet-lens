"""Initialization: the store, and the optional docling component."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys

from .config import store_home
from .elements import docling_available
from .errors import LensError
from .fileutil import write_atomic

DOCLING_SPEC = "docling>=2.144"

DOCLING_TEXT = """\
Optional component: docling (layout model for the element map)

With docling, the element map holds the regions of the tables, figures and
formulas of each page. The model then asks for an exact region image, not for a
full page. Result in the tests: the same correct answers, with no full-page
images (9 image requests, not 15).

Without docling, the element map holds the headings, text blocks and captions
from the text layer. The tool works, but the model reads more full pages.

Cost: docling installs PyTorch and models (some GB of disk). It runs on a
CPU (about 0.3 s for each page with 24 cores) or a GPU (CUDA, Apple MPS).
A team server can build the element map one time for all users.
"""


def config_path():
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


def save_config(cfg: dict) -> None:
    """Write the configuration."""
    write_atomic(config_path(), (json.dumps(cfg, indent=1) + "\n").encode("utf-8"))


def element_backend() -> str:
    """The configured element backend: "docling" (if installed) or "text"."""
    choice = load_config().get("element_backend", "auto")
    if choice == "docling" and not docling_available():
        return "text"
    if choice == "auto":
        return "docling" if docling_available() else "text"
    return choice


def install_command() -> list[str] | None:
    """The command that installs docling into the Python of this tool: pip of this
    Python, or uv (a uv environment has no pip), or None."""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", DOCLING_SPEC]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, DOCLING_SPEC]
    return None


def init(layout: str = "ask", out=print, ask=input) -> int:
    """layout: "ask", "yes" or "no". Gives the exit code."""
    home = store_home()
    home.mkdir(parents=True, exist_ok=True)
    out(f"store: {home}")
    cfg = load_config()
    if docling_available():
        out("docling: installed; the element map uses the layout model.")
        cfg["element_backend"] = "docling"
    else:
        if layout == "ask":
            out(DOCLING_TEXT)
            if not sys.stdin.isatty():
                out("Not an interactive terminal: docling is not installed. Use --layout yes to install it.")
                layout = "no"
            else:
                layout = "yes" if ask("Install docling now? [y/N] ").strip().lower() in ("y", "yes") else "no"
        if layout == "yes":
            command = install_command()
            if command is None:
                out("No pip and no uv for this Python. Install the extra yourself, for example:\n"
                    "  pip install 'datasheet-lens[layout]'\n"
                    "  uv tool install --force 'datasheet-lens[layout]'")
                cfg["element_backend"] = "text"
                save_config(cfg)
                return 1
            out(f"Installing {DOCLING_SPEC}: {' '.join(command)}")
            result = subprocess.run(command)
            if result.returncode != 0:
                out("The installation failed. The element map uses the text layer.")
                cfg["element_backend"] = "text"
                save_config(cfg)
                return 1
            cfg["element_backend"] = "docling"
            out("docling: installed.")
        else:
            cfg["element_backend"] = "text"
            out("docling: not installed; the element map uses the text layer. "
                "To install it later: datasheet-lens init --layout yes")
    save_config(cfg)
    return 0
