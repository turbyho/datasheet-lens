"""One lock for pdfium in a process.

pdfium is not thread-safe: two threads that use it at the same time can crash
the process (the MCP server runs each tool call in a worker thread). Each
function that opens a PDF with pypdfium2 holds this lock until it closes the
PDF. The element map with docling runs in a separate process, because docling
uses pdfium inside and this lock cannot reach it.
"""

from __future__ import annotations

import functools
import threading

PDFIUM = threading.RLock()


def pdfium_locked(func):
    """Hold the pdfium lock while func runs."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with PDFIUM:
            return func(*args, **kwargs)

    return wrapper
