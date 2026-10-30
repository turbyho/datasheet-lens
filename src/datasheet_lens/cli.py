"""Command line interface of datasheet-lens."""

import argparse
import contextlib
import math
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

from . import __version__
from .config import store_home
from .errors import LensError
from .store import ROLES, Store


def cmd_info(_args: argparse.Namespace) -> int:
    """The command info: show the version, the store folder, and a newer release
    on PyPI (it asks PyPI each time, and waits for the answer a few seconds at
    most)."""
    from .version_check import TIMEOUT, start_update_check, user_notes

    print(f"datasheet-lens {__version__}")
    print(f"store: {store_home()}")
    thread = start_update_check(force=True)
    if thread is not None:
        thread.join(TIMEOUT)
    for note in user_notes():
        print(note)
    return 0


def _input(prompt: str) -> str:
    """An answer of the user. End of input (Ctrl-D) is an empty answer: the
    default."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""


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
        answer = _input("  Type the variant that you use (- = no part): ")
        row = next((r for r in linked if r["mpn"].lower() == answer.lower()), None)
        if row is None:
            if answer and answer != "-":
                print(f"  {answer} is not one of them: give it with --part")
            return "", "", []
        return row["mpn"], row["manufacturer"], []
    p = propose(doc)
    if not p.mpn:
        print("part: no MPN found in the PDF; give it with --part")
        return "", "", []
    manufacturer = (args.manufacturer or "").strip() or store.known_maker(p.mpn, p.manufacturer)
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
    answer = _input(question)
    if answer == "-":
        return "", "", []
    if not answer:
        return p.mpn, manufacturer, p.variants
    chosen = next((v for v in p.variants if v.lower() == answer.lower()), "")
    if chosen:  # a variant of this datasheet: the same manufacturer
        return chosen, manufacturer, p.variants
    default = (args.manufacturer or "").strip() or store.known_maker(answer, p.manufacturer)
    return answer, _input(f"  manufacturer [{default}]: ") or default, []


def cmd_add(args: argparse.Namespace) -> int:
    """The command add: a document into the store and, in a project folder, into the project."""
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
                print(text, file=sys.stderr)
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
    # The project (the BOM) needs the concrete orderable MPN: the package, the
    # voltage or the grade make it (TLV75533PDBVR of the base part TLV755P).
    project = load(root)
    concrete = (args.orderable or "").strip()
    users = [p["mpn"] for p in project.parts if p.get("base", "").lower() == mpn.lower()]
    if not concrete and users:
        # The project has concrete parts of this base part: they get the
        # document through the base part. The base part is not a second BOM line.
        print(f"project: {', '.join(users)} (base part {mpn}) get the document")
    else:
        if not concrete and not args.yes and sys.stdin.isatty():
            concrete = _input(f"  the concrete orderable MPN that the design uses (package, voltage ...; "
                              f"Enter = {mpn}): ")
        part = add_part(project, store, concrete or mpn, manufacturer, doc.sha256, args.role, category, base=mpn)
        if part.get("base"):
            print(f"project part: {part['mpn']} (base part {part['base']})")
        note = _base_note(store, part)
        if note:
            print(note)
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
    """The document of a reference (ID or SHA-256 prefix), or a LensError."""
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
    """The command index: build the index of documents (all documents by default)."""
    from .indexer import build_index

    with Store() as store:
        for doc in _docs(store, args.documents):
            result = build_index(store, doc.id, force=args.force)
            note = " (current, not built again; --force builds it)" if result["skipped"] else ""
            print(f"{doc.sha256[:12]} {doc.filename}: {result['pages']} pages, "
                  f"{result['outline']} outline entries, {result['captions']} captions{note}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """The command show: print the index of a document."""
    from .indexer import index_markdown

    with Store() as store:
        print(index_markdown(store, _doc(store, args.document).id), end="")
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    """The command outline: print the outline entries under a section."""
    from .indexer import outline_under

    with Store() as store:
        for o in outline_under(store, _doc(store, args.document).id, args.under):
            print(f"{'  ' * o['level']}- {o['title']} (p.{o['page']})")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """The command search: find a text in a document or in the documents of a part."""
    from .indexer import search

    with Store() as store:
        if args.part:
            ids = [r["id"] for r in store.part_documents(args.part)]
        else:
            ids = [_doc(store, args.document).id]
        mode = "regex" if args.regex else "word" if args.word else "text"
        hidden: list = []
        hits = search(store, ids, args.text, args.max, mode, args.all, hidden)
        for doc_id, page, line in hits:
            print(f"{store.document(doc_id).sha256[:12]} p.{page}: {line}")
        if hidden:
            print(f"({'at least ' if len(hits) >= args.max else ''}{len(hidden)} lines in page headers or "
                  "footers are not shown: -a shows them)")
    return 0


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    """A region "x0,y0,x1,y1" (pt) as four numbers, or a LensError."""
    try:
        bbox = tuple(float(v) for v in text.split(","))
    except ValueError:
        bbox = ()
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise LensError("--bbox needs four finite numbers: x0,y0,x1,y1 (pt, top-left origin)")
    return bbox


def cmd_mcp(_args: argparse.Namespace) -> int:
    """The command mcp: run the MCP server on stdin and stdout."""
    from .server import main as serve

    serve()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """The command init: make the store, and ask about the optional docling component."""
    from .setup import init

    return init(args.layout)


def cmd_elements(args: argparse.Namespace) -> int:
    """The command elements: build the element map of documents."""
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
    """The command page-map: print the structure of a page."""
    from .elements import page_map

    with Store() as store:
        for line in page_map(store, _doc(store, args.document).id, args.page, args.all):
            print(line)
    return 0


def cmd_element_text(args: argparse.Namespace) -> int:
    """The command element-text: print the full text of elements."""
    from .elements import element

    with Store() as store:
        doc = _doc(store, args.document)
        for eid in args.ids:
            e = element(store, doc.id, eid)
            print(f"{eid} ({e['type']}, p.{e['page']}, [{e['x0']:.0f}, {e['y0']:.0f}, "
                  f"{e['x1']:.0f}, {e['y1']:.0f}]): {e['text']}")
    return 0


def cmd_items(args: argparse.Namespace) -> int:
    """The command items: print the tables, figures and captions of a document."""
    from .elements import list_items

    with Store() as store:
        for e in list_items(store, _doc(store, args.document).id, args.kind):
            print(f"{e['element_id']:9} p.{e['page']:<4} [{e['x0']:.0f}, {e['y0']:.0f}, {e['x1']:.0f}, "
                  f"{e['y1']:.0f}]  {e['text'][:90] or '(no caption)'}")
    return 0


ELEMENT_PAD = 6.0  # pt around an element region


def cmd_page_image(args: argparse.Namespace) -> int:
    """The command page-image: render a page, a region or an element as a PNG file."""
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
    """The command pdf-pages: write pages of a document to a new PDF file."""
    from .pages import pdf_pages

    with Store() as store:
        path, pages = pdf_pages(store, _doc(store, args.document).id, args.pages)
        print(f"{path}  pages {args.pages} ({len(pages)})")
    return 0


def cmd_parts(_args: argparse.Namespace) -> int:
    """The command parts: list the parts in the store."""
    with Store() as store:
        for p in store.parts():
            print(f"{p['mpn']:24} {p['manufacturer']:20} documents: {p['documents']}")
    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    """The command docs: list the documents, or the documents of a part."""
    with Store() as store:
        rows = store.part_documents(args.part) if args.part else store.documents()
        for d in rows:
            extra = d["role"] if args.part else (d["parts"] or "-")
            print(f"{d['id']:4} {d['sha256'][:12]} {d['pages'] or '?':>5} p  {d['filename']:40} {extra}")
    return 0


PROJECT_ADD_HINT = ("datasheet-lens project add-part \"{part}\"{base} --document {sha}", " --base \"{base}\"")


def cmd_project_init(args: argparse.Namespace) -> int:
    """The command project init: make the project file and its folder, and put the
    datasheet-lens block into AGENTS.md and CLAUDE.md. A second run changes only
    a block that is not current."""
    from . import agents
    from .project import DEFAULT_FOLDER, DEFAULT_LAYOUT, PROJECT_FILE, find_project, init_project, load

    root = Path(args.path or Path.cwd()).expanduser().resolve()
    if not args.path:  # in a subfolder of a project: that project, not a new one in the subfolder
        with contextlib.suppress(LensError):
            root = find_project(root)
    if (root / PROJECT_FILE).is_file():
        project = load(root)
        print(f"project file: {project.path} (it exists: not changed)")
        for option, value, current in (("--folder", args.folder, project.folder),
                                       ("--layout", args.layout, project.layout)):
            if value is not None and value != current:
                print(f"  {option} {value!r} has no effect: the project has {current!r} (change it in "
                      f"{PROJECT_FILE})")
    else:
        project = init_project(root, args.folder if args.folder is not None else DEFAULT_FOLDER,
                               args.layout if args.layout is not None else DEFAULT_LAYOUT)
        print(f"project file: {project.path}\nfolder: {project.folder_path}")
    for name, action in agents.update(root, project.folder):
        print(f"{name}: datasheet-lens block {action}")
    return 0


def cmd_project_status(args: argparse.Namespace) -> int:
    """The command ls (project status): print the parts, documents and copies,
    and a newer release that an earlier check found."""
    from .project import format_parts, format_report, load, sync
    from .version_check import user_notes

    project = load(args.path)
    with Store() as store:
        report = sync(project, store, apply=False)
    print("\n".join(user_notes() + format_parts(project) + format_report(report, False, PROJECT_ADD_HINT)))
    return 0


def cmd_project_sync(args: argparse.Namespace) -> int:
    """The command sync (project sync): synchronize the project and build element maps."""
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


def _document_key(ref: str) -> str:
    """A document ID of the store (docs, list_documents) as its SHA-256; else the
    reference as it is (a SHA-256 prefix or a file name)."""
    if ref.strip().isdigit() and len(ref.strip()) < 8:
        with Store() as store:
            doc = store.document(ref.strip())
        if doc is None:
            raise LensError(f"document not found: {ref}")
        return doc.sha256
    return ref


def cmd_rm(args: argparse.Namespace) -> int:
    """The command rm: remove a part, or a document (-d), from the project."""
    from .project import load, remove_document, remove_part

    if not args.mpn and not args.document:
        raise LensError("give a part (MPN) or a document (-d)")
    project = load(args.path)
    apply = not args.dry_run
    with Store() as store:
        if args.document:
            result = remove_document(project, _document_key(args.document), args.mpn or "",
                                     args.manufacturer or "", args.pinned, apply, store)
        else:
            result = remove_part(project, args.mpn, args.manufacturer or "", args.pinned, apply, store)
    if args.document:
        what = f"document {result['document']} from {', '.join(result['parts'])}"
    else:
        what = f"part {result['part']}"
    prefix = "would remove" if args.dry_run else "removed"
    print(f"{prefix}: {what}")
    for rel in result["deleted"]:
        print(f"  {'would delete' if args.dry_run else 'deleted'}: {project.folder}/{rel}")
    for text in result["kept"]:
        print(f"  kept: {project.folder}/{text}")
    return 0


def cmd_project_add_part(args: argparse.Namespace) -> int:
    """The command project add-part: add a part with its documents to the project."""
    from .project import add_part, load

    project = load(args.path)
    with Store() as store:
        part = add_part(project, store, args.mpn, args.manufacturer or "", args.document or "", args.role,
                        args.category or "", args.base or "")
        note = _base_note(store, part)
    print(f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'}): {len(part['documents'])} documents")
    for d in part["documents"]:
        print(f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}")
    if any(not project.file_path(d["file"]).is_file() for d in part["documents"]):
        print("run 'datasheet-lens sync' to copy the documents into the folder")
    if note:
        print(note)
    return 0


def _base_note(store: Store, part: dict) -> str:
    """The note for a project part that is probably a base part (see identify)."""
    from .identify import project_base_note

    return project_base_note(store, part, 'datasheet-lens set "{mpn}" -m "{code}" -b {base}')


def cmd_key(args: argparse.Namespace) -> int:
    """The command key: save, remove or show the API keys of the distributors."""
    import getpass

    from .keys import KEYS, delete_key, key_source, key_sources, set_key

    if not args.name:
        if args.delete:
            raise LensError("give the key to remove, for example: datasheet-lens key mouser --delete")
        for name, where in key_sources().items():
            print(f"{name}: {where}")
        return 0
    if args.delete:
        delete_key(args.name)
        print(f"{args.name}: removed from the key store and the file")
        where = key_source(args.name)
        if where != "not set":  # an environment variable, or a key store that did not answer
            print(f"{args.name}: a key is still in {where}")
        return 0
    # Not on the screen and not in the shell history.
    value = getpass.getpass(f"{args.name} API key: ") if sys.stdin.isatty() else sys.stdin.readline()
    print(f"{args.name}: saved in {set_key(args.name, value)}")
    env = KEYS[args.name.lower()]
    if key_source(args.name).startswith("environment"):
        print(f"{args.name}: the environment variable {env} comes first: remove it, else the saved key is not used")
    return 0


def cmd_dist(args: argparse.Namespace) -> int:
    """The command dist: show or set the distributors that datasheet-lens asks,
    globally or (--local) for the project."""
    from .distributors import check_names, status_lines
    from .project import load, load_optional, set_distributors
    from .setup import load_config, save_config

    if args.local:
        project = load(args.path)
        set_distributors(project, check_names(args.names) if args.names else None)
        print(f"project {project.root}: " + (f"distributors {', '.join(project.distributors)}"
                                            if project.distributors is not None else "the global configuration"))
    elif args.names:
        cfg = load_config(strict=True)  # a file that cannot be read is not written over
        cfg["distributors"] = check_names(args.names)
        save_config(cfg)
        print(f"global: distributors {', '.join(cfg['distributors'])}")
    print("\n".join(status_lines(load_optional(args.path))))
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """The command find: the parts at the distributors (official APIs)."""
    from .distributors import search_lines
    from .project import load_optional

    print("\n".join(search_lines(args.mpn, args.exact, load_optional(args.path))))
    return 0


def cmd_stock(args: argparse.Namespace) -> int:
    """The command stock: the lifecycle state and the stock of each project part."""
    from .distributors import availability
    from .project import load

    with Store() as store:
        print("\n".join(availability(load(args.path), store)))
    return 0


def cmd_maker(args: argparse.Namespace) -> int:
    """The command maker: show the manufacturer registry, or record a relation
    of two manufacturer names."""
    with Store() as store:
        if not args.relation:
            print("\n".join(store.maker_listing()))
            return 0
        for line in store.maker_relation(args.relation, args.name, args.other):
            print(f"merged: {line}")
        print(f"recorded: {args.relation} {args.name!r} {args.other!r}")
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    """The command links: search links for a part (datasheet, stock, CAD files)."""
    from .links import format_links

    print("\n".join(format_links(args.mpn)))
    return 0


def cmd_codes(args: argparse.Namespace) -> int:
    """The command codes: the orderable codes of a datasheet."""
    from .identify import ordering_codes
    from .indexer import require_index

    with Store() as store:
        doc = _doc(store, args.document)
        require_index(store, [doc.id])
        rows = ordering_codes(store, doc.id)
    for code, page, line, table in rows:
        print(f"{code}  p.{page}  {'' if table else '(in the text) '}{line}")
    if not rows:
        print("no orderable codes in the text (the datasheet can give a scheme of its codes only)")
    return 0


# The column names of a BOM file, the best first, as _column() compares them
# (case, spaces, "_", "." and "-" do not count). KiCad (kicad-cli sch export
# bom): Reference, Value, Datasheet, DNP and the fields of the symbols, for
# example MPN or Manufacturer_Part_Number. Altium: Designator, "Manufacturer
# Part Number 1". Eagle and Fusion: Parts, Value, and the attributes of the
# libraries: MP (SnapEDA) or MANUFACTURER_PART_NUMBER (Mouser, SamacSys), each
# part in one of them. Thus each row gets the MPN from the first filled column.
# Value is the last choice: it also holds values of passive parts.
MPN_COLUMNS = ("MPN", "Manufacturer Part Number", "Manufacturer Part Number 1", "MPN 1", "MP", "Mfr Part Number",
               "Mfr PN", "Manufacturer PN", "Value")  # not "Part Number": often a part number of the company
REFERENCE_COLUMNS = ("Reference", "References", "Refs", "Designator", "Designators", "RefDes", "Parts")
DNP_COLUMNS = ("DNP", "Do not populate", "Do not place", "Not fitted")
DATASHEET_COLUMNS = ("Datasheet", "Datasheet URL", "Datasheet Link")


def _column(names: list[str], wanted) -> str:
    """The first name of wanted that the BOM has, as the BOM spells it, or "".
    The case, spaces, "_", "." and "-" do not count."""
    def key(text: str) -> str:
        return re.sub(r"[\s_.\-]", "", text).lower()

    have = {key(n): n for n in names}
    return next((have[key(w)] for w in wanted if key(w) in have), "")


def _read_bom(path: str) -> tuple[list[dict], str]:
    """The rows of a BOM file (CSV: comma, semicolon or tab) and its encoding:
    UTF-16 with its byte order mark (Excel "Unicode text"), UTF-8, or cp1252 (a
    BOM file from Windows) when it is not UTF-8."""
    import csv

    with open(path, "rb") as f:
        head = f.read(2)
    utf16 = head in (b"\xff\xfe", b"\xfe\xff")
    for encoding in ("utf-16",) if utf16 else ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                except csv.Error:  # one column, or a sample that the sniffer cannot read
                    dialect = csv.excel
                return list(csv.DictReader(f, dialect=dialect)), encoding
        except UnicodeDecodeError:
            continue
    raise LensError(f"{path}: not UTF-8 or cp1252 text: export the BOM as UTF-8")


def cmd_check(args: argparse.Namespace) -> int:
    """The command check: compare the project with a BOM file (CSV) of the CAD
    project, for example from "kicad-cli sch export bom"."""
    from .project import cad_mpns, check_parts, format_check, load

    rows, encoding = _read_bom(args.bom)
    names = list(rows[0]) if rows else []
    # The Value field is always read (alternatives, conflicts); --column selects the MPN field.
    value_column = _column(names, ["Value"])
    if args.column:
        chosen = _column(names, [args.column])
        if not chosen:
            raise LensError(f"{args.bom}: no column {args.column!r} ({', '.join(names)})")
        field_columns = [] if chosen == value_column else [chosen]
    else:
        field_columns = [c for c in dict.fromkeys(_column(names, [w]) for w in MPN_COLUMNS) if c and c != value_column]
    columns = field_columns + ([value_column] if value_column else [])
    if not columns:
        raise LensError(f"{args.bom}: no MPN column; give one with --column ({', '.join(names)})")
    ref_column, dnp = _column(names, REFERENCE_COLUMNS), _column(names, DNP_COLUMNS)
    datasheet = _column(names, DATASHEET_COLUMNS)
    cad = []
    for row in rows:
        if dnp and (row.get(dnp) or "").strip().lower() not in ("", "0", "no", "false"):
            continue  # not fitted
        # The MPN fields, and the Value field: it also holds values of passive
        # parts and alternatives (see project.cad_mpns).
        mpns, conflict = cad_mpns([(c, row.get(c) or "") for c in field_columns],
                                  row.get(value_column) or "" if value_column else "")
        if not mpns:
            continue
        cad.append({"mpns": mpns, "conflict": conflict,
                    "datasheet": (row.get(datasheet) or "").strip() if datasheet else "",
                    "reference": (row.get(ref_column) or "").strip() if ref_column else ""})
    with Store() as store:
        result = check_parts(load(args.path), store, cad)
    print(f"{args.bom}: MPN from {', '.join(columns)}" + (f"; references from {ref_column}" if ref_column else "")
          + ("; read as cp1252" if encoding == "cp1252" else "") + f"; {len(cad)} lines with an MPN")
    print("\n".join(format_check(result, cli=True)))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """The command set: change a part of the project in place (MPN, base part,
    category, manufacturer)."""
    from .project import load, update_part

    project = load(args.path)
    with Store() as store:
        part = update_part(project, store, args.part, args.of or "", args.mpn or "", args.base or "",
                           args.category, args.manufacturer or "")
    print(f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'})"
          + (f", base part {part['base']}" if part.get("base") else ""))
    print("run 'datasheet-lens sync' to move the copies to the subfolders of the layout")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The argument parser of the CLI: one subcommand for each task."""
    parser = argparse.ArgumentParser(
        prog="datasheet-lens",
        description="Index of component datasheets, and pages of the original on demand.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("info", help="show the version and the store location")
    info.set_defaults(func=cmd_info)

    add = sub.add_parser("add", help="add a document (PATH|URL): to the store, and in a project folder also "
                                     "to the project")
    add.add_argument("file", metavar="PATH|URL",
                     help="the path of a PDF file, or an http or https URL of a PDF to download")
    add.add_argument("-m", "--mpn", "--orderable", dest="orderable", metavar="MPN",
                     help="the concrete orderable MPN that the design uses (package, voltage ...), for the "
                          "project")
    add.add_argument("-b", "--base", "--part", dest="part", metavar="MPN",
                     help="the base part of the datasheet; without it, the tool proposes one from the PDF")
    add.add_argument("-y", "--yes", action="store_true", help="accept the proposed part without a question")
    add.add_argument("-p", "--project", "--path", dest="project",
                     help="the project folder (default: the current folder or a parent folder)")
    add.add_argument("--no-project", action="store_true", help="add to the store only, not to the project")
    add.add_argument("-M", "--manufacturer", help="the manufacturer of the part")
    add.add_argument("-c", "--category", help="the function of the part, for example ADC (for the project layout)")
    add.add_argument("--role", choices=ROLES, default="datasheet", help="the role of the document")
    add.add_argument("--title", help="the title (default: from the PDF metadata)")
    add.add_argument("--url", help="the source URL of a local file (a downloaded file records its URL)")
    add.set_defaults(func=cmd_add)

    # Short commands for the project (the long forms are under "project").
    rm = sub.add_parser("rm", help="remove a part (MPN), or a document (-d), from the project")
    rm.add_argument("mpn", nargs="?", metavar="MPN", help="the part; with -d: only from this part")
    rm.add_argument("-d", "--document", metavar="DOC", help="a document: ID, SHA-256 prefix or file name")
    rm.add_argument("-M", "--manufacturer", help="the manufacturer, if the project has more parts with the MPN")
    rm.add_argument("--pinned", action="store_true", help="also delete a file that you put into the folder")
    rm.add_argument("-n", "--dry-run", action="store_true", help="only show what would change")
    rm.set_defaults(func=cmd_rm)
    st = sub.add_parser("set", help="change a part of the project in place: its MPN, base part, category or "
                                    "manufacturer")
    st.add_argument("part", metavar="MPN", help="the part of the project now")
    st.add_argument("-m", "--mpn", help="the new MPN, for example the concrete orderable MPN")
    st.add_argument("-b", "--base", metavar="MPN", help="the base part of the datasheet")
    st.add_argument("-c", "--category", help="the function of the part (\"\" removes it)")
    st.add_argument("-M", "--manufacturer", help="the new manufacturer")
    st.add_argument("--of", metavar="MANUFACTURER", help="the manufacturer now, if the project has more parts "
                                                         "with the MPN")
    st.set_defaults(func=cmd_set)
    ck = sub.add_parser("check", help="compare the project with a BOM file (CSV) of the CAD project")
    ck.add_argument("bom", metavar="BOM.csv", help="the BOM, for example from kicad-cli sch export bom")
    ck.add_argument("--column", help="the column of the MPN (default: MPN, Manufacturer Part Number (1) ... "
                                     "or Value)")
    ck.set_defaults(func=cmd_check)
    cd = sub.add_parser("codes", help="show the orderable codes of a datasheet")
    cd.add_argument("document", metavar="DOC", help="document ID or SHA-256 prefix")
    cd.set_defaults(func=cmd_codes)
    lk = sub.add_parser("links", help="show search links for a part: datasheet, stock, symbol, footprint, 3D model")
    lk.add_argument("mpn", metavar="MPN")
    lk.set_defaults(func=cmd_links)
    fd = sub.add_parser("find", help="find a part at the distributors: concrete MPNs, state, stock, prices "
                                     "(needs a key: datasheet-lens key mouser)")
    fd.add_argument("mpn", metavar="MPN", help="an MPN, or the start of the MPNs (a base part)")
    fd.add_argument("-x", "--exact", action="store_true", help="only this MPN")
    fd.set_defaults(func=cmd_find)
    ds = sub.add_parser("dist", help="show or set the distributors that datasheet-lens asks (global, or with "
                                     "--local for the project)")
    ds.add_argument("names", nargs="*", metavar="NAME", help="the distributors, for example mouser")
    ds.add_argument("--local", action="store_true",
                    help="set them in the project file; with no NAME, the project uses the global setting")
    ds.add_argument("-p", "--path", "--project", dest="path", help="the project folder (default: the current folder or a parent "
                                                      "folder)")
    ds.set_defaults(func=cmd_dist)
    ky = sub.add_parser("key", help="save the API key of a distributor (asks for it), or show where the keys are")
    ky.add_argument("name", nargs="?", metavar="NAME", help="the distributor: mouser. Empty: show the keys")
    ky.add_argument("--delete", action="store_true", help="remove the saved key")
    ky.set_defaults(func=cmd_key)
    mk = sub.add_parser("maker", help="show the manufacturer registry, or record a relation of two manufacturer "
                                      "names")
    mk_sub = mk.add_subparsers(dest="relation")
    for relation, text, name, other in (
            ("alias", "one manufacturer with two names, for example TI and \"Texas Instruments\"", "NAME", "OTHER"),
            ("distinct", "two manufacturers: each makes a part with the same MPN (a second source)", "NAME",
             "OTHER")):
        rel = mk_sub.add_parser(relation, help=text)
        rel.add_argument("name", metavar=name)
        rel.add_argument("other", metavar=other)
    mk.set_defaults(func=cmd_maker)
    sk = sub.add_parser("stock", help="show the lifecycle state and the stock of each project part")
    sk.set_defaults(func=cmd_stock)
    ls = sub.add_parser("ls", help="show the parts and documents of the project (project status)")
    ls.set_defaults(func=cmd_project_status)
    psync_short = sub.add_parser("sync", help="synchronize the project (project sync)")
    psync_short.add_argument("--restore", action="store_true", help="copy a changed copy again from the store")
    psync_short.set_defaults(func=cmd_project_sync)
    for p in (rm, st, ck, sk, fd, ls, psync_short):
        p.add_argument("-p", "--path", "--project", dest="path",
                       help="the project folder (default: the current folder or a parent folder)")

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
    kind = search.add_mutually_exclusive_group()
    kind.add_argument("-w", "--word", action="store_true", help="whole words only")
    kind.add_argument("-r", "--regex", action="store_true", help="the text is a regular expression")
    search.add_argument("-a", "--all", action="store_true", help="also the page headers and footers")
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
    pinit = psub.add_parser("init", help="make the project file datasheet-lens.toml, and put the datasheet-lens "
                                         "block into AGENTS.md and CLAUDE.md (a second run updates the block)")
    pinit.add_argument("--folder", help="the document folder, relative to the project (default: datasheets)")
    pinit.add_argument("--layout",
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
    padd.add_argument("--base", metavar="MPN",
                      help="mpn is a concrete orderable part of this base part (the part of the datasheet)")
    padd.add_argument("--document", help="link this document (ID or SHA-256 prefix) to the part first")
    padd.add_argument("--role", choices=ROLES, default="datasheet", help="the role of that document")
    padd.set_defaults(func=cmd_project_add_part)
    for p in (pinit, pstatus, psync, padd):
        p.add_argument("-p", "--path", "--project", dest="path",
                       help="the project folder (default: the current folder or a parent folder)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Give the exit code: 0 done, 1 a file cannot be read, 2 a wrong input,
    130 stopped by the user."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # Ctrl-C, for example at a question
        print("\ndatasheet-lens: stopped by the user", file=sys.stderr)
        return 130
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
