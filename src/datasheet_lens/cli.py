"""Command line interface of datasheet-lens."""

import argparse
import math
import sys
from pathlib import Path

import pypdfium2 as pdfium

from . import __version__
from .config import store_home
from .errors import LensError
from .store import ROLES, Store


def cmd_info(_args: argparse.Namespace) -> int:
    print(f"datasheet-lens {__version__}")
    print(f"store: {store_home()}")
    return 0


def _ask_part(store: Store, doc, args: argparse.Namespace) -> tuple[str, str, list[str]]:
    """The part of a new document: --part, a part that the store links already,
    or the proposal from the PDF, which the user confirms. Also the variants that
    the datasheet covers. ("", "", []) for no part."""
    from .identify import propose

    if args.part:
        return args.part.strip(), (args.manufacturer or "").strip(), []
    linked = store.parts_of_document(doc.id)
    if len(linked) == 1:
        return linked[0]["mpn"], linked[0]["manufacturer"], []
    if linked:  # a datasheet of more variants: which one does the design use?
        names = [r["mpn"] for r in linked]
        print(f"part: the store links the document to {', '.join(names)}")
        if not sys.stdin.isatty():
            print(f"  give the variant that you use with --part (for example --part {names[0]})")
            return "", "", []
        answer = input("  Type the variant that you use (- = no part): ").strip()
        row = next((r for r in linked if r["mpn"].lower() == answer.lower()), None)
        if row is None:
            if answer and answer != "-":
                print(f"  {answer} is not one of them: give it with --part")
            return "", "", []
        return row["mpn"], row["manufacturer"], []
    p = propose(store, doc)
    if not p.mpn:
        print("part: no MPN found in the PDF; give it with --part")
        return "", "", []
    manufacturer = (args.manufacturer or "").strip() or p.manufacturer
    print(f"part: {p.mpn} ({manufacturer or 'manufacturer unknown'})\n  from: {', '.join(p.sources)}"
          + (f"; other candidates: {', '.join(p.candidates)}" if p.candidates else ""))
    if p.variants:
        print(f"  the datasheet covers the variants: {', '.join(p.variants)}")
    if args.yes:
        return p.mpn, manufacturer, p.variants
    if not sys.stdin.isatty():
        print(f"  not confirmed (no terminal): give --yes, or --part {p.mpn}")
        return "", "", []
    question = ("  Enter = yes, or type the variant that you use, or another MPN (- = no part): " if p.variants
                else "  Enter = yes, or type another MPN (- = no part): ")
    answer = input(question).strip()
    if answer == "-":
        return "", "", []
    if not answer:
        return p.mpn, manufacturer, p.variants
    chosen = next((v for v in p.variants if v.lower() == answer.lower()), "")
    if chosen:  # a variant of this datasheet: the same manufacturer
        return chosen, manufacturer, p.variants
    return answer, input(f"  manufacturer [{manufacturer}]: ").strip() or manufacturer, []


def cmd_add(args: argparse.Namespace) -> int:
    from .fetch import add_from_url, is_url
    from .indexer import build_index

    with Store() as store:
        if is_url(args.file):
            if args.url:
                raise LensError("--url is only for a local file: a downloaded file records its own URL")
            doc = add_from_url(store, args.file, title=args.title or "")
        else:
            doc = store.add_document(Path(args.file), title=args.title or "", source_url=args.url or "")
        print(f"document {doc.id}: {doc.sha256[:12]} {doc.filename} ({doc.pages} pages)")
        mpn, manufacturer, variants = _ask_part(store, doc, args)
        category = (args.category or "").strip()
        # The link first: an index error must not lose it. The datasheet is also
        # the datasheet of each variant that it covers.
        if mpn:
            part_id, failures = store.link_parts(doc.id, mpn, variants, manufacturer, args.role, category)
            for text in failures:
                print(f"{text} (give --manufacturer)", file=sys.stderr)
            if part_id is None:
                mpn = ""
            else:
                linked = [v for v in variants if v != mpn and not any(f.startswith(f"part {v}:") for f in failures)]
                print(f"part {mpn}: {args.role}" + (f"; also the variants {', '.join(linked)}" if linked else ""))
        idx = build_index(store, doc.id)
        if not idx["skipped"]:
            print(f"index: {idx['outline']} outline entries, {idx['captions']} captions")
        if not args.no_project:
            _add_to_project(store, doc, mpn, manufacturer, category, args)
    return 0


