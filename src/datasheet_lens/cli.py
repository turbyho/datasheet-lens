"""Command line interface of datasheet-lens."""

import argparse
import contextlib
import math
import sys
from pathlib import Path

import pypdfium2 as pdfium

from . import __version__
from .config import data_lines, store_home
from .errors import LensError
from .store import ROLES, SOURCES, Store


def cmd_info(_args: argparse.Namespace) -> int:
    """The command info: show the version, the store folder, the data folder (if
    it is a different one), and a newer release on PyPI (it asks PyPI each time,
    and waits for the answer a few seconds at most)."""
    from .version_check import TIMEOUT, start_update_check, user_defects, user_notes

    print(f"datasheet-lens {__version__}")
    print(f"store: {store_home()}")
    code = 0
    try:  # a wrong data folder is a line: info shows the other state too
        for line in data_lines():
            print(line)
    except LensError as e:
        print(f"datasheet-lens: error: {e}", file=sys.stderr)
        code = 2
    thread = start_update_check(force=True)
    if thread is not None:
        thread.join(TIMEOUT)
        if thread.is_alive():  # TIMEOUT is for each socket step, thus the request can take longer
            print(f"datasheet-lens: warning: the check of the newest release did not end in {TIMEOUT:g} s: "
                  "the newest release is not known", file=sys.stderr)
    for note in user_notes():
        print(note)
    for line in user_defects():
        print(line, file=sys.stderr)
    return code


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
    from .version_check import user_defects, user_notes

    project = load(args.path)
    with Store() as store:
        report = sync(project, store, apply=False)
    print("\n".join(user_notes() + format_parts(project) + format_report(report, False, PROJECT_ADD_HINT)))
    for line in user_defects():
        print(line, file=sys.stderr)
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
    """The command key: save, remove or show the API keys of the distributors and
    the token of the team server."""
    import getpass

    from .keys import GROUPS, KEYS, delete_key, key_source, key_sources, set_key

    if not args.name:
        if args.delete:
            raise LensError("give the key to remove, for example: datasheet-lens key mouser --delete")
        for name, where in key_sources().items():
            print(f"{name}: {where}")
        return 0
    name = args.name.strip().lower()
    if name not in KEYS and name not in GROUPS:
        raise LensError(f"unknown key {args.name!r}: {', '.join([*KEYS, *GROUPS])}")
    # A group (digikey) is more keys: the command asks for each one.
    items = list(GROUPS[name]) if name in GROUPS else [(name, "token" if name == "team" else "API key")]
    if args.delete:
        for key, _ in items:
            delete_key(key)
            print(f"{key}: removed from the key store and the file")
            where = key_source(key)
            if where != "not set":  # an environment variable, or a key store that did not answer
                print(f"{key}: a key is still in {where}")
        return 0
    for key, label in items:
        # Not on the screen and not in the shell history.
        value = getpass.getpass(f"{name} {label}: ") if sys.stdin.isatty() else sys.stdin.readline()
        print(f"{key}: saved in {set_key(key, value)}")
        if key_source(key).startswith("environment"):
            print(f"{key}: the environment variable {KEYS[key]} comes first: remove it, else the saved key is not "
                  "used")
    from .distributors import SUPPORTED, enabled

    names, source, _ = enabled(None)
    if name in SUPPORTED and name not in names:
        # A distributor list that the user set before does not get the new distributor.
        print(f"{name} is not in the distributor list ({source}): run \"datasheet-lens dist "
              f"{' '.join([*names, name])}\" to use it")
    return 0


