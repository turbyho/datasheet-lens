"""Original pages for the model: a page image, a region image, a PDF of pages.

Coordinates are the same as in the index: pt, origin top left of the page as it is
shown (after /Rotate, the CropBox within the MediaBox). Rendered files are kept in
the store (render/), thus the same request is not rendered again.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from .errors import LensError
from .fileutil import temp_path, write_atomic
from .pdflock import pdfium_locked
from .pdftext import view_transform
from .store import Store

MAX_PX = 2576         # the maximum long edge of the current Claude models
MIN_MAX_PX, MAX_MAX_PX = 256, 8000
REGION_MIN_DPI = 300
MAX_DPI = 600         # a higher dpi adds no information (also for a small page)


@dataclass
class Image:
    """A rendered PNG file: path, page, region (None: a full page), dpi and size in pixels."""
    path: Path
    page: int
    dpi: float
    bbox: tuple[float, float, float, float] | None  # None: the full page
    width_px: int
    height_px: int


def _png(width: int, height: int, rgb: bytes, stride: int) -> bytes:
    """Encode 8-bit RGB rows as PNG (standard library only)."""
    raw = b"".join(b"\x00" + rgb[y * stride:y * stride + width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _png_size(path: Path) -> tuple[int, int]:
    """The width and the height of a PNG file, from its header."""
    with open(path, "rb") as f:
        return struct.unpack(">II", f.read(24)[16:24])


def _fit_dpi(long_pt: float, max_px: int) -> float:
    # pdfium rounds the bitmap size up; half a pixel of margin keeps it <= max_px.
    return (max_px - 0.5) * 72 / long_pt


def region_dpi(bbox: tuple[float, float, float, float], max_px: int = MAX_PX) -> float:
    """300 to 600 dpi, and the long edge within max_px. A region too large for
    300 dpi gets the dpi that fits."""
    fit = _fit_dpi(max(bbox[2] - bbox[0], bbox[3] - bbox[1]), max_px)
    return min(MAX_DPI, fit) if fit >= REGION_MIN_DPI else fit


def _check_bbox(bbox) -> tuple[float, float, float, float]:
    """Check a region: four finite numbers, x0 < x1 and y0 < y1, on the page."""
    if len(bbox) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise LensError(f"a region needs four finite numbers x0,y0,x1,y1 (pt): {bbox}")
    return tuple(float(v) for v in bbox)


def _page_size(store: Store, doc, page: int) -> tuple[float, float] | None:
    """The size of a page in pt as a viewer shows it (after /Rotate and the CropBox)."""
    row = store.db.execute("SELECT width_pt, height_pt FROM pages WHERE document_id = ? AND page = ?",
                           (doc.id, page)).fetchone()
    return (row["width_pt"], row["height_pt"]) if row else None


@pdfium_locked
def page_image(store: Store, doc_id: int, page: int, bbox=None, max_px: int = MAX_PX) -> Image:
    """Render a page, or a region of a page (bbox: x0, y0, x1, y1 in pt, origin at
    the top left), as a PNG file in the render cache of the store. A region gets
    300 to 600 dpi, a full page the dpi that fits max_px on its long edge. A file
    in the cache is used again."""
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    if not 1 <= page <= (doc.pages or 0):
        raise LensError(f"page {page} is not in the document (1 to {doc.pages})")
    if not MIN_MAX_PX <= max_px <= MAX_MAX_PX:
        raise LensError(f"max_px must be {MIN_MAX_PX} to {MAX_MAX_PX}: {max_px}")

    pdf = None
    size = _page_size(store, doc, page)  # from the index; no PDF open for a cache hit
    if size is None:
        pdf = pdfium.PdfDocument(str(doc.path))
        _box, w, h = view_transform(pdf[page - 1])
        size = (w, h)
    width, height = size
    try:
        if bbox is None:
            dpi = min(MAX_DPI, _fit_dpi(max(width, height), max_px))
        else:
            x0, y0, x1, y1 = _check_bbox(bbox)
            x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(width, x1), min(height, y1)
            if x1 - x0 < 1 or y1 - y0 < 1:
                raise LensError(f"the region {tuple(bbox)} is not on the page ({width:.0f} x {height:.0f} pt)")
            bbox = (x0, y0, x1, y1)
            dpi = region_dpi(bbox, max_px)
        key = "full" if bbox is None else "_".join(f"{v:.1f}" for v in bbox)
        out = store.home / "render" / doc.sha256[:16] / f"p{page}-{key}-{dpi:.1f}dpi.png"
        if out.exists():
            w_px, h_px = _png_size(out)
        else:
            if pdf is None:
                pdf = pdfium.PdfDocument(str(doc.path))
            # pdfium crop: the amount to cut from the left, bottom, right and top edge.
            crop = (0, 0, 0, 0) if bbox is None else (bbox[0], height - bbox[3], width - bbox[2], bbox[1])
            bitmap = pdf[page - 1].render(scale=dpi / 72, crop=crop, rev_byteorder=True)
            if bitmap.n_channels != 3:
                raise RuntimeError(f"unexpected bitmap with {bitmap.n_channels} channels")
            w_px, h_px = bitmap.width, bitmap.height
            write_atomic(out, _png(w_px, h_px, bytes(bitmap.buffer), bitmap.stride))
        return Image(out, page, round(dpi, 1), bbox, w_px, h_px)
    finally:
        if pdf is not None:
            pdf.close()


def parse_pages(spec: str, count: int) -> list[int]:
    """"7", "3-5,8": a list of page numbers, each one in 1..count."""
    pages = []
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if not m:
            raise LensError(f"not a page list: {spec!r} (use for example 7 or 3-5,8)")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a > b or a < 1 or b > count:
            raise LensError(f"pages {part} are not in the document (1 to {count})")
        pages.extend(range(a, b + 1))
    return pages


@pdfium_locked
def pdf_pages(store: Store, doc_id: int, spec: str) -> tuple[Path, list[int]]:
    """Write the pages to a new PDF in the store (render/), and give its path."""
    doc = store.document(doc_id)
    if doc is None:
        raise LensError(f"document not found: {doc_id}")
    pages = parse_pages(spec, doc.pages or 0)
    # The key is a hash of the page list: a long list must not give a long name.
    digest = hashlib.sha256(",".join(map(str, pages)).encode()).hexdigest()[:16]
    out = store.home / "render" / doc.sha256[:16] / f"pages-{pages[0]}-{len(pages)}p-{digest}.pdf"
    if not out.exists():
        src = pdfium.PdfDocument(str(doc.path))
        dst = pdfium.PdfDocument.new()
        tmp = temp_path(out)
        try:
            dst.import_pages(src, [p - 1 for p in pages])
            dst.save(str(tmp))
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
            dst.close()
            src.close()
    return out, pages