def _add_to_project(store: Store, doc, mpn: str, manufacturer: str, category: str,
                    args: argparse.Namespace) -> None:
    """In a project folder: add the part to the project, copy the document into
    the folder and build its element map."""
    from .elements import build_elements
    from .project import add_part, find_project, load, sync
    from .setup import element_backend

    try:
        root = find_project(args.project)
    except LensError:
        if args.project:
            raise
        return  # not in a project: the store only
    if not mpn:
        print(f"project {root}: not added (the document has no part)")
        return
    project = load(root)
    add_part(project, store, mpn, manufacturer, doc.sha256, args.role, category)
    report = sync(project, store)
    for text in report["problems"]:
        print(f"problem: {text}")
    files = [d["file"] for part in project.parts for d in part["documents"] if d["sha256"] == doc.sha256]
    if files:
        print(f"project: {project.folder}/{files[0]}")
    result = build_elements(store, doc.id, element_backend())
    if not result["skipped"]:
        print(f"element map: {result['elements']} elements ({result['backend']})")


def _doc(store: Store, ref: str):
    doc = store.document(ref)
    if doc is None:
        raise LensError(f"document not found: {ref}")
    return doc


def _docs(store: Store, refs: list[str]) -> list:
    """The documents of the references, or all documents if there is none."""
    if refs:
        return [_doc(store, r) for r in refs]
    return [store.document(r["id"]) for r in store.documents()]


def cmd_index(args: argparse.Namespace) -> int:
    from .indexer import build_index

    with Store() as store:
        for doc in _docs(store, args.documents):
            result = build_index(store, doc.id, force=args.force)
            note = " (current, not built again; --force builds it)" if result["skipped"] else ""
            print(f"{doc.sha256[:12]} {doc.filename}: {result['pages']} pages, "
                  f"{result['outline']} outline entries, {result['captions']} captions{note}")
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


def cmd_mcp(_args: argparse.Namespace) -> int:
    from .server import main as serve

    serve()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .setup import init

    return init(args.layout)


def cmd_elements(args: argparse.Namespace) -> int:
    from .elements import build_elements
    from .setup import element_backend

    backend = element_backend() if args.backend == "auto" else args.backend
    with Store() as store:
        for doc in _docs(store, args.documents):
            result = build_elements(store, doc.id, backend, force=args.force)
            note = " (current, not built again; --force builds it)" if result["skipped"] else ""
            print(f"{doc.sha256[:12]} {doc.filename}: {result['elements']} elements ({result['backend']}){note}")
    return 0


def cmd_page_map(args: argparse.Namespace) -> int:
    from .elements import page_map

    with Store() as store:
        for line in page_map(store, _doc(store, args.document).id, args.page, args.all):
            print(line)
    return 0


def cmd_element_text(args: argparse.Namespace) -> int:
    from .elements import element

    with Store() as store:
        doc = _doc(store, args.document)
        for eid in args.ids:
            e = element(store, doc.id, eid)
            print(f"{eid} ({e['type']}, p.{e['page']}, [{e['x0']:.0f}, {e['y0']:.0f}, "
                  f"{e['x1']:.0f}, {e['y1']:.0f}]): {e['text']}")
    return 0


def cmd_items(args: argparse.Namespace) -> int:
    from .elements import list_items

    with Store() as store:
        for e in list_items(store, _doc(store, args.document).id, args.kind):
            print(f"{e['element_id']:9} p.{e['page']:<4} [{e['x0']:.0f}, {e['y0']:.0f}, {e['x1']:.0f}, "
                  f"{e['y1']:.0f}]  {e['text'][:90] or '(no caption)'}")
    return 0


ELEMENT_PAD = 6.0  # pt around an element region