def cmd_server(args: argparse.Namespace) -> int:
    """The command server: connect the store to a team server, sync, show the
    state, or disconnect (see doc/team-server.md)."""
    from . import teamsync

    with Store() as store:
        if args.action == "connect":
            if not args.url:
                raise LensError("give the URL of the team server, for example: datasheet-lens server connect "
                                "https://dslens.example.com")
            for line in teamsync.connect(store, args.url):
                print(line)
        elif args.action == "sync":
            print(teamsync.sync(store).line())
        elif args.action == "disconnect":
            print(f"disconnected; {teamsync.disconnect(store)} local changes that the server did not get are not "
                  "sent (the local rows stay)")
        else:
            s = teamsync.state(store)
            if not s["server"]:
                print("not connected to a team server (datasheet-lens server connect URL)")
                return 0
            print(f"team server: {s['server']}")
            print(f"last complete sync: {s['last_sync'] or 'never'}; local changes that wait: {s['waiting']}")
            if s["last_error"]:
                print(f"the last sync failed: {s['last_error']}")
            for line in teamsync.notes(store):
                print(f"note: {line}")
    return 0


def cmd_dist(args: argparse.Namespace) -> int:
    """The command dist: show or set the distributors that datasheet-lens asks,
    globally or (--local) for the project."""
    from .distributors import check_locale, check_names, set_locale, status_lines
    from .project import load, load_optional, set_distributors
    from .config import load_config
    from .setup import save_config

    if args.site == "" or args.currency == "":
        raise LensError("--site and --currency need a value, for example --site CZ --currency CZK")
    check_locale(args.site, args.currency)
    if args.local:
        if args.site or args.currency:
            raise LensError("--site and --currency are global settings: give them without --local")
        project = load(args.path)
        set_distributors(project, check_names(args.names) if args.names else None)
        print(f"project {project.root}: " + (f"distributors {', '.join(project.distributors)}"
                                            if project.distributors is not None else "the global configuration"))
    else:
        if args.names:
            cfg = load_config(strict=True)  # a file that cannot be read is not written over
            cfg["distributors"] = check_names(args.names)
            save_config(cfg)
            print(f"global: distributors {', '.join(cfg['distributors'])}")
        if args.site or args.currency:
            print("\n".join(set_locale(args.site or "", args.currency or "")))
    print("\n".join(status_lines(load_optional(args.path))))
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """The command find: the parts at the distributors (official APIs)."""
    from .distributors import search_lines
    from .project import load_optional

    print("\n".join(search_lines(args.mpn, args.exact, load_optional(args.path))))
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """The command adopt: a part of a source, with its datasheet and its purchase source."""
    from .adopt import adopt
    from .elements import build_elements
    from .project import load_optional, sync
    from .setup import element_backend

    project = load_optional(args.path)
    result = adopt(args.source, args.number, args.mpn, args.base, args.category, project)
    print("\n".join(result["lines"]))
    with Store() as store:
        if result["project"] is not None:  # as the command add: the copy in the document folder
            for text in sync(result["project"], store)["problems"]:
                print(f"problem: {text}")
        if result["document"] is not None:
            built = build_elements(store, result["document"], element_backend())
            if not built["skipped"]:
                print(f"element map: {built['elements']} elements ({built['backend']})")
    return 0


def cmd_jlc(args: argparse.Namespace) -> int:
    """The command jlc: the JLCPCB parts library on this machine."""
    from . import jlcpcb

    if args.action == "update":
        print(jlcpcb.LIMITS)
        print(f"download {jlcpcb.DATA_URL}/ into {jlcpcb.folder()} (276 MB, 2.2 GB on the disk)")
        s = jlcpcb.update(progress=lambda line: print(line, flush=True))
        print(f"done in {s['seconds']} s: {jlcpcb.db_path()} ({s['size'] // (1 << 20)} MB)")
    elif args.action == "remove":
        print("removed" if jlcpcb.remove() else "there is no JLCPCB database on this machine")
        return 0
    s = jlcpcb.state()
    if not s:
        print("there is no JLCPCB database on this machine: run \"datasheet-lens jlc update\"")
        return 0
    print(f"JLCPCB database: {s['path']}, {s['age_days']:.1f} days old")
    note = jlcpcb.age_note()
    if note:
        print(note)
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    """The command pick: the parametric search at DigiKey or in the JLCPCB parts library."""
    from .distributors import parameter_lines, pick_lines
    from .project import load_optional

    project = load_optional(args.path)
    if args.params:
        if args.filters:
            raise LensError("-P shows the parameters of a category: give no FILTER with it")
        lines = parameter_lines(args.source, args.category, project)
    else:
        lines = pick_lines(args.source, args.category, args.filters, args.keywords, not args.all, args.sort,
                           args.limit, args.offset, project)
    print("\n".join(lines))
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
    from .distributors import link_lines

    # The global distributor list: the links do not need a project file.
    print("\n".join(link_lines(args.mpn)))
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


