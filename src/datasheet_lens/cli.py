"""Command line interface of datasheet-lens."""

import argparse
import math
import sys
from pathlib import Path

import pypdfium2 as pdfium

from . import __version__
from .config import store_home
from .errors import LensError
from .store import Store

ROLES = ("datasheet", "manual", "errata", "appnote", "other")


def cmd_info(_args: argparse.Namespace) -> int:
    print(f"datasheet-lens {__version__}")
    print(f"store: {store_home()}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    with Store() as store:
        doc = store.add_document(Path(args.file), title=args.title or "", source_url=args.url or "")
        print(f"document {doc.id}: {doc.sha256[:12]} {doc.filename} ({doc.pages} pages)")
        if args.part:
            part_id = store.add_part(args.part, args.manufacturer or "")
            store.link(part_id, doc.id, args.role)
            print(f"part {args.part}: {args.role}")
    return 0


def _doc(store: Store, ref: str):
    doc = store.document(ref)
    if doc is None:
        raise LensError(f"document not found: {ref}")
    return doc


def cmd_index(args: argparse.Namespace) -> int:
    from .indexer import build_index

    with Store() as store:
        docs = [_doc(store, r) for r in args.documents] if args.documents else \
            [store.document(r["id"]) for r in store.documents()]
        for doc in docs:
            result = build_index(store, doc.id)
            print(f"{doc.sha256[:12]} {doc.filename}: {result['pages']} pages, "
                  f"{result['outline']} outline entries, {result['captions']} captions")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .indexer import index_markdown

    with Store() as store:
        print(index_markdown(store, _doc(store, args.document).id), end="")
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    from .indexer import outline_under

    with Store() as store:
        for o in outline_under(store, _doc(store, args.document).id, args.under):
            print(f"{'  ' * o['level']}- {o['title']} (p.{o['page']})")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .indexer import search

    with Store() as store:
        if args.part:
            ids = [r["id"] for r in store.part_documents(args.part)]
        else:
            ids = [_doc(store, args.document).id]
        for doc_id, page, line in search(store, ids, args.text, args.max):
            print(f"{store.document(doc_id).sha256[:12]} p.{page}: {line}")
    return 0


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        bbox = tuple(float(v) for v in text.split(","))
    except ValueError:
        bbox = ()
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise LensError("--bbox needs four finite numbers: x0,y0,x1,y1 (pt, top-left origin)")
    return bbox


def cmd_page_image(args: argparse.Namespace) -> int:
    from .pages import page_image

    bbox = parse_bbox(args.bbox) if args.bbox else None
    with Store() as store:
        img = page_image(store, _doc(store, args.document).id, args.page, bbox, args.max_px)
        region = "full page" if img.bbox is None else "region " + ",".join(f"{v:.0f}" for v in img.bbox)
        print(f"{img.path}  page {img.page}, {region}, {img.dpi:.0f} dpi, {img.width_px} x {img.height_px} px")
    return 0


def cmd_pdf_pages(args: argparse.Namespace) -> int:
    from .pages import pdf_pages

    with Store() as store:
        path, pages = pdf_pages(store, _doc(store, args.document).id, args.pages)
        print(f"{path}  pages {args.pages} ({len(pages)})")
    return 0


def cmd_parts(_args: argparse.Namespace) -> int:
    with Store() as store:
        for p in store.parts():
            print(f"{p['mpn']:24} {p['manufacturer']:20} documents: {p['documents']}")
    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    with Store() as store:
        rows = store.part_documents(args.part) if args.part else store.documents()
        for d in rows:
            extra = d["role"] if args.part else (d["parts"] or "-")
            print(f"{d['id']:4} {d['sha256'][:12]} {d['pages'] or '?':>5} p  {d['filename']:40} {extra}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datasheet-lens",
        description="Index of component datasheets, and pages of the original on demand.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("info", help="show the version and the store location")
    info.set_defaults(func=cmd_info)

    add = sub.add_parser("add", help="add a document to the central store")
    add.add_argument("file", help="the document file (PDF)")
    add.add_argument("--part", help="the part number (MPN) of the component")
    add.add_argument("--manufacturer", help="the manufacturer of the part")
    add.add_argument("--role", choices=ROLES, default="datasheet", help="the role of the document")
    add.add_argument("--title", help="the title (default: from the PDF metadata)")
    add.add_argument("--url", help="the source URL of the document")
    add.set_defaults(func=cmd_add)

    parts = sub.add_parser("parts", help="list the parts")
    parts.set_defaults(func=cmd_parts)

    docs = sub.add_parser("docs", help="list the documents, or the documents of one part")
    docs.add_argument("--part", help="the part number (MPN or alias)")
    docs.set_defaults(func=cmd_docs)

    index = sub.add_parser("index", help="build the index of documents (default: all)")
    index.add_argument("documents", nargs="*", help="document ID or SHA-256 prefix")
    index.set_defaults(func=cmd_index)

    show = sub.add_parser("show", help="show the index of a document")
    show.add_argument("document", help="document ID or SHA-256 prefix")
    show.set_defaults(func=cmd_show)

    outline = sub.add_parser("outline", help="show the outline under a section")
    outline.add_argument("document", help="document ID or SHA-256 prefix")
    outline.add_argument("--under", required=True, help="section number, for example 8.27")
    outline.set_defaults(func=cmd_outline)

    search = sub.add_parser("search", help="search the text of a document or of a part")
    search.add_argument("text", help="the text to find (case is ignored)")
    target = search.add_mutually_exclusive_group(required=True)
    target.add_argument("--document", help="document ID or SHA-256 prefix")
    target.add_argument("--part", help="part number: search all documents of the part")
    search.add_argument("--max", type=int, default=30, help="maximum number of lines")
    search.set_defaults(func=cmd_search)

    image = sub.add_parser("page-image", help="render a page, or a region of it, as PNG")
    image.add_argument("document", help="document ID or SHA-256 prefix")
    image.add_argument("page", type=int, help="page number (1 is the first page)")
    image.add_argument("--bbox", help="region x0,y0,x1,y1 in pt, top-left origin (300 to 600 dpi)")
    image.add_argument("--max-px", type=int, default=2576, help="maximum long edge in px (256 to 8000, default 2576)")
    image.set_defaults(func=cmd_page_image)

    pdfp = sub.add_parser("pdf-pages", help="write pages of a document to a new PDF")
    pdfp.add_argument("document", help="document ID or SHA-256 prefix")
    pdfp.add_argument("pages", help="pages, for example 7 or 3-5,8")
    pdfp.set_defaults(func=cmd_pdf_pages)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LensError as e:
        # A wrong input or a state that the user can repair: one line, no traceback.
        print(f"datasheet-lens: error: {e}", file=sys.stderr)
        return 2
    except (OSError, pdfium.PdfiumError) as e:
        # A file of the store that cannot be read (missing, damaged).
        print(f"datasheet-lens: error: cannot read a file: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