def cmd_page_image(args: argparse.Namespace) -> int:
    from .pages import page_image

    if args.element and args.bbox:
        raise LensError("give --element or --bbox, not the two")
    bbox = parse_bbox(args.bbox) if args.bbox else None
    with Store() as store:
        doc = _doc(store, args.document)
        page = args.page
        if args.element:
            from .elements import element

            e = element(store, doc.id, args.element)
            if page is not None and page != e["page"]:
                raise LensError(f"element {args.element} is on page {e['page']}, not on page {page}")
            page = e["page"]
            bbox = (e["x0"] - ELEMENT_PAD, e["y0"] - ELEMENT_PAD, e["x1"] + ELEMENT_PAD, e["y1"] + ELEMENT_PAD)
        if page is None:
            raise LensError("give a page number or --element")
        img = page_image(store, doc.id, page, bbox, args.max_px)
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


PROJECT_ADD_HINT = "datasheet-lens project add-part {mpn} --document {sha}"


def cmd_project_init(args: argparse.Namespace) -> int:
    from .project import init_project

    project = init_project(args.path or Path.cwd(), args.folder, args.layout)
    print(f"project file: {project.path}\nfolder: {project.folder_path}")
    return 0


def cmd_project_status(args: argparse.Namespace) -> int:
    from .project import format_parts, format_report, load, sync

    project = load(args.path)
    with Store() as store:
        report = sync(project, store, apply=False)
    print("\n".join(format_parts(project) + format_report(report, False, PROJECT_ADD_HINT)))
    return 0


def cmd_project_sync(args: argparse.Namespace) -> int:
    from .elements import build_elements
    from .project import format_report, load, sync
    from .setup import element_backend

    project = load(args.path)
    with Store() as store:
        report = sync(project, store, restore=args.restore)
        print("\n".join(format_report(report, True, PROJECT_ADD_HINT)))
        backend, failed = element_backend(), 0
        for doc_id in report["documents"]:
            try:
                result = build_elements(store, doc_id, backend)
            except (LensError, OSError, pdfium.PdfiumError) as e:
                print(f"element map of document {doc_id}: failed: {e}", file=sys.stderr)
                failed += 1
                continue
            if not result["skipped"]:
                print(f"element map of document {doc_id}: {result['elements']} elements ({result['backend']})")
    return 1 if failed else 0


