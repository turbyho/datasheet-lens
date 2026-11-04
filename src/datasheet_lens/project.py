"""Project: the parts of a user project, with references to their documents in
the central store, and copies of the documents in the folder of the project.

The project file `datasheet-lens.toml` is in the root of the project. The central
store is the source: sync copies the documents into the folder of the project
(default `datasheets/`). A new PDF that a user puts into the folder goes into the
central store. sync adds it to a part only when the store links the document to
a part of the project. For a different file, sync proposes a part from the file
name, and the user (or the model) assigns it with add_part.

save() keeps the comments, the format and the other keys of the project file.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import shutil
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import pypdfium2 as pdfium
import tomlkit
import tomlkit.exceptions
from tomlkit.container import Container
from tomlkit.items import AoT, Comment, Whitespace

from . import PROJECT_URL, makers
from .errors import LensError
from .fileutil import temp_path, write_atomic
from .store import ROLES, Document, Store, sha256_file

PROJECT_FILE = "datasheet-lens.toml"
DEFAULT_FOLDER = "datasheets"
# The subfolder of a copy in the folder: a pattern with {manufacturer},
# {category} (the function of the part) and {mpn}. "" puts all copies into the
# folder itself. A missing value gives the subfolder UNKNOWN.
DEFAULT_LAYOUT = "{manufacturer}"
LAYOUT_KEYS = ("manufacturer", "category", "mpn")
UNKNOWN = "_unknown"
# The comment at the start of a new project file.
HEADER = (
    "datasheet-lens project file: the parts of this project and their documents.",
    "",
    "datasheet-lens gives component datasheets (PDF) to LLM agents: an index of",
    "each document, and the original pages and regions on demand.",
    f"Source and documentation: {PROJECT_URL}",
    f"Install: pipx install git+{PROJECT_URL}.git",
    "Then run \"datasheet-lens init\" one time, and \"datasheet-lens project sync\"",
    "in the folder of this file.",
    "",
    "The central store holds the documents. \"datasheet-lens project sync\" copies",
    "them into the folder, and imports a new PDF of the folder into the store.",
)


def _rel_parts(rel: str, what: str) -> tuple[str, ...]:
    """The parts of a relative path with "/". The path must not go out of its base."""
    if not isinstance(rel, str) or not rel.strip() or "\\" in rel:
        raise LensError(f"{what} {rel!r} must be a relative path with '/' and no '..'")
    p = PurePosixPath(rel)
    if p.is_absolute() or PureWindowsPath(rel).drive or ".." in p.parts or not p.parts:
        raise LensError(f"{what} {rel!r} must be a relative path with '/' and no '..'")
    return p.parts


def _rel(rel: str, what: str) -> str:
    """The normal form of a relative path ("./a//b.pdf" gives "a/b.pdf")."""
    return "/".join(_rel_parts(rel, what))


@dataclass
class Project:
    """A user project: its root folder, the folder of the copies, the parts with their
    document references, the layout of the subfolders, and the text of the project
    file that load() read or save() wrote."""
    root: Path
    folder: str = DEFAULT_FOLDER
    # {"mpn", "manufacturer", "documents": [{"sha256", "file", "role"} and
    # "pinned": True for a file that the user put into the folder: sync does not
    # move it]}
    parts: list[dict] = field(default_factory=list)
    layout: str = DEFAULT_LAYOUT
    # The text of the project file that load() read or save() wrote.
    text: str | None = field(default=None, repr=False, compare=False)
    # The distributors that the project asks (the key distributors), or None:
    # the global configuration. save() keeps the key; set_distributors() writes it.
    distributors: list[str] | None = field(default=None, compare=False)

    @property
    def path(self) -> Path:
        """The project file."""
        return self.root / PROJECT_FILE

    @property
    def folder_path(self) -> Path:
        """The folder of the copies."""
        return self.root.joinpath(*_rel_parts(self.folder, "the folder"))

    def file_path(self, rel: str) -> Path:
        """The path of a file in the folder (rel is relative to the folder)."""
        return self.folder_path.joinpath(*_rel_parts(rel, "the file"))

    def find(self, mpn: str, manufacturer: str = "", store: Store | None = None) -> dict | None:
        """The part with this MPN (and this manufacturer, if given), or None. With
        the store, two names of one manufacturer are the same (makers.same)."""
        for part in self.parts:
            if part["mpn"].lower() == mpn.lower() and (
                    not manufacturer or not part["manufacturer"]
                    or makers.same(store.db if store else None, part["manufacturer"], manufacturer)):
                return part
        return None


def find_project(start: Path | str | None = None) -> Path:
    """The root of the project: the folder start, or its nearest parent folder,
    that holds the project file."""
    start = Path(start or Path.cwd()).expanduser().resolve()
    for folder in (start, *start.parents):
        if (folder / PROJECT_FILE).is_file():
            return folder
    raise LensError(f"no {PROJECT_FILE} in {start} or in a parent folder "
                    "(make one with the command: datasheet-lens project init)")


def init_project(root: Path | str, folder: str = DEFAULT_FOLDER, layout: str = DEFAULT_LAYOUT) -> Project:
    """Make the project file and the folder of the copies in root. A LensError if
    the project file is there already."""
    root = Path(root).expanduser().resolve()
    if (root / PROJECT_FILE).exists():
        raise LensError(f"{root / PROJECT_FILE} exists already")
    project = Project(root, _rel(folder, "the folder"), layout=_check_layout(layout))
    project.folder_path.mkdir(parents=True, exist_ok=True)
    save(project)
    return project


def _check_layout(layout) -> str:
    """Give the layout if it is valid: only {manufacturer}, {category} and {mpn},
    and a relative path. Else a LensError."""
    if not isinstance(layout, str):
        raise LensError("layout must be a text, for example \"{manufacturer}\"")
    names = ", ".join("{" + k + "}" for k in LAYOUT_KEYS)
    try:
        keys = [name for _, name, _, _ in string.Formatter().parse(layout) if name is not None]
    except ValueError as e:
        raise LensError(f"layout {layout!r}: {e} (use {names})") from e
    if any(k not in LAYOUT_KEYS for k in keys):
        raise LensError(f"layout {layout!r}: use only {names}")
    try:
        sample = layout.format_map({k: "x" for k in LAYOUT_KEYS})
    except (ValueError, KeyError, IndexError) as e:
        raise LensError(f"layout {layout!r}: {e} (use {names})") from e
    if sample.strip("/"):
        _rel(sample, "the layout")
    return layout


def _parse(text: str, path: Path) -> tuple[str, str, list[dict]]:
    """The folder, the layout and the parts of the text of a project file."""
    return _parse_data(_toml(text, path), path)


def _toml(text: str, path: Path) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LensError(f"{path}: {e}") from e


def _parse_data(data: dict, path: Path) -> tuple[str, str, list[dict]]:
    """The folder, the layout and the parts of the data of a project file."""
    folder = _rel(data.get("folder", DEFAULT_FOLDER), "the folder")
    try:
        # No layout key: a project file of a version with no layout. Its copies
        # stay where they are ("").
        layout = _check_layout(data.get("layout", ""))
    except LensError as e:
        raise LensError(f"{path}: {e}") from e
    parts = data.get("part", [])
    if not isinstance(parts, list):
        raise LensError(f"{path}: 'part' must be a list of tables ([[part]])")
    out = []
    for p in parts:
        if not isinstance(p, dict) or not isinstance(p.get("mpn"), str) or not p["mpn"].strip():
            raise LensError(f"{path}: each [[part]] needs an mpn")
        documents = p.get("document", [])
        if not isinstance(documents, list) or not all(isinstance(d, dict) for d in documents):
            raise LensError(f"{path}: part {p['mpn']}: 'document' must be a list of tables ([[part.document]])")
        docs = []
        for d in documents:
            sha, file, role = d.get("sha256"), d.get("file"), d.get("role", "datasheet")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise LensError(f"{path}: part {p['mpn']}: sha256 must have 64 hex digits")
            if role not in ROLES:
                raise LensError(f"{path}: part {p['mpn']}: role must be one of {', '.join(ROLES)}")
            if not isinstance(d.get("pinned", False), bool):
                raise LensError(f"{path}: part {p['mpn']}: pinned must be true or false")
            doc = {"sha256": sha, "file": _rel(file, "the file"), "role": role}
            if d.get("pinned"):
                doc["pinned"] = True
            docs.append(doc)
        part = {"mpn": p["mpn"].strip(), "manufacturer": str(p.get("manufacturer", "")),
                "category": str(p.get("category", "")).strip(), "documents": docs}
        variants = p.get("variants", [])
        if not isinstance(variants, list) or not all(isinstance(v, str) for v in variants):
            raise LensError(f"{path}: part {p['mpn']}: variants must be a list of MPNs")
        if variants:  # for information only: the store gives them
            part["variants"] = variants
        exclude = p.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s)
                                                    for s in exclude):
            raise LensError(f"{path}: part {p['mpn']}: exclude must be a list of SHA-256 values")
        if exclude:  # documents that the project does not want for this part
            part["exclude"] = exclude
        base = p.get("base", "")
        if not isinstance(base, str):
            raise LensError(f"{path}: part {p['mpn']}: base must be an MPN")
        if base.strip():  # mpn is a concrete orderable part of this base part
            part["base"] = base.strip()
        out.append(part)
    return folder, layout, out


def load(start: Path | str | None = None) -> Project:
    """Read the project file of the project that holds start (the current folder by default)."""
    root = find_project(start)
    path = root / PROJECT_FILE
    text = path.read_text(encoding="utf-8")
    data = _toml(text, path)
    folder, layout, parts = _parse_data(data, path)
    names = data.get("distributors")
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        raise LensError(f"{path}: distributors must be a list of names, for example [\"mouser\"]")
    return Project(root, folder, parts, layout, text=text, distributors=names)


def load_optional(start: Path | str | None = None) -> Project | None:
    """The project that holds start, or None when there is no project file. A
    project file with an error gives the error (not None)."""
    try:
        find_project(start)
    except LensError:
        return None
    return load(start)


def set_distributors(project: Project, names: list[str] | None) -> None:
    """Write the key distributors of the project file, or remove it (None: the
    project then uses the global configuration). The rest of the file stays."""
    text = project.path.read_text(encoding="utf-8")
    if project.text is not None and text != project.text:
        raise LensError(f"{project.path} changed after datasheet-lens read it: run the command again")
    doc = tomlkit.parse(text)
    if names is None:
        if "distributors" in doc:
            del doc["distributors"]
    else:
        doc["distributors"] = names
    new = tomlkit.dumps(doc)
    # Read back before the write: a key after a table belongs to the table.
    if tomllib.loads(new).get("distributors") != names:
        raise LensError(f"{project.path}: cannot write the key distributors (nothing changed)")
    if new != text:
        write_atomic(project.path, new.encode("utf-8"))
        project.text = new
    project.distributors = names


def _summary(folder: str, layout: str, parts: list[dict]):
    """The data of a project with no order, to compare two projects."""
    return folder, layout, sorted((p["mpn"], p["manufacturer"], p.get("category", ""), p.get("base", ""),
                                   tuple(p.get("variants", [])), tuple(sorted(p.get("exclude", []))), tuple(sorted((d["sha256"], d["file"], d["role"], d.get("pinned", False))
                                                for d in p["documents"]))) for p in parts)


def _strip(value) -> str:
    return str(value).strip()


def _norm_rel(value) -> str:
    try:
        return _rel(str(value), "the file")
    except LensError:
        return str(value)


# The sequence of the keys in the project file. A new key goes after the key
# before it in this sequence, not to the end of its table.
ROOT_KEYS = ("folder", "layout")
PART_KEYS = ("mpn", "manufacturer", "base", "category", "variants", "exclude")
DOCUMENT_KEYS = ("sha256", "file", "role", "pinned")


def _reorder(table, order: tuple[str, ...]) -> None:
    """Put the keys of order in a table into this sequence (a file of an older
    version, or of a person, can have a different one). A comment at the end of the
    line of a key moves with the key; other keys and lines do not move."""
    container = table if isinstance(table, Container) else table.value
    present = [k.key for k, _ in container.body if k is not None and k.key in order]
    wanted = [k for k in order if k in present]
    if present == wanted or not hasattr(container, "_insert_after"):  # tomlkit: no public API for this
        return  # a different tomlkit: the keys stay where they are
    for previous, key in zip(wanted, wanted[1:]):
        item = container.item(key)
        container.remove(key)
        try:
            container._insert_after(previous, key, item)
        except (AttributeError, KeyError, ValueError):
            container.append(key, item)  # the key stays in the table, at its end
            return


def _set(table, key: str, value: str, default: str = "", norm=str, order: tuple[str, ...] = ()) -> None:
    """Set a value only if it changed (norm gives the form to compare): an
    unchanged value keeps its format and its comment. A missing key with the
    default value is not added. A new key goes to its place in order."""
    if key in table and norm(table[key]) == value:
        return
    if key not in table and value == default:
        return
    if key not in table and key in order:
        before = [k for k in order[:order.index(key)] if k in table]
        if before:
            container = table if isinstance(table, Container) else table.value
            try:
                container._insert_after(before[-1], key, value)  # tomlkit: no public API for this
                return
            except (AttributeError, KeyError):
                pass  # a different tomlkit: the key goes to the end
    table[key] = value


def _same_part(table, part: dict) -> bool:
    """True if a table of the project file is this project part: the same MPN, and
    the same manufacturer or no manufacturer in the table. A part that
    update_part changed has its old MPN and manufacturer in "was"."""
    mpn, manufacturer = _strip(table.get("mpn", "")), _strip(table.get("manufacturer", ""))
    old_mpn, old_manufacturer = part.get("was", (part["mpn"], part["manufacturer"]))
    return mpn.lower() == old_mpn.lower() and (
        not manufacturer or manufacturer.lower() == old_manufacturer.strip().lower())


def _tables(container, key: str) -> list:
    """The tables of the array of tables container[key]. datasheet-lens cannot
    add to an inline array ("part = [{...}]") and keep its place in the text."""
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, AoT):
        raise LensError(f"{PROJECT_FILE}: write '{key}' as an array of tables ([[...]]), "
                        "not as an inline array, so that datasheet-lens can add to it")
    return list(value)


def _body(container) -> list:
    """The list of the items of a tomlkit table or document."""
    return container.body if isinstance(container, Container) else container.value.body


def _last_table(table):
    """The table whose text comes last in a table or a document (the last table
    of its last array of tables, at each depth)."""
    while True:
        body = _body(table)
        i = len(body) - 1
        while i >= 0 and body[i][0] is None and isinstance(body[i][1], (Comment, Whitespace)):
            i -= 1
        if i < 0 or not isinstance(body[i][1], AoT) or not len(body[i][1]):
            return table
        table = body[i][1][-1]


def _take_tail(table) -> list:
    """Remove the comment lines and blank lines at the end of the text of a
    table, and give them. tomlkit keeps the comment lines above a header there."""
    body = _body(_last_table(table))
    tail = []
    while body and body[-1][0] is None and isinstance(body[-1][1], (Comment, Whitespace)):
        tail.insert(0, body.pop()[1])
    return tail


def _set_tables(container, key: str, old: list, new: list) -> None:
    """Make the array of tables container[key] hold the tables of new. The array
    is changed in place: a kept table keeps its position, its text and its
    comments, and a new table comes at the end. The comment lines above a header
    stay above it. The order of the parts and of the documents has no meaning."""
    keep, before = {id(t) for t in new}, {id(t) for t in old}
    added = [t for t in new if id(t) not in before]
    aot = container.get(key)
    for i in reversed(range(len(old))):
        if id(old[i]) in keep:
            continue
        tail = _take_tail(old[i])
        following = next((t for t in old[i + 1:] if id(t) in keep), None)
        previous = next((t for t in reversed(old[:i]) if id(t) in keep), None)
        if following is not None:
            following.trivia.indent = "".join(x.as_string() for x in tail) + following.trivia.indent
        elif previous is not None:
            for x in tail:
                _last_table(previous).add(x)
        del aot[i]
    if not new:
        if key in container:
            del container[key]
        return
    if not added:
        return
    # The comment lines at the end of the text before the new tables are above
    # the header that comes after the array: they go after the new tables.
    tail = _take_tail(aot[-1] if aot is not None and len(aot) else container)
    if aot is None:
        container[key] = tomlkit.aot()
        aot = container[key]
    for t in added:
        t.trivia.indent = "\n"  # a blank line before the header
        aot.append(t)
    for x in tail:
        _last_table(added[-1]).add(x)


def _match_documents(old_docs: list, documents: list[dict]) -> list:
    """The table of each document: first the tables with the same content, then
    the tables with the same file (a new revision of the copy), then new tables."""
    free = list(old_docs)
    found: list = [None] * len(documents)
    for match in (lambda t, d: t.get("sha256") == d["sha256"],
                  lambda t, d: _norm_rel(t.get("file", "")) == d["file"]):
        for i, d in enumerate(documents):
            if found[i] is None:
                found[i] = next((t for t in free if match(t, d)), None)
                free = [t for t in free if t is not found[i]]
    return [t if t is not None else tomlkit.table() for t in found]


def save(project: Project) -> None:
    """Write the project file. The text of the file stays: the comments, the
    format and the keys that datasheet-lens does not know. Only the changed values
    and the new parts and documents are written. If the file changed after
    load() read it, nothing is written (the changes of the user stay). The new
    text is read back before the write: if it does not give the data of the
    project, nothing is written."""
    source = None
    if project.path.exists():
        text = project.path.read_text(encoding="utf-8")
        if project.text is not None and text != project.text:
            raise LensError(f"{project.path} changed after datasheet-lens read it: run the command again")
        source = text if text.endswith("\n") or not text else text + "\n"
        try:
            doc = tomlkit.parse(source)
        except tomlkit.exceptions.ParseError as e:
            raise LensError(f"{project.path}: {e}") from e
        _set(doc, "folder", project.folder, default=DEFAULT_FOLDER, norm=_norm_rel, order=ROOT_KEYS)
        _set(doc, "layout", project.layout, default="", order=ROOT_KEYS)
        _reorder(doc, ROOT_KEYS)
    elif project.text is not None:
        raise LensError(f"{project.path} was removed after datasheet-lens read it: run the command again")
    else:
        doc = tomlkit.document()
        for line in HEADER:
            doc.add(tomlkit.comment(line))
        doc.add(tomlkit.nl())
        doc.add("folder", project.folder)
        doc.add(tomlkit.comment("The subfolder of each copy: {manufacturer}, {category} (the function of"))
        doc.add(tomlkit.comment("the part) and {mpn}, for example \"{category}/{manufacturer}\". \"\": no"))
        doc.add(tomlkit.comment("subfolders. Sync moves the copies, but not a file with pinned = true."))
        doc.add("layout", project.layout)
    old_parts = _tables(doc, "part")
    used: set[int] = set()
    tables = []
    for part in project.parts:
        table = next((t for t in old_parts if id(t) not in used and _same_part(t, part)), None) or tomlkit.table()
        used.add(id(table))
        _set(table, "mpn", part["mpn"], norm=_strip, order=PART_KEYS)
        _set(table, "manufacturer", part["manufacturer"], norm=_strip, order=PART_KEYS)
        if part.get("base"):
            _set(table, "base", part["base"], norm=_strip, order=PART_KEYS)
        elif "base" in table:
            del table["base"]
        _set(table, "category", part.get("category", ""), norm=_strip, order=PART_KEYS)
        if part.get("variants"):
            _set(table, "variants", part["variants"], norm=lambda v: [str(x) for x in v], order=PART_KEYS)
        elif "variants" in table:  # the store gives no variants now
            del table["variants"]
        if part.get("exclude"):
            _set(table, "exclude", part["exclude"], norm=lambda v: [str(x) for x in v], order=PART_KEYS)
        elif "exclude" in table:
            del table["exclude"]
        _reorder(table, PART_KEYS)
        old_docs = _tables(table, "document")
        doc_tables = _match_documents(old_docs, part["documents"])
        for dt, d in zip(doc_tables, part["documents"]):
            _set(dt, "sha256", d["sha256"], order=DOCUMENT_KEYS)
            _set(dt, "file", d["file"], norm=_norm_rel, order=DOCUMENT_KEYS)
            _set(dt, "role", d["role"], default="datasheet", order=DOCUMENT_KEYS)
            _set(dt, "pinned", d.get("pinned", False), default=False, norm=bool, order=DOCUMENT_KEYS)
            _reorder(dt, DOCUMENT_KEYS)
        _set_tables(table, "document", old_docs, doc_tables)
        tables.append(table)
    _set_tables(doc, "part", old_parts, tables)
    text = tomlkit.dumps(doc)
    if text == source:  # no change: the file and its format stay as they are
        return
    try:
        same = _summary(*_parse(text, project.path)) == _summary(project.folder, project.layout, project.parts)
    except LensError:
        same = False
    if not same:
        raise LensError(f"{project.path}: the new text does not give the data of the project (internal "
                        "error); the file did not change")
    write_atomic(project.path, text.encode("utf-8"))
    project.text = text


# {path: (size, mtime_ns, SHA-256)}: a server process does not read a file again
# that did not change.
_hashes: dict[Path, tuple[int, int, str]] = {}


def _hash(path: Path) -> str:
    """The SHA-256 of a file; it is kept while the size and the time of the file stay."""
    st = path.stat()
    known = _hashes.get(path)
    if known and known[:2] == (st.st_size, st.st_mtime_ns):
        return known[2]
    sha = sha256_file(path)
    _hashes[path] = (st.st_size, st.st_mtime_ns, sha)
    return sha


def _folder_files(project: Project) -> dict[str, str]:
    """{relative file name: SHA-256} of the PDF files in the folder."""
    folder = project.folder_path
    if not folder.is_dir():
        return {}
    return {p.relative_to(folder).as_posix(): _hash(p)
            for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix.lower() == ".pdf"}


# Folders of a repository that do not hold the datasheets of the design.
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "build", "dist", "target"}
MAX_OUTSIDE = 1000  # the PDF files outside the project folder that sync looks at


def outside_pdfs(project: Project, store: Store) -> tuple[list[dict], bool]:
    """The folders of the repository (the project root) outside the project
    folder that hold PDF files: {"folder", "files", "in_store", "in_project"},
    and false when the search stopped after MAX_OUTSIDE files. A hidden folder
    (.git) and the folders of SKIP_DIRS are not searched. Only a file with the
    size of a store document is read (SHA-256)."""
    sizes = {r[0] for r in store.db.execute("SELECT DISTINCT size FROM documents")}
    known = {d["sha256"] for p in project.parts for d in p["documents"]}
    folder = project.folder_path.resolve()
    groups: dict[str, dict] = {}
    count = 0
    for dirpath, dirnames, filenames in os.walk(project.root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                             and (here / d).resolve() != folder)
        for name in sorted(n for n in filenames if n.lower().endswith(".pdf")):
            count += 1
            if count > MAX_OUTSIDE:
                return list(groups.values()), False
            path = here / name
            rel = here.relative_to(project.root).as_posix()
            group = groups.setdefault(rel, {"folder": rel, "files": 0, "in_store": 0, "in_project": 0})
            group["files"] += 1
            try:
                sha = _hash(path) if path.stat().st_size in sizes else ""
            except OSError:
                continue
            if sha and store.document(sha) is not None:
                group["in_store"] += 1
                group["in_project"] += sha in known
    return list(groups.values()), True


def _safe(name: str) -> str:
    """A name that is valid as a file or folder name on each system."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")


