"""Download of a document from a URL into the store.

datasheet-lens does not search the web: the agent gives the URL (from a part
registry, a distributor or the Datasheet field of a CAD symbol). Known problems
of the sources:

- A server can return an HTML page (a login, a captcha, a block), not a PDF.
  The file must start with %PDF (only blank characters can come before it).
- Some servers block a request that has no browser User-Agent.
- Some servers block each script request (for example Mouser /pdfdocs/). Then
  the user downloads the file and puts it into the folder of the project.
"""

from __future__ import annotations

import contextlib
import email.utils
import http.client
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from .errors import LensError
from .store import Document, Store

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
MAX_BYTES = 300 * 1024 * 1024
TIMEOUT_S = 60  # with no data for each network operation (connect, each read)
# No limit for the full download time: a slow connection must work. A download
# stops only when its mean rate after MIN_RATE_AFTER_S is below MIN_RATE (a
# server that sends almost nothing).
MIN_RATE = 1024  # bytes per second
MIN_RATE_AFTER_S = 120
MAX_NAME = 120
MANUAL = "download the file by hand, put it into the folder of the project and run project sync"


def is_url(text: str) -> bool:
    """True for an http or https URL."""
    return bool(re.match(r"(?i)https?://", text.strip()))


def _quote(url: str) -> str:
    """Encode the characters that a URL cannot hold (a space in a URL that was
    copied from a CAD field)."""
    return urllib.parse.quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~")


def _filename(url: str, headers=None) -> str:
    """A file name for the download: from Content-Disposition (filename* first),
    else from the URL. It always ends with .pdf."""
    name = ""
    if headers is not None:
        params = [v for k, v in headers.get_params([], header="content-disposition") if k == "filename"]
        # An RFC 2231 value (filename*) is a tuple: RFC 6266 prefers it.
        encoded = [v for v in params if isinstance(v, tuple)]
        name = email.utils.collapse_rfc2231_value(encoded[0]) if encoded else next(iter(params), "")
    if not name:
        name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "document"
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return stem[:MAX_NAME] + ".pdf"


def _is_pdf(head: bytes) -> bool:
    """True if the data starts with %PDF (a BOM and blank characters can come first)."""
    return head.lstrip(b"\xef\xbb\xbf \t\r\n\x00").startswith(b"%PDF")


def download(url: str, folder: Path) -> Path:
    """Download a PDF into folder. Give its path. A LensError tells why the
    download failed, with the URL. An error of the local disk is an OSError."""
    if not is_url(url):
        raise LensError(f"not an http or https URL: {url}")
    url = _quote(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    start = time.monotonic()
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        e.close()  # the error holds the open response
        raise LensError(f"{url}: HTTP {e.code} {e.reason}: {MANUAL}") from e
    except (urllib.error.URLError, http.client.HTTPException, ValueError, TimeoutError, ConnectionError) as e:
        raise LensError(f"{url}: the download failed ({getattr(e, 'reason', e)}): {MANUAL}") from e
    with response:
        length = response.headers.get("Content-Length", "")
        if length.isdigit() and int(length) > MAX_BYTES:
            raise LensError(f"{url}: the file is larger than {MAX_BYTES >> 20} MB")
        target = folder / _filename(response.geturl(), response.headers)
        with open(target, "wb") as f:
            size = 0
            while True:
                try:
                    block = response.read(1 << 16)
                except (http.client.HTTPException, TimeoutError, ConnectionError) as e:
                    raise LensError(f"{url}: the download stopped ({e}): {MANUAL}") from e
                if size == 0 and not _is_pdf(block[:1024]):
                    kind = response.headers.get("Content-Type", "unknown type")
                    raise LensError(f"{url} did not give a PDF ({kind}; for example a login, a captcha or a "
                                    f"block page): {MANUAL}")
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise LensError(f"{url}: the file is larger than {MAX_BYTES >> 20} MB")
                elapsed = time.monotonic() - start
                if elapsed > MIN_RATE_AFTER_S and size / elapsed < MIN_RATE:
                    raise LensError(f"{url}: the server sends less than {MIN_RATE} bytes/s "
                                    f"({size} bytes in {elapsed:.0f} s): {MANUAL}")
                f.write(block)
    return target


@contextlib.contextmanager
def downloaded(url: str, store_home: Path) -> Iterator[Path]:
    """Download a PDF into a temporary folder of the store; remove it after use."""
    tmp = store_home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="download-", dir=tmp))
    try:
        yield download(url, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def add_from_url(store: Store, url: str, title: str = "") -> Document:
    """Download a PDF and add it to the store, with its source URL."""
    with downloaded(url, store.home) as path:
        return store.add_document(path, title=title, source_url=url.strip(), move=True)