def cmd_project_add_part(args: argparse.Namespace) -> int:
    from .project import add_part, load

    project = load(args.path)
    with Store() as store:
        part = add_part(project, store, args.mpn, args.manufacturer or "", args.document or "", args.role,
                        args.category or "")
    print(f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'}): {len(part['documents'])} documents")
    for d in part["documents"]:
        print(f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}")
    print("run 'datasheet-lens project sync' to copy the documents into the folder")
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

    add = sub.add_parser("add", help="add a document: to the store, and in a project folder also to the project")
    add.add_argument("file", help="the document file (PDF), or an http or https URL to download it from")
    add.add_argument("--part", help="the part number (MPN); without it, the tool proposes one from the PDF")
    add.add_argument("--yes", action="store_true", help="accept the proposed part without a question")
    add.add_argument("--project", help="the project folder (default: the current folder or a parent folder)")
    add.add_argument("--no-project", action="store_true", help="add to the store only, not to the project")
    add.add_argument("--manufacturer", help="the manufacturer of the part")
    add.add_argument("--category", help="the function of the part, for example ADC (for the project layout)")
    add.add_argument("--role", choices=ROLES, default="datasheet", help="the role of the document")
    add.add_argument("--title", help="the title (default: from the PDF metadata)")
    add.add_argument("--url", help="the source URL of a local file (a downloaded file records its URL)")
    add.set_defaults(func=cmd_add)

    parts = sub.add_parser("parts", help="list the parts")
    parts.set_defaults(func=cmd_parts)

    docs = sub.add_parser("docs", help="list the documents, or the documents of one part")
    docs.add_argument("--part", help="the part number (MPN or alias)")
    docs.set_defaults(func=cmd_docs)

    index = sub.add_parser("index", help="build the index of documents (default: all)")
    index.add_argument("documents", nargs="*", help="document ID or SHA-256 prefix")
    index.add_argument("--force", action="store_true", help="build again also a current index")
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

    serve = sub.add_parser("mcp", help="run the MCP server (stdio)")
    serve.set_defaults(func=cmd_mcp)

    init = sub.add_parser("init", help="make the store; ask about the optional docling component")
    init.add_argument("--layout", choices=("ask", "yes", "no"), default="ask",
                      help="install docling for the element map (default: ask)")
    init.set_defaults(func=cmd_init)

    elements = sub.add_parser("elements", help="build the element map of documents (default: all)")
    elements.add_argument("documents", nargs="*", help="document ID or SHA-256 prefix")
    elements.add_argument("--backend", choices=("auto", "docling", "text"), default="auto",
                          help="auto: the configured backend (see init)")
    elements.add_argument("--force", action="store_true", help="build again also a current map")
    elements.set_defaults(func=cmd_elements)

    pmap = sub.add_parser("page-map", help="the structure of a page: headings, tables, figures, formulas")
    pmap.add_argument("document", help="document ID or SHA-256 prefix")
    pmap.add_argument("page", type=int, help="page number")
    pmap.add_argument("--all", action="store_true", help="also the text elements")
    pmap.set_defaults(func=cmd_page_map)

    etext = sub.add_parser("element-text", help="the full text of elements")
    etext.add_argument("document", help="document ID or SHA-256 prefix")
    etext.add_argument("ids", nargs="+", help="element IDs, for example p7.T1")
    etext.set_defaults(func=cmd_element_text)

    items = sub.add_parser("items", help="the tables and figures of a document")
    items.add_argument("document", help="document ID or SHA-256 prefix")
    items.add_argument("kind", nargs="?", choices=("table", "figure", "caption"), help="only one type")
    items.set_defaults(func=cmd_items)

    image = sub.add_parser("page-image", help="render a page, a region or an element as PNG")
    image.add_argument("document", help="document ID or SHA-256 prefix")
    image.add_argument("page", type=int, nargs="?", help="page number (not necessary with --element)")
    image.add_argument("--element", help="element ID from the element map, for example p9.F2")
    image.add_argument("--bbox", help="region x0,y0,x1,y1 in pt, top-left origin (300 to 600 dpi)")
    image.add_argument("--max-px", type=int, default=2576, help="maximum long edge in px (256 to 8000, default 2576)")
    image.set_defaults(func=cmd_page_image)

    pdfp = sub.add_parser("pdf-pages", help="write pages of a document to a new PDF")
    pdfp.add_argument("document", help="document ID or SHA-256 prefix")
    pdfp.add_argument("pages", help="pages, for example 7 or 3-5,8")
    pdfp.set_defaults(func=cmd_pdf_pages)

    project = sub.add_parser("project", help="the parts of a project and the copies of their documents")
    psub = project.add_subparsers(dest="project_command", required=True)
    pinit = psub.add_parser("init", help="make the project file datasheet-lens.toml")
    pinit.add_argument("--folder", default="datasheets", help="the document folder, relative to the project")
    pinit.add_argument("--layout", default="{manufacturer}",
                       help="the subfolder of each copy: {manufacturer}, {category}, {mpn}, or \"\" (default: "
                            "{manufacturer})")
    pinit.set_defaults(func=cmd_project_init)
    pstatus = psub.add_parser("status", help="show the parts, and the copies that are missing or changed")
    pstatus.set_defaults(func=cmd_project_status)
    psync = psub.add_parser("sync", help="copy the documents into the folder; import the new files of the folder")
    psync.add_argument("--restore", action="store_true", help="copy a changed copy again from the store")
    psync.set_defaults(func=cmd_project_sync)
    padd = psub.add_parser("add-part", help="add a part, with the documents that the store links to it")
    padd.add_argument("mpn", help="the part number (MPN) or an alias")
    padd.add_argument("--manufacturer", help="the manufacturer of the part")
    padd.add_argument("--category", help="the function of the part, for example ADC (for the layout)")
    padd.add_argument("--document", help="link this document (ID or SHA-256 prefix) to the part first")
    padd.add_argument("--role", choices=ROLES, default="datasheet", help="the role of that document")
    padd.set_defaults(func=cmd_project_add_part)
    for p in (pinit, pstatus, psync, padd):
        p.add_argument("--path", help="the project folder (default: the current folder or a parent folder)")
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