def layout_dir(project: Project, part: dict) -> str:
    """The subfolder of the copies of a part (relative to the folder), from the
    layout and the part in the project file (manufacturer, category, mpn). A
    missing value gives UNKNOWN."""
    values = {k: _safe(str(part.get(k, ""))) or UNKNOWN for k in LAYOUT_KEYS}
    text = project.layout.format_map(values)
    return "/".join(p for p in text.split("/") if p.strip() and p.strip() != ".")


def _used(project: Project) -> dict[str, str]:
    """{file name: SHA-256} of the references of the project."""
    return {d["file"]: d["sha256"] for part in project.parts for d in part["documents"]}


def _free_name(rel: str, sha: str, files: dict[str, str], used: dict[str, str]) -> str:
    """rel, or rel with a hash suffix if a different file of the folder or a
    reference to a different document has the name."""
    if files.get(rel, sha) == sha and used.get(rel, sha) == sha:
        return rel
    stem, dot, suffix = rel.rpartition(".")
    return f"{stem}-{sha[:8]}.{suffix}" if dot else f"{rel}-{sha[:8]}"


def _copy_name(project: Project, doc: Document, files: dict[str, str], subdir: str) -> tuple[str, bool]:
    """The file name of the copy of a document, and True if it is a file that the
    user put into the folder (it has the content, and no reference has the name).
    A copy that sync made for a different part is shared, not pinned."""
    used = _used(project)
    for rel, sha in files.items():
        if sha == doc.sha256:
            return rel, rel not in used
    name = _safe(doc.filename) or f"{doc.sha256[:12]}.pdf"
    return _free_name(f"{subdir}/{name}" if subdir else name, doc.sha256, files, used), False


