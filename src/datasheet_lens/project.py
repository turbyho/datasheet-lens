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

from . import PROJECT_URL
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

    def find(self, mpn: str, manufacturer: str = "") -> dict | None:
        """The part with this MPN (and this manufacturer, if given), or None."""
        for part in self.parts:
            if part["mpn"].lower() == mpn.lower() and (
                    not manufacturer or not part["manufacturer"]
                    or part["manufacturer"].lower() == manufacturer.lower()):
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
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LensError(f"{path}: {e}") from e
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
    folder, layout, parts = _parse(text, path)
    return Project(root, folder, parts, layout, text=text)


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
    the same manufacturer or no manufacturer in the table."""
    mpn, manufacturer = _strip(table.get("mpn", "")), _strip(table.get("manufacturer", ""))
    return mpn.lower() == part["mpn"].lower() and (
        not manufacturer or manufacturer.lower() == part["manufacturer"].strip().lower())


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


def _entry(project: Project, store: Store, part_id: int) -> dict:
    """The project part of a store part (made if necessary). An empty
    manufacturer or category of the project part comes from the store."""
    row = store.part(part_id)
    part = project.find(row["mpn"], row["manufacturer"])
    if part is None:
        part = {"mpn": row["mpn"], "manufacturer": row["manufacturer"], "category": row["family"] or "",
                "documents": []}
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
    if document:
        doc = store.document(document)
        if doc is None:
            raise LensError(f"document not found: {document}")
        store.link(owner, doc.id, role)
    part = _entry(project, store, part_id)
    if category.strip():
        part["category"] = category.strip()
    if document and doc.sha256 in part.get("exclude", []):
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


def _one_part(project: Project, mpn: str, manufacturer: str = "") -> dict:
    """The project part with this MPN (and manufacturer, if more parts have it)."""
    found = [p for p in project.parts if p["mpn"].lower() == mpn.strip().lower()
             and (not manufacturer or p["manufacturer"].lower() == manufacturer.strip().lower())]
    if not found:
        raise LensError(f"the project has no part {mpn}")
    if len(found) > 1:
        raise LensError(f"the project has more parts {mpn}: give the manufacturer ("
                        + ", ".join(p["manufacturer"] or "unknown" for p in found) + ")")
    return found[0]


def remove_part(project: Project, mpn: str, manufacturer: str = "", delete_pinned: bool = False,
                apply: bool = True) -> dict:
    """Remove a part from the project, and delete its copies (see _drop_copies).
    The central store does not change: other projects can use the part. If apply
    is false, nothing changes: the result tells what would change."""
    part = _one_part(project, mpn, manufacturer)
    rest = [p for p in project.parts if p is not part]
    result = _drop_copies(Project(project.root, project.folder, rest, project.layout), part["documents"],
                          delete_pinned, apply)
    if apply:
        project.parts[:] = rest
        save(project)
    return {"part": part["mpn"], **result}


def remove_document(project: Project, document: str, mpn: str = "", manufacturer: str = "",
                    delete_pinned: bool = False, apply: bool = True) -> dict:
    """Remove a document (SHA-256 prefix of 8 or more characters, or its file
    name, with or without the folder) from the parts of the project, or from part
    mpn only. Each part remembers it in "exclude", so that sync does not add it
    again (add_part with the document adds it again). The copies are deleted when
    no part uses them (see _drop_copies). The central store does not change."""
    key = document.strip()
    if key.startswith(project.folder + "/"):
        key = key[len(project.folder) + 1:]
    parts = [_one_part(project, mpn, manufacturer)] if mpn else project.parts
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
            part = project.find(row["mpn"], row["manufacturer"])
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
    # One copy can belong to two parts: count the files.
    current = set(report["ok"])
    if apply:  # after a sync, the copied and the moved copies are current too
        current |= set(report["copied"]) | set(report["restored"]) | {m.split(" -> ")[-1] for m in report["moved"]}
    lines.append(f"{len(current)} copies are current")
    return lines