def cmd_check(args: argparse.Namespace) -> int:
    """The command check: compare the project with a BOM file (CSV) of the CAD
    project, for example from "kicad-cli sch export bom"."""
    from .project import bom_files, check_parts, format_check, load

    reads = bom_files(args.bom, args.column or "", args.delimiter, args.quote,
                      {"column": "--column", "delimiter": "--delimiter", "quote": "--quote",
                       "confirmed": "--confirmed-column"}, args.confirmed_column or "")
    with Store() as store:
        result = check_parts(load(args.path), store, [s for r in reads for s in r.cad])
    for read in reads:
        print(read.summary)
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
    for text in part.pop("notes", []):
        print(text)
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
    ck.add_argument("bom", metavar="BOM.csv", nargs="+",
                    help="the BOM, for example from kicad-cli sch export bom; more files: one for each variant of "
                         "the product, the default variant first (the parts of all files)")
    ck.add_argument("--column", help="the column of the MPN (default: MPN, Manufacturer Part Number (1) ...); "
                                     "the tool always reads the column Value too")
    ck.add_argument("--delimiter", default="", metavar="D", help="the delimiter of the values: a text or 'tab' (default: the "
                                        "one of , ; tab | that splits the header line)")
    ck.add_argument("--quote", default="", metavar="Q",
                    help="the quote: \" (default, also when empty), ' (the Semicolons preset of KiCad), none (its "
                         "TSV preset), or an other text of a preset")
    ck.add_argument("--confirmed-column", metavar="NAME",
                    help="the column of the field Value_Confirmed, for example an unused custom column of the BOM "
                         "of an other CAD tool (default: Value_Confirmed)")
    ck.set_defaults(func=cmd_check)
    cd = sub.add_parser("codes", help="show the orderable codes of a datasheet")
    cd.add_argument("document", metavar="DOC", help="document ID or SHA-256 prefix")
    cd.set_defaults(func=cmd_codes)
    lk = sub.add_parser("links", help="show search links for a part: datasheet, stock, symbol, footprint, 3D model")
    lk.add_argument("mpn", metavar="MPN")
    lk.set_defaults(func=cmd_links)
    fd = sub.add_parser("find", help="find a part at the distributors: concrete MPNs, state, stock, prices "
                                     "(needs a key: datasheet-lens key mouser or digikey)")
    fd.add_argument("mpn", metavar="MPN", help="an MPN, or the start of the MPNs (a base part)")
    fd.add_argument("-x", "--exact", action="store_true", help="only this MPN")
    fd.set_defaults(func=cmd_find)
    pk = sub.add_parser("pick", help="find parts by their parameters: at DigiKey (needs a key: datasheet-lens key "
                                     "digikey) or in the JLCPCB parts library (needs: datasheet-lens jlc update)")
    pk.add_argument("source", metavar="SOURCE", choices=("digikey", "jlcpcb"),
                    help="digikey (stock and lifecycle at DigiKey) or jlcpcb (JLCPCB stock for the assembly and "
                         "library class)")
    pk.add_argument("category", metavar="CATEGORY",
                    help="the category: its name or a part of it, for example \"Single FETs, MOSFETs\" or mosfet "
                         "(a text that fits more categories gives the list to select from)")
    pk.add_argument("filters", nargs="*", metavar="FILTER",
                    help="NAME OP VALUE, with OP one of = < <= > >= has, for example \"Vdss >= 20 V\" or "
                         "\"package has 3x3\". With no FILTER and -P: the parameters of the category")
    pk.add_argument("-P", "--params", action="store_true", help="show the parameters of the category and their "
                                                                "values")
    pk.add_argument("-k", "--keywords", default="", help="words that the parts must have")
    pk.add_argument("-a", "--all", action="store_true", help="also the parts with no stock")
    pk.add_argument("-s", "--sort", choices=("stock", "price"), default="stock", help="default: stock")
    pk.add_argument("-n", "--limit", type=int, default=20, help="the number of parts (1 to 50, default 20)")
    pk.add_argument("-o", "--offset", type=int, default=0, help="skip this number of parts (the next page)")
    pk.set_defaults(func=cmd_pick)
    ad = sub.add_parser("adopt", help="take a part of a source into the store and the project: its datasheet, "
                                      "the part and its purchase source. Give NUMBER or --mpn")
    ad.add_argument("source", metavar="SOURCE", choices=SOURCES,
                    help="digikey, jlcpcb or mouser")
    ad.add_argument("number", metavar="NUMBER", nargs="?", default="",
                    help="the part number of the source: a DigiKey number, a JLCPCB C number (C47616) or a Mouser "
                         "number")
    ad.add_argument("-m", "--mpn", default="", help="the MPN, in place of NUMBER")
    ad.add_argument("-b", "--base", default="", help="the base part of the datasheet (for example TLV755P)")
    ad.add_argument("-c", "--category", default="", help="the function of the part, for example MOSFET")
    ad.add_argument("-p", "--path", "--project", dest="path",
                    help="the project folder (default: the current folder or a parent folder)")
    ad.set_defaults(func=cmd_adopt)
    jl = sub.add_parser("jlc", help="the JLCPCB parts library on this machine: download or update it, show its "
                                    "state, or remove it")
    jl.add_argument("action", nargs="?", default="status", choices=("update", "status", "remove"),
                    help="default: status. update downloads 276 MB and needs 2.2 GB on the disk")
    jl.set_defaults(func=cmd_jlc)
    ds = sub.add_parser("dist", help="show or set the distributors that datasheet-lens asks (global, or with "
                                     "--local for the project)")
    ds.add_argument("names", nargs="*", metavar="NAME", help="the distributors, for example mouser")
    ds.add_argument("--local", action="store_true",
                    help="set them in the project file; with no NAME, the project uses the global setting")
    ds.add_argument("--site", metavar="CC", help="the country of the prices and the stock (global), for example "
                                                 "CZ (default US). DigiKey uses it")
    ds.add_argument("--currency", metavar="CUR", help="the currency of the prices (global), for example CZK "
                                                      "(default USD). DigiKey uses it")
    ds.add_argument("-p", "--path", "--project", dest="path", help="the project folder (default: the current folder or a parent "
                                                      "folder)")
    ds.set_defaults(func=cmd_dist)
    sv = sub.add_parser("server", help="the team server: connect the store, sync, show the state, or disconnect")
    sv.add_argument("action", nargs="?", default="status", choices=("connect", "sync", "status", "disconnect"),
                    help="default: status")
    sv.add_argument("url", nargs="?", help="connect: the URL of the team server (https://...)")
    sv.set_defaults(func=cmd_server)
    ky = sub.add_parser("key", help="save the API key of a distributor or the token of the team server (asks for "
                                     "it), or show where the keys are")
    ky.add_argument("name", nargs="?", metavar="NAME", help="mouser or digikey (a distributor; digikey-id or "
                                                             "digikey-secret: one part of the DigiKey key), or team "
                                                             "(the token of the team server). Empty: show the keys")
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
    for p in (rm, st, ck, sk, fd, pk, ls, psync_short):
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