def _refresh(project: Project, store: Store, part: dict, part_id: int, files: dict[str, str]) -> list[str]:
    """Add the documents that the store links to the part. Give their file names.
    A reference to a file that the user put into the folder gets pinned = true.
    The base part of a concrete part, and the variants of a base part (the parts
    with the same datasheet), come from the store."""
    base = store.base_of(part_id)
    if base is not None:
        part["base"] = base["mpn"]
    if part.get("base"):
        part.pop("variants", None)  # the variants belong to the base part
    else:
        variants = store.variants(part_id)
        if variants:
            part["variants"] = variants
        else:
            part.pop("variants", None)
    added = []
    refs = {d["sha256"]: d for d in part["documents"]}
    subdir = layout_dir(project, part)
    excluded = set(part.get("exclude", []))
    for row in store.documents_of_part(part_id):
        if row["sha256"] in excluded:  # the project does not want it (remove_document)
            continue
        if row["sha256"] in refs:
            refs[row["sha256"]]["role"] = row["role"]
            continue
        name, pinned = _copy_name(project, store.document(row["id"]), files, subdir)
        ref = {"sha256": row["sha256"], "file": name, "role": row["role"]}
        if pinned:
            ref["pinned"] = True
        part["documents"].append(ref)
        added.append(name)
    return added


def _entry(project: Project, store: Store, part_id: int, manufacturer: str = "") -> dict:
    """The project part of a store part (made if necessary). A new project part
    gets the manufacturer name of the caller: the store part can have a
    different name of the same manufacturer (TI and Texas Instruments), and
    the layout of the project uses the name as a folder. An empty manufacturer
    or category of the project part comes from the store."""
    row = store.part(part_id)
    part = project.find(row["mpn"], row["manufacturer"], store)
    if part is None:
        part = {"mpn": row["mpn"], "manufacturer": manufacturer.strip() or row["manufacturer"],
                "category": row["family"] or "", "documents": []}
        project.parts.append(part)
    _fill(part, row)
    return part


def _fill(part: dict, row) -> None:
    """Fill an empty manufacturer or category of a project part from the store.
    The project file holds them, so that each person of a team gets the same
    layout."""
    if not part["manufacturer"]:
        part["manufacturer"] = row["manufacturer"]
    if not part.get("category"):
        part["category"] = row["family"] or ""


def add_part(project: Project, store: Store, mpn: str, manufacturer: str = "", document: str = "",
             role: str = "datasheet", category: str = "", base: str = "") -> dict:
    """Add a part to the project, with the documents that the store links to it.
    A document (ID or SHA-256 prefix) is first linked to the part in the store.
    category: the function of the part (for example ADC), for the layout.
    base: mpn is a concrete orderable part (TLV75533PDBVR) of this base part
    (TLV755P); the document is then linked to the base part."""
    if role not in ROLES:
        raise LensError(f"role must be one of {', '.join(ROLES)}")
    doc = store.document(document) if document else None
    if document and doc is None:
        raise LensError(f"document not found: {document}")
    owner = None
    existing = store.match_parts(mpn)  # an existing concrete part, with any spelling of its manufacturer
    if base.strip() and base.strip().lower() != mpn.strip().lower():
        owner = store.find_part(base.strip(), manufacturer, create=True)
        if not manufacturer and len(existing) == 1:
            manufacturer = existing[0]["manufacturer"]
    if not manufacturer and len(existing) == 1:
        part_id = existing[0]["id"]
    else:
        part_id = store.find_part(mpn, manufacturer or (store.part(owner)["manufacturer"] if owner else ""),
                                  create=True)
    if manufacturer:
        store.fill_manufacturer(part_id, manufacturer)
    if owner is not None:
        store.set_base(part_id, owner)  # it also gives a part with no manufacturer the one of the base part
    else:
        owner = part_id
    if category.strip():
        store.set_category(part_id, category.strip())
    if doc is not None:
        store.link(owner, doc.id, role)
    part = _entry(project, store, part_id, manufacturer)
    if category.strip():
        part["category"] = category.strip()
    if doc is not None and doc.sha256 in part.get("exclude", []):
        # An explicit request adds a document that remove_document took away.
        part["exclude"] = [s for s in part["exclude"] if s != doc.sha256]
        if not part["exclude"]:
            del part["exclude"]
    _refresh(project, store, part, part_id, _folder_files(project))
    save(project)
    return part


def _prune_empty(project: Project, folder: Path) -> None:
    """Remove empty folders from folder up to the project folder. An error is no
    problem: the file operation is done."""
    with contextlib.suppress(OSError):
        while folder != project.folder_path and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent


def _drop_copies(project: Project, removed: list[dict], delete_pinned: bool, apply: bool) -> dict:
    """Delete the copies of removed references that no other reference of the
    project uses. A file that the user put into the folder (pinned) stays,
    unless delete_pinned is true. A file with a different content (for example a
    new revision that the user put there) stays. Give {"deleted": [...],
    "kept": [...]}."""
    used = _used(project)
    out = {"deleted": [], "kept": []}
    for d in {d["file"]: d for d in removed}.values():
        path = project.file_path(d["file"])
        if d["file"] in used:
            out["kept"].append(f"{d['file']} (a different part uses it)")
        elif d.get("pinned") and not delete_pinned:
            out["kept"].append(f"{d['file']} (pinned: you put it there; sync shows it as a file with no part; "
                               "delete_pinned deletes it)")
        elif path.is_file() and _hash(path) != d["sha256"]:
            out["kept"].append(f"{d['file']} (its content is not the document: not deleted)")
        elif path.is_file():
            if apply:
                path.unlink()
                _prune_empty(project, path.parent)
            out["deleted"].append(d["file"])
    return out


def _one_part(project: Project, mpn: str, manufacturer: str = "", store: Store | None = None) -> dict:
    """The project part with this MPN (and manufacturer, if more parts have it).
    With the store, two names of one manufacturer are the same (makers.same)."""
    found = [p for p in project.parts if p["mpn"].lower() == mpn.strip().lower()
             and (not manufacturer or makers.same(store.db if store else None, p["manufacturer"],
                                                  manufacturer.strip()))]
    if not found:
        raise LensError(f"the project has no part {mpn}")
    if len(found) > 1:
        raise LensError(f"the project has more parts {mpn}: give the manufacturer ("
                        + ", ".join(p["manufacturer"] or "unknown" for p in found) + ")")
    return found[0]


def remove_part(project: Project, mpn: str, manufacturer: str = "", delete_pinned: bool = False,
                apply: bool = True, store: Store | None = None) -> dict:
    """Remove a part from the project, and delete its copies (see _drop_copies).
    The central store does not change: other projects can use the part. If apply
    is false, nothing changes: the result tells what would change. store: its
    manufacturer registry finds the part by any name of its manufacturer."""
    part = _one_part(project, mpn, manufacturer, store)
    rest = [p for p in project.parts if p is not part]
    result = _drop_copies(Project(project.root, project.folder, rest, project.layout), part["documents"],
                          delete_pinned, apply)
    if apply:
        project.parts[:] = rest
        save(project)
    return {"part": part["mpn"], **result}


def _store_part(store: Store, mpn: str, manufacturer: str, create: bool) -> int | None:
    """The store part of an MPN: the one with this manufacturer or with none, or
    the only part with the MPN (a different spelling of the manufacturer). Else
    a new part if create is true, or None."""
    rows = store.match_parts(mpn)
    same = [r for r in rows if not r["manufacturer"] or store.same_maker(r["manufacturer"], manufacturer)]
    if len(same) == 1:
        return same[0]["id"]
    if len(rows) == 1 and not same:
        return rows[0]["id"]
    return store.find_part(mpn, manufacturer, create=True) if create else None


def update_part(project: Project, store: Store, mpn: str, manufacturer: str = "", new_mpn: str = "",
                base: str = "", category: str | None = None, new_manufacturer: str = "") -> dict:
    """Change a part of the project in place: its MPN (TMP112D0 to the concrete
    TMP112D0IDPWR), its base part, its category or its manufacturer. An empty
    value does not change; category "" removes the category. The part keeps its
    place in the project file and its copies: sync moves a copy when the layout
    gives a different subfolder. In the store, the part with the new MPN gets
    the base part; with no base part, it gets the documents of the old MPN. The
    manufacturer must be the same manufacturer as in the store (any of its names
    in the registry), so that the store does not get a second part with the same
    MPN."""
    part = _one_part(project, mpn, manufacturer, store)
    target = new_mpn.strip() or part["mpn"]
    maker = new_manufacturer.strip() or part["manufacturer"]
    if any(p is not part and p["mpn"].lower() == target.lower() and store.same_maker(p["manufacturer"], maker)
           for p in project.parts):
        raise LensError(f"the project has a part {target} already: remove one of the two parts")
    old_base = "" if part.get("base", "").lower() == part["mpn"].lower() else part.get("base", "")
    if base.strip() and base.strip().lower() == target.lower():
        raise LensError(f"{target} cannot be its own base part")
    base = base.strip() or ("" if old_base.lower() == target.lower() else old_base)
    # The checks of save, before the store changes: a store change stays when
    # the file cannot be written.
    if project.text is not None and (not project.path.exists()
                                     or project.path.read_text(encoding="utf-8") != project.text):
        raise LensError(f"{project.path} changed after datasheet-lens read it: run the command again")
    old_id = _store_part(store, part["mpn"], part["manufacturer"], create=False)
    part_id = _store_part(store, target, maker, create=True)
    known = store.part(part_id)["manufacturer"]
    if new_manufacturer.strip() and known and not store.same_maker(known, maker):
        raise LensError(f"the store has {target} from {known}: use this name, or record that the two names are one "
                        f"manufacturer (datasheet-lens maker alias \"{maker}\" \"{known}\", or the MCP tool "
                        f"manufacturer_relation)")
    if maker:
        store.fill_manufacturer(part_id, maker)
    if base:
        base_id = _store_part(store, base, maker, create=True)
        if (store.part(part_id)["base_id"] or 0) != base_id:
            store.set_base(part_id, base_id)
        part["base"] = base
    elif old_id is not None and old_id != part_id:
        # A new MPN with no base part: it gets the documents of the old MPN.
        for row in store.db.execute("SELECT document_id, role FROM part_documents WHERE part_id = ?",
                                    (old_id,)).fetchall():
            store.link(part_id, row["document_id"], row["role"])
    if category is not None:  # also in the store, else sync fills an empty category again
        store.set_category(part_id, category.strip())
        part["category"] = category.strip()
    # save finds the table of the part in the file by the old MPN and manufacturer.
    part["was"] = (part["mpn"], part["manufacturer"])
    part["mpn"], part["manufacturer"] = target, maker
    _refresh(project, store, part, part_id, _folder_files(project))
    try:
        save(project)
    finally:
        del part["was"]
    return part


def mpns_in_value(text: str) -> list[str]:
    """The MPNs in a Value field of a CAD symbol, in their order. The field can
    hold a value, a value and an MPN ("390uF (EEU-FS1H391L)", "10uF/10V
    (GRM188R61A106KE69D)"), an MPN, or more MPNs: alternatives with any separator
    ("AP2205-33YR-13 / AP7370-33Y-13", "A, B", "A | B"). Each character that an
    MPN does not have separates. Not an MPN: a value with a unit (100nF,
    4.7uF/10V, >=120R) or the name of a generic symbol (Conn_01x05_Socket,
    Battery LiPo - 1 cell). A slash with no spaces separates two MPNs only when
    the part after it is a full MPN with a digit: LM358DR/NOPB and
    MCP1827S-3302E/EB are one MPN, AP2205-33YR-13/AP7370-33Y-13 are two. Words of
    one part (OPA2134 (OPA2134PA)) give one MPN: the longest."""
    from .identify import _is_mpn

    def is_mpn(word: str) -> bool:
        # A word with "_" is the name of a generic symbol (USB_C_Receptacle_GCT_USB4105).
        return _is_mpn(word) and not re.search(r"_|^\W|^\d+(\.\d+)?\w*/", word)

    out: list[str] = []
    for word in re.split(r"[^A-Za-z0-9\-/.+#=_]+", text):
        word = word.strip("-/.")
        parts = word.split("/")
        if len(parts) > 1 and all(is_mpn(p) and len(p) >= MIN_ALTERNATIVE for p in parts):
            words = parts  # AP2205-33YR-13/AP7370-33Y-13
        else:
            words = [word]
        for w in words:
            if not is_mpn(w):
                continue
            same = next((i for i, o in enumerate(out) if _same_mpn(w, o)), None)
            if same is None:
                # A second MPN is a full code (not N16R8 of "ESP32-S3-WROOM-1 N16R8").
                if not out or len(w) >= MIN_ALTERNATIVE:
                    out.append(w)
            elif len(w) > len(out[same]):  # the longer code of one part
                out[same] = w
    return out


MIN_ALTERNATIVE = 6  # an alternative MPN in a Value field has this length or more


def _same_mpn(a: str, b: str) -> bool:
    """True if two MPNs of one symbol are probably one part (for a conflict, a
    part with no doubt is enough): the same with no regard to signs
    (LTC3588EMSE#PBF, LTC3588EMSEPBF); one starts with the other (TMP112D0 and
    TMP112D0IDPWR, TPS7A02 and TPS7A0233PYCHR); the base part of a TI code with
    digits in it (TLV755P and TLV75533PDBVR); or placeholders (see _near)."""
    ka, kb = _norm(a), _norm(b)
    if ka == kb:
        return True
    short, long = sorted((ka, kb), key=len)
    if len(short) >= 4 and long.startswith(short):
        return True
    start = len(os.path.commonprefix([ka, kb]))
    if start >= 4:
        digits = re.match(r"\d{1,3}", long[start:])
        if digits and long[start + digits.end():].startswith(short[start:]) and short[start:]:
            return True
    return _near(a, {"mpn": b})


def cad_mpns(fields: list[tuple[str, str]], value: str = "") -> tuple[list[str], str]:
    """The MPNs of one CAD symbol, the main one first, and a conflict text or "".
    fields: (name, text) of its MPN fields, the best first; value: its Value
    field. The first filled MPN field gives the main MPN; with none, the first
    MPN of the Value. The other MPNs of the Value are alternatives (a second
    source). A conflict: an MPN field or a Value that names a different part
    (in Eagle: a new Value, and the attributes of the old library part)."""
    named = [(n, t.strip()) for n, t in fields if t.strip()]
    in_value = mpns_in_value(value)
    if not named:
        return in_value, ""
    main = named[0][1]
    other_fields = [(n, m) for n, m in named[1:] if not _same_mpn(m, main)]
    alternatives = [m for m in in_value if not _same_mpn(m, main)]
    wrong = []
    if in_value and len(alternatives) == len(in_value):  # no MPN of the Value is the main part
        wrong = [("Value", m) for m in in_value]
        alternatives = []
    wrong = other_fields + wrong
    conflict = (f"{named[0][0]} {main}, " + ", ".join(f"{n} {m}" for n, m in wrong)
                + f". The check uses {main}") if wrong else ""
    return [main] + alternatives, conflict


def check_parts(project: Project, store: Store, cad: list[dict]) -> dict:
    """Compare the parts of the project with the parts of the CAD project. cad:
    one item for each CAD part: {"mpn", "datasheet" (optional), "reference"
    (optional)}, or {"mpns", "conflict"} of bom_parts (mpns can be empty: a line
    that gives only a conflict). Give:
    - ok: the MPNs that the project has.
    - different: {"cad", "mpn", "manufacturer", "ambiguous"} for a project part
      that is probably the same part with a different MPN (see _near).
      ambiguous: the project has more parts with this MPN.
    - missing: the CAD parts that the project does not have, with the
      references of the CAD symbols, the Datasheet field and the documents
      that the store has for the MPN.
    - extra: the project parts that the CAD project does not have.
    - conflicts: the symbols whose fields name different parts (see cad_mpns), or
      whose columns of one role are different (see bom_parts).
    An item can also give "value" (the Value field): the MPNs in it count (see
    cad_mpns). An alternative is a CAD part too, and needs its own datasheet: the
    Datasheet field of the symbol belongs to the main part."""
    merged: dict[str, dict] = {}
    conflicts = []
    for item in cad:
        reference = str(item.get("reference", "")).strip()
        if "mpns" in item:  # computed by the caller (more MPN fields, see cad_mpns)
            mpns, conflict = list(item["mpns"]), str(item.get("conflict", ""))
        else:
            mpns, conflict = cad_mpns([("MPN", _filled(str(item.get("mpn", ""))))],
                                      _filled(str(item.get("value", ""))))
        if conflict:
            conflicts.append(f"{reference or 'a symbol'}: {conflict}")
        for i, mpn in enumerate(mpns):
            entry = merged.setdefault(mpn.lower(), {"mpn": mpn, "references": [], "datasheet": "",
                                                    "alternative_of": mpns[0] if i else ""})
            if i == 0:
                entry["alternative_of"] = ""  # a main part of a different symbol
            if reference and reference not in entry["references"]:
                entry["references"].append(reference)
            if i == 0 and not entry["datasheet"] and str(item.get("datasheet", "")).strip() not in ("", "~"):
                entry["datasheet"] = str(item["datasheet"]).strip()
    out = {"ok": [], "different": [], "missing": [], "extra": [], "conflicts": conflicts}
    # First the exact matches, then the near matches of the other parts: a part
    # that is the same is not also "different".
    exact = {id(p) for p in project.parts if p["mpn"].lower() in merged}
    out["ok"] = [e["mpn"] for k, e in merged.items() if any(p["mpn"].lower() == k for p in project.parts)]
    near_found: set[int] = set()
    for key, entry in merged.items():
        if entry["mpn"] in out["ok"]:
            continue
        near = [p for p in project.parts if id(p) not in exact and _near(entry["mpn"], p)]
        if near:
            near_found.update(id(p) for p in near)
            count = {p["mpn"].lower(): sum(q["mpn"].lower() == p["mpn"].lower() for q in project.parts) for p in near}
            out["different"] += [{"cad": entry["mpn"], "mpn": p["mpn"], "manufacturer": p["manufacturer"],
                                  "ambiguous": count[p["mpn"].lower()] > 1} for p in near]
            continue
        docs = store.part_documents(entry["mpn"])
        # The datasheet first: the next step links it, not an errata or a manual.
        docs = sorted(docs, key=lambda d: d["role"] != "datasheet")
        out["missing"].append({**entry, "documents": [d["sha256"][:12] for d in docs],
                               "bases": store.prefix_parts(entry["mpn"])[:3]})
    out["extra"] = [p["mpn"] for p in project.parts if id(p) not in exact | near_found]
    return out


def _near(cad: str, part: dict) -> bool:
    """True if the CAD MPN and the project part are probably one part with two
    MPNs: the CAD MPN is the base part or a variant of the part, or one MPN
    starts with the other (TMP112D0 and TMP112D0IDPWR). Placeholders at the end
    of the CAD MPN (nRF9151-LAXX, STM32F103C8Tx) are any characters. A digit
    after a shorter MPN that ends with a digit is a different part (LM358 and
    LM3580)."""
    key = cad.lower()
    if part.get("base", "").lower() == key or key in (v.lower() for v in part.get("variants", [])):
        return True
    stem = re.sub(r"(x+|X{2,})$", "", cad).lower() or key
    own = part["mpn"].lower()
    shorter, longer = (stem, own) if own.startswith(stem) else (own, key) if key.startswith(own) else ("", "")
    if len(shorter) < 4:
        return False
    rest = longer[len(shorter):]
    return not (shorter[-1].isdigit() and rest[:1].isdigit())


# The column names of a BOM file, the best first, as _bom_columns() compares them
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


def _column_key(name: str) -> str:
    """The name of a BOM column with no case, spaces, "_", "." and "-"."""
    return re.sub(r"[\s_.\-]", "", name).lower()


def _bom_columns(keys: list[str], name: str) -> list[int]:
    """The columns of a name. keys: the _column_key of each column name. The case,
    spaces, "_", "." and "-" do not count, thus more columns can have the name (for
    example the fields MPN and mpn of symbols from different libraries)."""
    key = _column_key(name)
    return [i for i, k in enumerate(keys) if k and k == key]


BOM_SUFFIXES = (".csv", ".tsv", ".txt")  # CSV text; Excel writes "Unicode text" as .txt
BOM_DELIMITERS = (",", ";", "\t", "|")  # the delimiters that the reader finds in the header
BOM_MAX_BYTES = 20 * 1024 * 1024  # a larger file is not a BOM


def _decode_bom(path: str) -> tuple[str, str]:
    """The text of a BOM file and its encoding: UTF-16 with its byte order mark
    (Excel "Unicode text"), UTF-8, or cp1252 (a BOM file from Windows) when it is
    not UTF-8."""
    try:
        with open(path, "rb") as f:
            data = f.read(BOM_MAX_BYTES + 1)
    except OSError as e:
        raise LensError(f"{path}: cannot read the BOM file: {e.strerror or e}") from e
    if len(data) > BOM_MAX_BYTES:
        raise LensError(f"{path}: the file is larger than {BOM_MAX_BYTES // 2**20} MB: it is not a BOM file")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            raise LensError(f"{path}: not valid UTF-16 text: export the BOM again") from None
    for encoding in ("utf-8", "cp1252"):
        try:
            # A UTF-8 byte order mark is not text of cp1252 either.
            return data.removeprefix(b"\xef\xbb\xbf").decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise LensError(f"{path}: not UTF-8 or cp1252 text: export the BOM as UTF-8")


def _filled(value: str) -> str:
    """The value with no outer spaces, or "" for an empty value of KiCad ("~")."""
    value = value.strip()
    return "" if value == "~" else value


def _references(text: str) -> frozenset[str]:
    """The references of a text, with no regard to case and to the separators: R1 R2,
    "R1, R2" and the range R1-R2 of KiCad give the same set."""
    out: set[str] = set()
    for word in re.split(r"[\s,;]+", text.upper()):
        m = re.fullmatch(r"([A-Z_#]+)(\d+)-\1?(\d+)", word)
        if m and int(m.group(2)) <= int(m.group(3)) <= int(m.group(2)) + 10000:
            out.update(f"{m.group(1)}{n}" for n in range(int(m.group(2)), int(m.group(3)) + 1))
        elif word:
            out.add(word)
    return frozenset(out)


def _option_names(options: dict[str, str] | None) -> dict[str, str]:
    """The names of the options column, delimiter and quote in the interface of the
    caller, with a general name for a name that the caller does not give."""
    return {key: (options or {}).get(key, f"the {key} option") for key in ("column", "delimiter", "quote")}


def bom_format(delimiter: str, quote: str, options: dict[str, str]) -> tuple[str, str]:
    """The delimiter and the quote of the options of an interface.

    The delimiter is empty (the reader finds it), "tab", or a text (a preset of
    KiCad can have a delimiter of more characters). The quote is '"' (also when
    empty), "none" (no quotes), or a text, for example "'" (the Semicolons preset
    of KiCad). options: the names of the options (see _option_names), for the
    errors."""
    delimiter_option = options["delimiter"]
    if quote.lower() == "none":
        quote = ""
    elif not quote:
        quote = '"'  # the default, also for an empty option
    if delimiter.lower() == "tab":
        delimiter = "\t"
    elif delimiter == "\\t":
        raise LensError(f"'\\t' is not a delimiter for {delimiter_option}: a tab is 'tab'")
    if any(c in "\r\n" for c in delimiter + quote):
        raise LensError(f"the delimiter and the quote cannot have a line end ({delimiter_option}, "
                        f"{options['quote']})")
    if delimiter and quote and (quote in delimiter or delimiter in quote):
        raise LensError(f"the delimiter {delimiter!r} and the quote {quote!r} must be different "
                        f"({delimiter_option}, {options['quote']})")
    return delimiter, quote


class _RecordError(Exception):
    """A record of a BOM file that does not follow RFC 4180: (the line, the reason)."""


QUOTE_NOT_CLOSED = "a quote that is not closed"
QUOTE_NOT_AROUND_VALUE = "a quote that is not around a full value"
DELIMITER_OVERLAP = "the delimiter can start at more than one place"
QUOTE_RULE = "a value between quotes starts directly after a delimiter and ends directly before the next one"
_LINE_END = re.compile(r"\r\n?|\n")


def _between(value: str, quote: str) -> bool:
    return len(value) >= 2 * len(quote) and value.startswith(quote) and value.endswith(quote)


def _records(text: str, delimiter: str, quote: str, pos: int = 0, line: int = 1):
    """The records of a BOM text from the offset pos (the start of the line line),
    by the rules of RFC 4180: (the line where the record starts, its values). A
    value with the quote, the delimiter or a line end is between quotes, and a
    quote in it is doubled. A line end is CRLF, LF or CR. quote "": no quotes.
    delimiter "": one column. The delimiter and the quote can have more
    characters. A record with no value that is not empty (an empty line, or a
    line with only delimiters) is not given.

    Raises:
        _RecordError: a record that does not follow the rules.
    """
    n = len(text)
    run_end = -1  # the end of the run of delimiters that the check of DELIMITER_OVERLAP did
    end_of_value = re.compile((re.escape(delimiter) + "|" if delimiter else "") + r"\r|\n")
    while pos < n:
        start, values = line, []
        while True:
            if quote and text.startswith(quote, pos):
                parts, pos = [], pos + len(quote)
                while True:
                    k = text.find(quote, pos)
                    if k < 0:
                        raise _RecordError(start, QUOTE_NOT_CLOSED)
                    parts.append(text[pos:k])
                    line += len(_LINE_END.findall(text, pos, k))
                    if not text.startswith(quote, k + len(quote)):
                        pos = k + len(quote)
                        break
                    parts.append(quote)
                    pos = k + 2 * len(quote)
                values.append("".join(parts))
                if pos < n and not (delimiter and text.startswith(delimiter, pos)) and text[pos] not in "\r\n":
                    raise _RecordError(start, QUOTE_NOT_AROUND_VALUE)
            else:
                m = end_of_value.search(text, pos)
                stop = m.start() if m else n
                value = text[pos:stop]
                if quote and quote in value:
                    raise _RecordError(start, QUOTE_NOT_AROUND_VALUE)
                values.append(value)
                pos = stop
            if delimiter and text.startswith(delimiter, pos):
                if len(delimiter) > 1 and pos >= run_end:
                    # A run of full delimiters is clear (empty values). A delimiter
                    # that starts inside the last one of the run ("|||" with "||")
                    # gives two readings.
                    run = pos
                    while text.startswith(delimiter, run):
                        run += len(delimiter)
                    if any(text.startswith(delimiter, run - len(delimiter) + k) for k in range(1, len(delimiter))):
                        raise _RecordError(start, DELIMITER_OVERLAP)
                    run_end = run
                pos += len(delimiter)
                continue
            m = _LINE_END.match(text, pos)
            pos = m.end() if m else n
            line += 1
            break
        if any(value.strip() for value in values):
            yield start, values


def _known_columns(column: str) -> set[str]:
    """The keys of the column names that the reader knows, and of the column of the caller."""
    names = MPN_COLUMNS + REFERENCE_COLUMNS + DNP_COLUMNS + DATASHEET_COLUMNS + ((column,) if column else ())
    return {_column_key(n) for n in names}


def _bom_delimiter(path: str, text: str, pos: int, line: int, quote: str, option: str) -> tuple[str, list[str]]:
    """The delimiter of a BOM file: the one of BOM_DELIMITERS that splits the header
    record into more than one value (a delimiter at the end of the line counts),
    or "" (one column) when none splits it. When more delimiters split it, the
    caller must give it. Also gives the errors of the delimiters that do not
    follow the rules of CSV in the header."""
    found, errors = [], []
    for d in BOM_DELIMITERS:
        if quote and (d in quote or quote in d):
            continue  # the quote of the file is not its delimiter
        try:
            header = next(_records(text, d, quote, pos, line), None)
        except _RecordError as e:
            errors.append(f"with {d!r}: {e.args[1]}")
            continue
        if header and len(header[1]) > 1:
            found.append(d)
    if len(found) > 1:
        raise LensError(f"{path}: the delimiters {' and '.join(map(repr, found))} split the header line: give the "
                        f"delimiter with {option}")
    return (found[0] if found else ""), errors


def _sep_line(path: str, line: str, number: int, quote: str) -> str:
    """The delimiter of the Excel line "sep=;", or "" when the line is not one. The
    line can be between quotes. LibreOffice can save the file again with
    delimiters after it ("sep=;;;", or an other delimiter: "sep=;",,), which give
    the delimiter of the file."""
    if not re.match(r"""\s*["']?sep=""", line, re.IGNORECASE):
        return ""
    marker = re.fullmatch(r"""\s*(["']?)sep=([^\w\r\n])\1(.*)""", line, re.IGNORECASE)
    sep, rest = (marker.group(2), marker.group(3)) if marker else ("", "")
    rest = rest if sep == " " else rest.strip(" ")
    if not marker or len(set(rest)) > 1 or (rest and rest[0] not in set(BOM_DELIMITERS) | {sep}):
        raise LensError(f"{path}: line {number}: the line 'sep=' of the file does not give one delimiter: correct or "
                        "remove the line")
    found = rest[0] if rest else sep
    if quote and found in quote:
        raise LensError(f"{path}: line {number}: the line 'sep=' of the file gives the delimiter {found!r}, which is "
                        f"the quote {quote!r}: correct the line in the file, or give an other quote")
    return found


def _read_bom(path: str, delimiter: str = "", quote: str = '"', column: str = "",
              options: dict[str, str] | None = None,
              quote_given: bool = False) -> tuple[list[str], list[list[str]], str]:
    """The column names, the rows (one value for each column) and the encoding of a
    BOM file: CSV (RFC 4180).

    quote: the quote, '"' (CSV, Excel, the CSV preset of KiCad), "'" (the
    Semicolons preset of KiCad), "" (no quotes: the TSV preset of KiCad), or an
    other text. delimiter: empty to find it (the Excel line "sep=;", else
    _bom_delimiter). column: the MPN column of the caller. See bom_format for the
    values of the options of the interfaces. options: the names of the options
    column, delimiter and quote (see _option_names), for the errors.
    quote_given: the caller gave the quote; else it is the default '"'.

    A header with one value is a file with one column only when the value is a
    known column name or the column of the caller. A record with fewer values than
    the header gets empty values (Excel can leave out the empty values at the end
    of a line). A record with more values gives an error when a value after the
    last column is not empty: a delimiter in a value with no quotes moves the
    columns. A column with no name is not used."""
    options = options or _option_names(None)
    delimiter_option, quote_option, column_option = options["delimiter"], options["quote"], options["column"]
    if Path(path).suffix.lower() not in BOM_SUFFIXES:
        raise LensError(f"{path}: a BOM file must be CSV text ({', '.join(BOM_SUFFIXES)}): export the BOM as CSV")
    text, encoding = _decode_bom(path)
    pos, first = 0, 1
    while True:  # the first line that is not empty (only spaces or delimiters)
        m = _LINE_END.search(text, pos)
        line = text[pos:m.start()] if m else text[pos:]
        if line.strip(" \t" + "".join(BOM_DELIMITERS)) or not m:
            break
        pos, first = m.end(), first + 1
    sep = _sep_line(path, line, first, quote)
    if sep:
        if delimiter and delimiter != sep:
            raise LensError(f"{path}: the file gives the delimiter {sep!r} (line {first}, 'sep='), but "
                            f"{delimiter_option} gives {delimiter!r}")
        delimiter = sep
        m = _LINE_END.search(text, pos)  # the header is after the line "sep="
        pos, first = (m.end() if m else len(text)), first + 1
    errors: list[str] = []
    try:
        if not delimiter:
            delimiter, errors = _bom_delimiter(path, text, pos, first, quote, delimiter_option)
        records = _records(text, delimiter, quote, pos, first)
        header = next(records, None)
        if header is None:
            return [], [], encoding
        first, names = header
        # The Semicolons preset of KiCad puts each name between "'": with the quote
        # '"', such a header means that the file uses the quote "'". (One name
        # between "'" is a valid name of CSV.)
        if not quote_given and all(_between(n.strip(), "'") for n in names if n.strip()):
            raise LensError(f"{path}: line {first}: each name of the header is between ': give the quote ' with "
                            f"{quote_option}")
        if len(names) == 1 and _column_key(names[0]) not in _known_columns(column):
            why = f" (by the rules of CSV: {'; '.join(errors)}; {QUOTE_RULE})" if errors else ""
            which = f"the delimiter {delimiter!r} does not split" if delimiter else "no delimiter of , ; tab | splits"
            raise LensError(f"{path}: line {first}: {which} the header line{why}. Give the "
                            f"delimiter of the file with {delimiter_option}, or the quote with {quote_option} (none for "
                            f"a file with no quotes); for a "
                            f"file with one column, give its name with {column_option}")
        rows: list[list[str]] = []
        for start, row in records:
            if any(value.strip() for value in row[len(names):]):
                raise LensError(f"{path}: line {start} has more values than the header line: a delimiter in a "
                                f"value with no quotes moves the columns; or give the delimiter with "
                                f"{delimiter_option}")
            rows.append(row[:len(names)] + [""] * (len(names) - len(row)))
    except _RecordError as e:
        line, reason = e.args
        if not delimiter:
            why = f" (by the rules of CSV: {'; '.join(errors)})" if errors else ""
            hint = (f"; no delimiter splits the header line{why}, thus the reader uses one column. {QUOTE_RULE}. "
                    f"Give the delimiter with {delimiter_option}, or the quote with {quote_option} (none for a "
                    "file with no quotes)")
        elif reason == DELIMITER_OVERLAP:
            hint = f" ({delimiter!r}): put the value between quotes, or give an other delimiter with {delimiter_option}"
        else:
            hint = (f"; {QUOTE_RULE}. For an other quote, give it with {quote_option}: ' (the Semicolons preset "
                    "of KiCad) or none (a file with no quotes: the TSV preset of KiCad, Eagle)")
        raise LensError(f"{path}: line {line} is not valid CSV: {reason}{hint}") from e
    return [n.strip() for n in names], rows, encoding


@dataclass
class BomParts:
    """The CAD parts of a BOM file, for check_parts."""
    cad: list[dict]  # one item for each fitted line with an MPN or a conflict (mpns can be empty)
    summary: str  # one line: the columns that the parts come from, and the number of lines with an MPN


def bom_parts(path: str, column: str = "", delimiter: str = "", quote: str = "",
              options: dict[str, str] | None = None) -> BomParts:
    """Read the CAD parts of a BOM file (CSV), for example from "kicad-cli sch
    export bom". column: the column of the MPN; empty: each column of
    MPN_COLUMNS that the BOM has, each one an MPN field for cad_mpns (also more
    columns of one name, for example MPN and mpn of symbols from different
    libraries). The Value column is always read (alternatives, conflicts). A line
    with a value in the DNP column is not fitted: it gives no part. The values
    empty, 0, no and false mean fitted.

    More Value, DNP, Reference or Datasheet columns (also with different names of
    one list, for example DNP and "Do not populate"): the first filled one gives
    the value, and a line in which they are different gets a conflict. A line is
    not fitted only when each DNP column says so; thus the line of a conflict
    needs its datasheet. All MPNs of more Value columns count.

    delimiter, quote: the values of the options of the interface (see
    bom_format). options: the names of the options column, delimiter and quote in
    the interface of the caller, for the errors."""
    options = _option_names(options)
    quote_given = bool(quote)
    delimiter, quote = bom_format(delimiter, quote, options)
    names, rows, encoding = _read_bom(path, delimiter, quote, column, options, quote_given)
    if not names:
        raise LensError(f"{path}: the BOM file has no header line: export the BOM again")
    # A name that more columns have gets the number of its column.
    labels = [f"{n} (column {i + 1})" if n and names.count(n) > 1 else n for i, n in enumerate(names)]
    shown = [label for label in labels if label]
    roles = {"Value": ["Value"], "DNP": DNP_COLUMNS, "Reference": REFERENCE_COLUMNS, "Datasheet": DATASHEET_COLUMNS}
    # Each column of each name of a role, for example DNP and "Do not populate".
    keys = [_column_key(n) for n in names]
    found = {role: [i for w in wanted for i in _bom_columns(keys, w)] for role, wanted in roles.items()}
    value_columns = found["Value"]
    if column:
        matched = _bom_columns(keys, column)
        if not matched:
            raise LensError(f"{path}: no column {column!r} ({', '.join(shown)})")
        field_columns = [i for i in matched if i not in value_columns]
    else:
        field_columns = [i for w in MPN_COLUMNS if _column_key(w) != "value"
                         for i in _bom_columns(keys, w) if i not in value_columns]
    columns = list(dict.fromkeys(labels[i] for i in field_columns + value_columns))
    if not columns:
        hint = (f"; if the file uses the quote ', give it with {options['quote']}"
                if quote == '"' and any(n.strip().startswith("'") for n in names) else "")
        raise LensError(f"{path}: no MPN column; give the column of the MPN with {options['column']} "
                        f"({', '.join(shown)}){hint}")

    # How the values of more columns of one role compare: a value with no regard to
    # case and spaces (100nF, 100 nF), the references as a set (R1 R2, "R1, R2"),
    # a datasheet as text.
    compare = {"Value": lambda v: re.sub(r"\s+", "", v.lower()),
               "Reference": _references,
               "Datasheet": lambda v: v}

    def role(row: list[str], name: str) -> tuple[str, str]:
        """The first filled value of the columns of a role, and the conflict text
        when two filled values are different."""
        filled = [(labels[i], _filled(row[i])) for i in found[name] if _filled(row[i])]
        conflict = len({compare[name](v) for _, v in filled}) > 1
        return (filled[0][1] if filled else ""), (", ".join(f"{label} {v}" for label, v in filled) if conflict else "")

    def fitted(value: str) -> bool:
        return _filled(value).lower() in ("", "0", "no", "false")

    cad = []
    for row in rows:
        states = [fitted(row[i]) for i in found["DNP"]]
        if states and not any(states):
            continue  # not fitted
        # The MPN fields, and each Value column: a Value also holds values of
        # passive parts and alternatives (see cad_mpns).
        values = [(labels[i], row[i]) for i in value_columns]
        # Each MPN of each Value column counts (more Value columns: see role).
        value = "; ".join(_filled(text) for _, text in values if _filled(text))
        mpns, conflict = cad_mpns([(labels[i], _filled(row[i])) for i in field_columns], value)
        # Two Value columns are different when their values are different, and when
        # they are not one part (TLV755P and TLV75533PDBVR are one part).
        filled = [(label, _filled(text)) for label, text in values if _filled(text)]
        found_mpns = [mpns_in_value(text) for _, text in filled]

        def one_part(a: int, b: int) -> bool:
            ma, mb = found_mpns[a], found_mpns[b]
            return len(ma) == len(mb) == 1 and _same_mpn(ma[0], mb[0])

        value_conflict = ", ".join(f"{label} {text}" for label, text in filled) if any(
            compare["Value"](filled[a][1]) != compare["Value"](filled[b][1]) and not one_part(a, b)
            for a in range(len(filled)) for b in range(a + 1, len(filled))) else ""
        reference, reference_conflict = role(row, "Reference")
        datasheet, datasheet_conflict = role(row, "Datasheet")
        # An empty DNP value is a state too (fitted).
        dnp_conflict = (", ".join(f"{labels[i]} {_filled(row[i]) or '(empty)'}" for i in found["DNP"])
                        if len(set(states)) > 1 else "")
        conflicts = [c for c in (conflict, value_conflict, dnp_conflict, reference_conflict, datasheet_conflict) if c]
        if mpns or conflicts:  # a line with no MPN gives only its conflicts
            cad.append({"mpns": mpns, "conflict": "; ".join(conflicts), "datasheet": datasheet,
                        "reference": reference})
    references = ", ".join(labels[i] for i in found["Reference"])
    summary = (f"{path}: MPN from {', '.join(columns)}" + (f"; references from {references}" if references else "")
               + ("; read as cp1252" if encoding == "cp1252" else "")
               + f"; {sum(1 for item in cad if item['mpns'])} lines with an MPN")
    return BomParts(cad, summary)


def format_check(result: dict, cli: bool = False) -> list[str]:
    """The result of check_parts as lines, with the next step for each part: the
    MCP tool calls, or the CLI commands if cli is true."""
    def add(mpn: str, base: str = "", doc: str = "") -> str:
        if cli:
            return (f'datasheet-lens project add-part "{mpn}"' + (f' --base "{base}"' if base else "")
                    + (f" --document {doc}" if doc else ""))
        return (f"project_add_part(mpn='{mpn}'" + (f", base='{base}'" if base else "")
                + (f", document='{doc}'" if doc else "") + ")")

    lines = [f"conflict in the CAD project: {c}. Tell the user: one of the fields is wrong."
             for c in result.get("conflicts", [])]
    lines.append(f"the same in the CAD project and in the project file: {len(result['ok'])} parts")
    for d in result["different"]:
        cad, own, maker = d["cad"], d["mpn"], d["manufacturer"] if d["ambiguous"] else ""
        update = (f'datasheet-lens set "{own}"' + (f' --of "{maker}"' if maker else "") + f' -m "{cad}"' if cli
                  else f"project_update_part(mpn='{own}'" + (f", manufacturer='{maker}'" if maker else "")
                  + f", new_mpn='{cad}')")
        lines.append(f"different: CAD {cad}, project {own}. Probably one part with two MPNs. Tell the user: the "
                     f"CAD project or the project file must change ({update} when the CAD MPN is correct).")
    for m in result["missing"]:
        refs = (f" ({', '.join(m['references'])})" if m["references"] else "") + (
            f" [alternative of {m['alternative_of']}]" if m.get("alternative_of") else "")
        if m["documents"]:
            step = f"the store has its documents {', '.join(m['documents'])}: {add(m['mpn'], doc=m['documents'][0])}"
        elif m["bases"]:
            step = (f"the store has the part {m['bases'][0]}, probably its base part: {add(m['mpn'], m['bases'][0])}"
                    " (read the ordering information first)")
        elif m["datasheet"]:
            step = (f"the store has no datasheet. Datasheet field: {m['datasheet']}: "
                    + (f"datasheet-lens add \"{m['datasheet']}\" -m \"{m['mpn']}\"" if cli
                       else f"add_document(path='{m['datasheet']}'), then {add(m['mpn'], '<base part>', '<doc>')}"))
        else:
            step = ("the store has no datasheet, and the CAD part has no Datasheet field: find the datasheet "
                    + (f'(datasheet-lens links "{m["mpn"]}")' if cli else f"(part_links(mpn='{m['mpn']}'))")
                    + ", or tell the user")
        lines.append(f"missing: {m['mpn']}{refs}: {step}")
    for mpn in result["extra"]:
        lines.append(f"extra: {mpn}: the CAD project does not have it. Tell the user; remove it only when the "
                     "user agrees.")
    return lines


def remove_document(project: Project, document: str, mpn: str = "", manufacturer: str = "",
                    delete_pinned: bool = False, apply: bool = True, store: Store | None = None) -> dict:
    """Remove a document (SHA-256 prefix of 8 or more characters, or its file
    name, with or without the folder) from the parts of the project, or from part
    mpn only. Each part remembers it in "exclude", so that sync does not add it
    again (add_part with the document adds it again). The copies are deleted when
    no part uses them (see _drop_copies). The central store does not change.
    store: its manufacturer registry finds the part by any name of its
    manufacturer."""
    key = document.strip()
    if key.startswith(project.folder + "/"):
        key = key[len(project.folder) + 1:]
    parts = [_one_part(project, mpn, manufacturer, store)] if mpn else project.parts
    hits = [(p, d) for p in parts for d in p["documents"]
            if (len(key) >= 8 and d["sha256"].startswith(key.lower())) or d["file"] == _norm_rel(key)]
    if not hits:
        raise LensError(f"no part of the project has the document {document}"
                        + (f" (part {mpn})" if mpn else ""))
    if len({d["sha256"] for _, d in hits}) > 1:
        raise LensError(f"{document} gives more documents: give a longer SHA-256 prefix")
    sha = hits[0][1]["sha256"]
    hit_ids = {id(p) for p, _ in hits}
    changed = copy.deepcopy(project.parts)
    for p, src in zip(changed, project.parts):
        if id(src) in hit_ids:
            p["documents"] = [d for d in p["documents"] if d["sha256"] != sha]
            p["exclude"] = sorted(set(p.get("exclude", [])) | {sha})
    result = _drop_copies(Project(project.root, project.folder, changed, project.layout),
                          [d for _, d in hits], delete_pinned, apply)
    if apply:
        project.parts[:] = changed
        save(project)
    return {"document": sha[:12], "parts": [p["mpn"] for p, _ in hits], **result}


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.lower())


def suggest_parts(store: Store, filename: str) -> list:
    """The parts whose MPN or alias is in the file name (no '-', '_' or case). Only
    the longest names that match."""
    stem = _norm(Path(filename).stem)
    best: dict[int, int] = {}
    for part_id, name in store.part_names():
        n = _norm(name)
        if len(n) >= 3 and n in stem:
            best[part_id] = max(best.get(part_id, 0), len(n))
    if not best:
        return []
    top = max(best.values())
    return [store.part(i) for i, n in sorted(best.items()) if n == top]


def _misplaced(store: Store, part_docs: set[int], doc_id: int) -> list[str]:
    """The parts that the store links to a document of a project part, when the
    store does not give the document to the part (part_docs: the IDs of the
    documents of the store part and of its base part). In TOML a
    [[part.document]] belongs to the [[part]] above it: a block that a person
    moved changes the part with no error. An empty list: no sign of this (also
    for a document that the store links to no part yet, for example on a new
    machine: there the store cannot find a moved block)."""
    if doc_id in part_docs:
        return []
    return sorted({f"{r['mpn']} ({r['manufacturer'] or 'manufacturer unknown'})"
                   for r in store.parts_of_document(doc_id)})


def _store_base(store: Store, part: dict, part_id: int, report: dict) -> int | None:
    """The base part that the project file names for a concrete part goes into
    the store (the project file of a team is the source). Give its ID, or None."""
    if not part.get("base"):
        return None
    base = store.base_of(part_id)
    if base is not None:
        return base["id"]
    try:
        base_id = store.find_part(part["base"], part["manufacturer"], create=True)
        store.set_base(part_id, base_id)
        return base_id
    except LensError as e:
        report["problems"].append(f"part {part['mpn']}: base part {part['base']}: {e}")
        return None


def sync(project: Project, store: Store, apply: bool = True, restore: bool = False) -> dict:
    """Compare the project with the store and the folder. If apply is true: add the
    documents that the store links to the parts, copy the missing copies, import
    the new files of the folder into the store, build the index of each document
    of the project that has none (an index of an older version stays: the
    command datasheet-lens index builds it again), and save the project file. If
    restore is true, a changed copy is copied again from the store. Give the
    report: lists of file names (and of document IDs in "documents"). If apply is
    false, the project does not change."""
    from .indexer import build_index

    if not apply:
        project = copy.deepcopy(project)

    report = {k: [] for k in ("ok", "copied", "restored", "changed", "missing", "imported", "added", "moved",
                              "suggested", "unassigned", "unknown_parts", "problems", "misplaced", "documents")}
    files = _folder_files(project)
    placed: set[str] = set()  # the documents whose copy is at its place: the first part decides
    part_ids: dict[int, int] = {}  # id(project part) -> the ID of its store part

    def move(rel: str, part: dict, sha: str) -> str:
        """Move a copy to the subfolder of the layout. Give its new name."""
        subdir = layout_dir(project, part)
        name = PurePosixPath(rel).name
        want = f"{subdir}/{name}" if subdir else name
        if want == rel:
            return rel
        want = _free_name(want, sha, files, _used(project))
        if want == rel:
            return rel
        if apply:
            source, target = project.file_path(rel), project.file_path(want)
            if want in files:  # the same content is there already
                source.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
            _prune_empty(project, source.parent)
        files[want] = files.pop(rel)
        for p in project.parts:
            for d in p["documents"]:
                if d["file"] == rel:
                    d["file"] = want
        report["moved"].append(f"{rel} -> {want}")
        return want

    def import_file(rel: str) -> Document | None:
        """Import a file of the folder. A file that cannot be read is a problem of
        the report, not an error of the sync."""
        try:
            doc = store.add_document(project.file_path(rel))
        except (LensError, OSError, pdfium.PdfiumError) as e:
            report["problems"].append(f"{rel}: not imported: {e}")
            return None
        report["imported"].append(rel)
        return doc

    # 1. The documents that the store links to the parts of the project.
    for part in project.parts:
        rows = store.match_parts(part["mpn"], part["manufacturer"])
        if not rows:
            report["unknown_parts"].append(part["mpn"])
        elif len(rows) > 1:
            report["problems"].append(
                f"part {part['mpn']}: more parts in the store have this MPN; give the manufacturer in "
                f"{PROJECT_FILE} (" + ", ".join(r["manufacturer"] or "unknown" for r in rows) + ")")
        else:
            _fill(part, store.part(rows[0]["id"]))  # first: the base part search needs the manufacturer
            if apply:
                _store_base(store, part, rows[0]["id"], report)
            # A concrete part with no manufacturer (an older version) gets the one of its base part.
            base = store.base_of(rows[0]["id"])
            if base is not None and base["manufacturer"] and not part["manufacturer"]:
                if apply:
                    store.fill_manufacturer(rows[0]["id"], base["manufacturer"])
                part["manufacturer"] = base["manufacturer"]
            report["added"] += _refresh(project, store, part, rows[0]["id"], files)
            part_ids[id(part)] = rows[0]["id"]

    # 2. The copies of the referenced documents.
    referenced = set()
    done: dict[str, str] = {}  # the copies that this step copied or found changed: file -> SHA-256
    imported: set[str] = set()  # the documents that this sync put into the store
    for part in project.parts:
        part_docs = {r["id"] for r in store.documents_of_part(part_ids[id(part)])} if id(part) in part_ids else None
        for d in part["documents"]:
            rel, doc = d["file"], store.document(d["sha256"])
            misplaced = []
            if doc is not None and part_docs is not None and d["sha256"] not in imported:
                misplaced = _misplaced(store, part_docs, doc.id)
                if misplaced:
                    report["misplaced"].append({"file": rel, "mpn": part.get("base") or part["mpn"],
                                                "part": part["mpn"], "base": part.get("base", ""),
                                                "sha256": doc.sha256, "linked": misplaced})
            if rel in files and files[rel] == d["sha256"]:
                if apply and (doc is None or d["sha256"] in imported):  # a project from a different machine
                    doc = doc or import_file(rel)
                    if doc is not None:
                        imported.add(doc.sha256)
                        try:
                            part_id = store.find_part(part["mpn"], part["manufacturer"], create=True)
                            # The document belongs to the base part, if the file names one.
                            owner = _store_base(store, part, part_id, report) or part_id
                            store.link(owner, doc.id, d["role"])
                        except LensError as e:
                            report["problems"].append(f"{rel}: not linked to a part: {e}")
                # A copy that the user did not pin goes to the subfolder of the layout.
                # A misplaced reference does not move it: its part is not sure.
                moved = False
                if doc is not None and not d.get("pinned") and not misplaced and d["sha256"] not in placed:
                    try:
                        new = move(rel, part, d["sha256"])
                        moved, rel = new != rel, new
                    except OSError as e:
                        report["problems"].append(f"{rel}: not moved: {e}")
                placed.add(d["sha256"])
                if not moved:
                    report["ok"].append(rel)
            elif doc is None:
                # The store does not have the document. A different file with this
                # name is a new document (step 3).
                report["missing"].append(rel)
                if rel in files:
                    continue
            elif done.get(rel) == d["sha256"]:
                pass  # a document of two parts has one copy: the first part did it
            elif rel in files and not restore:
                report["changed"].append(rel)
                done[rel] = d["sha256"]
            else:
                done[rel] = d["sha256"]
                if apply:
                    target = project.file_path(rel)
                    tmp = temp_path(target)
                    try:
                        shutil.copyfile(doc.path, tmp)
                        tmp.replace(target)
                    finally:
                        tmp.unlink(missing_ok=True)
                report["restored" if rel in files else "copied"].append(rel)
            referenced.add(rel)
            if doc is not None:
                report["documents"].append(doc.id)

    # 3. The new files of the folder. A document that the project does not want
    # (exclude) is not a new file: the user kept its copy.
    known = {d["sha256"] for part in project.parts for d in part["documents"]}
    known |= {sha for part in project.parts for sha in part.get("exclude", [])}
    for rel, sha in files.items():
        if rel in referenced or sha in known:
            continue
        doc = import_file(rel) if apply else store.document(sha)
        if doc is None and apply:
            continue
        linked = store.parts_of_document(doc.id) if doc else []
        assigned = False
        for row in linked:
            part = project.find(row["mpn"], row["manufacturer"], store)
            if part is not None and sha not in part.get("exclude", []):
                if apply:  # the user put the file there: it stays there
                    part["documents"].append({"sha256": sha, "file": rel, "role": row["role"], "pinned": True})
                report["added"].append(rel)
                assigned = True
        if not assigned:
            proposals = linked or suggest_parts(store, rel)
            key = "suggested" if proposals else "unassigned"
            report[key].append({"file": rel, "sha256": sha, "parts": [
                f"{p['mpn']}" + (f" ({p['manufacturer']})" if p["manufacturer"] else "") for p in proposals]})
        if doc is not None:
            report["documents"].append(doc.id)
        known.add(sha)
    report["documents"] = sorted(set(report["documents"]))
    if apply:
        # A new document, or a document that came into the store with no index
        # (for example through the command add of an older version).
        for doc_id in report["documents"]:
            if store.build(doc_id, "index") is not None:
                continue
            try:
                build_index(store, doc_id)
            except (LensError, OSError, pdfium.PdfiumError) as e:
                doc = store.document(doc_id)
                report["problems"].append(f"{doc.filename} ({doc.sha256[:12]}): no index: {e}")
        save(project)
    report["outside"], report["outside_complete"] = outside_pdfs(project, store)
    return report


def format_parts(project: Project) -> list[str]:
    """The parts and the documents of a project as lines, as in the project file."""
    lines = [f"project {project.root} (folder {project.folder}/): {len(project.parts)} parts "
             f"(as in {PROJECT_FILE})"]
    for part in project.parts:
        lines.append(f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'})"
                     + (f", base part {part['base']}" if part.get("base") else "")
                     + (f", variants: {', '.join(part['variants'])}" if part.get("variants") else ""))
        for d in part["documents"]:
            lines.append(f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}")
    return lines


def format_report(report: dict, apply: bool, add_hint: tuple[str, str]) -> list[str]:
    """The report of sync as lines. add_hint tells how to assign a document to a
    part: a template with {part} (the concrete MPN), {base} and {sha}, and the
    template of the base argument with {base} (left out when there is no base)."""
    template, base_arg = add_hint

    def hint(part: str, sha: str, base: str = "") -> str:
        return template.format(part=part, base=base_arg.format(base=base) if base else "", sha=sha[:12])

    lines = []
    for key, text in (("added", "new reference"), ("copied", "copied" if apply else "copy missing"),
                      ("restored", "copied again" if apply else "copy to restore"),
                      ("moved", "moved to its subfolder" if apply else "to move to its subfolder"),
                      ("imported", "imported into the store")):
        lines += [f"{text}: {rel}" for rel in report[key]]
    lines += [f"changed: {rel} (the copy differs from the store; restore it, or give the file "
              "a different name to import it as a new document)" for rel in report["changed"]]
    lines += [f"missing: {rel} (the store does not have this document, and the folder does not have "
              "a file with its content)" for rel in report["missing"]]
    lines += [f"part not in the store: {mpn}" for mpn in report["unknown_parts"]]
    lines += [f"problem: {text}" for text in report["problems"]]
    for item in report["misplaced"]:
        lines.append(f"problem: {item['file']}: the project file has it under part {item['part']}, but the store "
                     f"links it to {', '.join(item['linked'])}. A [[part.document]] block belongs to the [[part]] "
                     "above it: was the block moved? If the part is correct, link it with "
                     + hint(item["part"], item["sha256"], item["base"]))
    # A proposal is the part of the datasheet. The BOM can have a concrete MPN
    # of it (TLV75533PDBVR of TLV755P): then the proposal is the base part.
    for item in report["suggested"]:
        mpn = item["parts"][0].split(" (")[0]
        lines.append(f"no part: {item['file']} ({item['sha256'][:12]}); proposal: {', '.join(item['parts'])}; "
                     f"assign it with {hint(mpn, item['sha256'])}. If the BOM has a different concrete MPN of "
                     f"{mpn}, give it as the MPN and {mpn} as the base part: {hint('<MPN>', item['sha256'], mpn)}")
    for item in report["unassigned"]:
        lines.append(f"no part: {item['file']} ({item['sha256'][:12]}); assign it with "
                     f"{hint('<MPN>', item['sha256'])}, with the concrete orderable MPN of the BOM")
    # The folders with store documents in detail, the others in one line.
    outside = report.get("outside", [])
    for g in (g for g in outside if g["in_store"]):
        lines.append(f"PDF files outside the project folder: {g['folder']}/ ({g['files']} files, {g['in_store']} "
                     f"in the store, {g['in_project']} documents of the project). sync does not import them. If "
                     "this folder holds the datasheets of the design, the key folder of the project file can "
                     "name it: tell the user.")
    other = [g for g in outside if not g["in_store"]]
    if other:
        lines.append(f"PDF files outside the project folder that the store does not have: "
                     f"{sum(g['files'] for g in other)} in {len(other)} folders (for example {other[0]['folder']}/)")
    if not report.get("outside_complete", True):
        lines.append(f"(the search for PDF files outside the project folder stopped after {MAX_OUTSIDE} files)")
    # One copy can belong to two parts: count the files.
    current = set(report["ok"])
    if apply:  # after a sync, the copied and the moved copies are current too
        current |= set(report["copied"]) | set(report["restored"]) | {m.split(" -> ")[-1] for m in report["moved"]}
    lines.append(f"{len(current)} copies are current")
    return lines
