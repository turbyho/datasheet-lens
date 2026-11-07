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
import sqlite3
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import pypdfium2 as pdfium
import tomlkit
import tomlkit.exceptions
from tomlkit.container import Container
from tomlkit.items import AoT, Comment, Null, Table, Whitespace

from . import PROJECT_URL, makers
from .errors import LensError
from .fileutil import temp_path, write_atomic
from .store import LIBRARIES, ROLES, Document, Store, check_source, sha256_file

PROJECT_FILE = "datasheet-lens.toml"
DEFAULT_FOLDER = "datasheets"
# The inbox: a subfolder of the folder for the PDF files that the user puts
# there (a datasheet that a tool could not download, or many datasheets at one
# time). sync imports each file into the store and gives it to the project
# parts that it agrees with (see _inbox). It is not a folder of copies.
INBOX = "_inbox"
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

    @property
    def inbox_path(self) -> Path:
        """The inbox in the folder (see INBOX)."""
        return self.folder_path / INBOX

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
    project.inbox_path.mkdir(parents=True, exist_ok=True)
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
        sources = _parse_sources(p.get("source", []), f"{path}: part {p['mpn']}")
        if sources:  # the sources that this project orders the part from
            part["sources"] = sources
        out.append(part)
    return folder, layout, out


def _parse_sources(sources, where: str) -> list[dict]:
    """The purchase sources of a project part: [{"source", "number", "library"}]."""
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        raise LensError(f"{where}: 'source' must be a list of tables ([[part.source]])")
    out = []
    for s in sources:
        name, number, library = s.get("source"), s.get("number"), s.get("library", "")
        if not isinstance(number, str) or not number.strip():
            raise LensError(f"{where}: a source needs its part number (number)")
        if not isinstance(library, str):
            raise LensError(f"{where}: library must be one of {', '.join(LIBRARIES)}")
        try:
            number, library = check_source(name, number, library)
        except LensError as e:
            raise LensError(f"{where}: {e}") from None
        entry = {"source": name, "number": number}
        if library:
            entry["library"] = library
        out.append(entry)
    return out


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
                                   tuple(p.get("variants", [])), tuple(sorted(p.get("exclude", []))),
                                   tuple(sorted((d["sha256"], d["file"], d["role"], d.get("pinned", False))
                                                for d in p["documents"])),
                                   tuple(sorted((s["source"], s["number"], s.get("library", ""))
                                                for s in p.get("sources", [])))) for p in parts)


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
SOURCE_KEYS = ("source", "number", "library")


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


def _starts_gap(items: list) -> bool:
    """True when a list of comment and blank-line items starts with a blank
    line, or is empty."""
    return not items or isinstance(items[0], Whitespace)


def _join_gaps(tails: list[list]) -> list:
    """Join the tails of tables that follow each other. The blank lines at the
    start of a tail and the blank lines at the end of the tail before it are
    one gap: only the later blank lines stay."""
    joined: list = []
    for tail in tails:
        if _starts_gap(tail):
            while joined and isinstance(joined[-1], Whitespace):
                joined.pop()
        joined.extend(tail)
    return joined


def _clear_gap(body: list, end: int) -> None:
    """Make the blank-line items before body[end] empty. The items stay in the
    list, because tomlkit keeps the index of each key of a body."""
    i = end - 1
    while i >= 0 and body[i][0] is None and isinstance(body[i][1], (Whitespace, Null)):
        body[i] = (None, Whitespace(""))
        i -= 1


def _key_index(container, key: str) -> int:
    """The index of the key in the body of a tomlkit table or document."""
    for n, (k, _) in enumerate(_body(container)):
        if k is not None and k.key == key:
            return n
    raise LensError(f"{PROJECT_FILE}: datasheet-lens cannot find the position of '{key}' in the file")


def _at_start(container, key: str) -> bool:
    """True when only blank lines come before the array container[key] in the
    file: the array is at the start of the document."""
    if not isinstance(container, Container):
        return False
    body = _body(container)
    return all(k is None and isinstance(v, (Whitespace, Null)) for k, v in body[:_key_index(container, key)])


def _remove_tables(container, key: str, old: list, keep: set[int]) -> list:
    """Remove the tables of old that are not in keep from the array of tables
    container[key]. The text of a removed table goes, and one gap goes with it:
    the blank lines before its header, or the blank lines at its end. The
    comment lines at the end of a removed table stay above the next header.
    Gives the comment and blank-line items that no kept table can hold (all
    tables of the array are removed): they come after the array."""
    aot = container.get(key)
    rest: list = []
    b = len(old)
    while b > 0:
        if id(old[b - 1]) in keep:
            b -= 1
            continue
        a = b - 1  # old[a:b] is a run of removed tables
        while a > 0 and id(old[a - 1]) not in keep:
            a -= 1
        gap = _join_gaps([_take_tail(t) for t in old[a:b]])
        previous = old[a - 1] if a > 0 else None
        following = old[b] if b < len(old) else None
        if previous is not None:
            body = _body(_last_table(previous))
            end = len(body)
        else:
            body = _body(container)
            end = _key_index(container, key)
        if previous is None and _at_start(container, key):
            _clear_gap(body, end)  # the start of the file: no blank lines
            while gap and isinstance(gap[0], Whitespace):
                gap.pop(0)
        elif _starts_gap(gap):
            _clear_gap(body, end)
        for i in reversed(range(a, b)):
            del aot[i]
        if following is not None:
            following.trivia.indent = "".join(x.as_string() for x in gap) + following.trivia.indent
        elif previous is not None:
            for x in gap:
                _last_table(previous).add(x)
        else:
            rest = gap
        b = a
    return rest


def _set_tables(container, key: str, old: list, new: list) -> None:
    """Make the array of tables container[key] hold the tables of new. The array
    is changed in place: a kept table keeps its position, its text and its
    comments, and a new table comes at the end. The comment lines above a header
    stay above it. The order of the parts and of the documents has no meaning."""
    keep, before = {id(t) for t in new}, {id(t) for t in old}
    added = [t for t in new if id(t) not in before]
    # rest: the comment and blank-line items above the header that comes after
    # the array, when the array keeps no table.
    rest = _remove_tables(container, key, old, keep)
    aot = container.get(key)
    if not new:
        if key in container:
            index = _key_index(container, key)
            del container[key]  # tomlkit puts a Null item in its place
            # In a parsed file, the next item after an array is a table or an
            # array of tables: tomlkit puts the comment lines above a header
            # at the end of the table before it.
            following = next((v for _, v in _body(container)[index + 1:] if not isinstance(v, Null)), None)
            if isinstance(following, AoT) and len(following):
                following = following[0]
            if isinstance(following, Table):
                following.trivia.indent = "".join(x.as_string() for x in rest) + following.trivia.indent
            else:
                for x in rest:
                    container.add(x)
        return
    if not added:
        return
    if aot is None:
        # The comment lines at the end of the text before the new array are
        # above the header that comes after it: they go after the new tables.
        tail = _take_tail(container)
        first_gap = "\n"
        container[key] = tomlkit.aot()
        aot = container[key]
    elif len(aot):
        # The same for the comment lines at the end of the last table.
        tail = _take_tail(aot[-1])
        first_gap = "\n"
    else:  # the array keeps no table: the new tables come in its place
        tail = rest
        first_gap = "" if _at_start(container, key) else "\n"
    for n, t in enumerate(added):
        t.trivia.indent = first_gap if n == 0 else "\n"  # a blank line before the header
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
        old_sources = _tables(table, "source")
        free = list(old_sources)
        source_tables = []
        for s in part.get("sources", []):
            st = next((x for x in free if x.get("source") == s["source"] and _strip(x.get("number", ""))
                       == s["number"]), None) or tomlkit.table()
            free = [x for x in free if x is not st]
            _set(st, "source", s["source"], order=SOURCE_KEYS)
            _set(st, "number", s["number"], norm=_strip, order=SOURCE_KEYS)
            _set(st, "library", s.get("library", ""), norm=_strip, order=SOURCE_KEYS)
            _reorder(st, SOURCE_KEYS)
            source_tables.append(st)
        _set_tables(table, "source", old_sources, source_tables)
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
    """{relative file name: SHA-256} of the PDF files in the folder. A file of
    the inbox is in the list only when a reference names it: then it is a copy
    (a pinned file of an older version, or a layout that gives the folder of
    the inbox), not a file for _inbox."""
    folder = project.folder_path
    if not folder.is_dir():
        return {}
    named = _used(project)
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if p.is_file() and p.suffix.lower() == ".pdf" and (rel.parts[0] != INBOX or rel.as_posix() in named):
            out[rel.as_posix()] = _hash(p)
    return out


def _inbox_files(project: Project) -> list[Path]:
    """The PDF files of the inbox, not the copies that a reference names (see
    _folder_files)."""
    inbox = project.inbox_path
    if not inbox.is_dir():
        return []
    named = _used(project)
    return [p for p in sorted(inbox.rglob("*")) if p.is_file() and p.suffix.lower() == ".pdf"
            and p.relative_to(project.folder_path).as_posix() not in named]


def _has_datasheet(project_part: dict, store: Store) -> bool:
    """True if the part has a datasheet: in the project file, or in the store
    (the part or its base part)."""
    if any(d.get("role", "datasheet") == "datasheet" for d in project_part["documents"]):
        return True
    rows = store.match_parts(project_part["mpn"], project_part["manufacturer"])
    return len(rows) == 1 and any(r["role"] == "datasheet" for r in store.documents_of_part(rows[0]["id"]))


def _inbox_parts(project: Project, store: Store, doc: Document) -> tuple[list[dict], str, list[str]]:
    """The project parts that a document of the inbox goes to, the part that
    the PDF proposes ("MPN (manufacturer)" or ""), and the reasons why a part
    that agrees does not get it.

    A part agrees when its MPN is an ordering code of the document, or when its
    MPN or its base part is the part of the PDF or one of its variants. A live
    check, 2026-11-07: 21 of 28 datasheets of a project agreed with their parts,
    none with a different part. The ordering codes come from the index of the
    document.

    A part that agrees gets the document only when these are true:
    - The manufacturer of the datasheet is the manufacturer of the part. The
      manufacturer of the datasheet is the valid one, and a copy of a part has
      the same MPN and its own datasheet. A name that the registry does not
      relate to the name of the part stops the link: the user decides.
    - The part has no datasheet. The tool does not find the role of a document
      (errata, manual, application note): the role is datasheet only for a part
      with no datasheet, and the user decides for the other parts."""
    from .identify import ordering_codes, propose

    proposal = propose(doc)
    codes = {code.upper() for code, *_ in ordering_codes(store, doc.id, proposal)}
    names = {n.upper() for n in (proposal.mpn, *proposal.variants) if n}
    agree, reasons = [], []
    for part in project.parts:
        if doc.sha256 in part.get("exclude", []):
            continue
        mpn, base = part["mpn"].upper(), (part.get("base") or "").upper()
        if not (mpn in codes or mpn in names or (base and base in names)):
            continue
        name = f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'})"
        if part["manufacturer"] and not (proposal.manufacturer
                                         and store.same_maker(part["manufacturer"], proposal.manufacturer)):
            reasons.append(f"the MPN agrees with {name}, but the datasheet is not of the same manufacturer, or "
                           "the tool cannot read its manufacturer")
        elif _has_datasheet(part, store):
            reasons.append(f"the MPN agrees with {name}, but the part has a datasheet already: is the file a "
                           "different document (errata, manual) or a new revision?")
        else:
            agree.append(part)
    text = f"{proposal.mpn} ({proposal.manufacturer or 'manufacturer unknown'})" if proposal.mpn else ""
    return agree, text, reasons


def _inbox(project: Project, store: Store, apply: bool, report: dict) -> list[dict]:
    """The files of the inbox: import each one into the store with its index,
    and link it to the project parts that it goes to (_inbox_parts), as add_part
    does. The next steps of sync give the documents to the parts and make their
    copies. A file with no part stays for the user. A file that the project has
    already goes from the inbox. Gives the files that can go from the inbox
    ({"path", "sha256"}): sync removes each one at its end, when the project has
    its document then. A dry run (apply false) only tells the files."""
    from .indexer import build_index

    if apply:
        project.inbox_path.mkdir(parents=True, exist_ok=True)
    referenced = {d["sha256"] for part in project.parts for d in part["documents"]}
    out = []
    for path in _inbox_files(project):
        rel = path.relative_to(project.folder_path).as_posix()
        if not apply:
            known = _hash(path) in referenced
            report["inbox_known" if known else "inbox_new"].append(rel)
            continue
        try:
            doc = store.add_document(path)
            report["documents"].append(doc.id)
            if doc.sha256 in referenced:
                report["inbox_known"].append(rel)
                out.append({"path": path, "sha256": doc.sha256})
                continue
            if store.build(doc.id, "index") is None:
                build_index(store, doc.id)  # the ordering codes come from the index
            parts, proposal, reasons = _inbox_parts(project, store, doc)
            for part in parts:
                store_part(store, part["mpn"], part["manufacturer"], doc.sha256, "datasheet", "",
                           part.get("base", ""))
        except (LensError, OSError, pdfium.PdfiumError) as e:
            report["problems"].append(f"{rel}: not taken from the inbox: {e}")
            continue
        if not parts:
            report["inbox_waiting"].append({"file": rel, "sha256": doc.sha256, "proposal": proposal,
                                            "other": reasons})
            continue
        report["inbox_assigned"].append({"file": rel, "sha256": doc.sha256, "parts": [p["mpn"] for p in parts]})
        out.append({"path": path, "sha256": doc.sha256})
    return out


def _remove_from_inbox(project: Project, files: list[dict], report: dict) -> None:
    """Remove the files of the inbox whose document the project has now, and
    the empty subfolders of the inbox. The inbox stays. A file whose document
    the project did not get (for example: two parts of the store have its MPN)
    stays, with a problem."""
    referenced = {d["sha256"] for part in project.parts for d in part["documents"]}
    for item in files:
        path = item["path"]
        if item["sha256"] not in referenced:
            rel = path.relative_to(project.folder_path).as_posix()
            report["inbox_assigned"] = [a for a in report["inbox_assigned"] if a["file"] != rel]
            report["problems"].append(f"{rel}: the store links it to its parts, but the project did not get it "
                                      "(see the problems above): the file stays in the inbox")
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        folder = path.parent
        with contextlib.suppress(OSError):
            while folder != project.inbox_path and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent


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
    layout and the part in the project file (manufacturer, category, mpn). The
    manufacturer is its built-in name (makers.builtin_name): the distributors
    and the datasheets write one manufacturer in different forms ("TI", "Texas
    Instruments"), and each form would get its own folder. Not the relations of
    the store: they differ between the machines, and the copies of a shared
    project would move with each sync. A missing value gives UNKNOWN."""
    values = {k: str(part.get(k, "")) for k in LAYOUT_KEYS}
    if values["manufacturer"].strip():
        values["manufacturer"] = makers.builtin_name(values["manufacturer"].strip())
    values = {k: _safe(v) or UNKNOWN for k, v in values.items()}
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
    return add_part_with_id(project, store, mpn, manufacturer, document, role, category, base)[0]


def store_part(store: Store, mpn: str, manufacturer: str = "", document: str = "", role: str = "datasheet",
               category: str = "", base: str = ""):
    """The store part of add_part (made if necessary), with its base part, its
    category and the link of the document (to the base part when there is
    one). Gives (part ID, the document or None)."""
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
    return part_id, doc


def add_part_with_id(project: Project, store: Store, mpn: str, manufacturer: str = "", document: str = "",
                     role: str = "datasheet", category: str = "", base: str = "",
                     source: dict | None = None) -> tuple[dict, int]:
    """add_part, and the ID of the store part: the caller gets the same store
    part as the project part (adopt saves its purchase source there). source:
    a purchase source ({"source", "number", "library"}) of the project part, in
    the same save of the project file."""
    if project.text is not None and (not project.path.exists()
                                     or project.path.read_text(encoding="utf-8") != project.text):
        # The check of save, before the store changes.
        raise LensError(f"{project.path} changed after datasheet-lens read it: run the command again")
    part_id, doc = store_part(store, mpn, manufacturer, document, role, category, base)
    part = _entry(project, store, part_id, manufacturer)
    if category.strip():
        part["category"] = category.strip()
    if doc is not None and doc.sha256 in part.get("exclude", []):
        # An explicit request adds a document that remove_document took away.
        part["exclude"] = [s for s in part["exclude"] if s != doc.sha256]
        if not part["exclude"]:
            del part["exclude"]
    _refresh(project, store, part, part_id, _folder_files(project))
    if source is not None:
        _put_source(part, source["source"], source["number"], source.get("library", ""))
    save(project)
    return part, part_id


def set_source(project: Project, part: dict, source: str, number: str, library: str = "") -> None:
    """Add or change a purchase source of a project part ([[part.source]]): the
    source that this project orders the part from. Saves the project file."""
    _put_source(part, source, number, library)
    save(project)


def remove_source(project: Project, store: Store, mpn: str, number: str, source: str = "",
                  manufacturer: str = "", from_store: bool = False, apply: bool = True) -> dict:
    """Remove a purchase source ([[part.source]]) from a project part: the part
    number of a source that the project does not order (a different packaging,
    or a wrong number). source: only of this source, when more sources have the
    number. from_store: also remove it from the central store (and from the team
    server), for a number that is wrong for the part (for example a C number of
    a different MPN). Else the store keeps it: the store tells where the part is
    available. Gives {"part", "removed" (the sources), "store" (True if the store
    had it)}. A LensError when the part has no such source."""
    part = _one_part(project, mpn, manufacturer, store)
    number = number.strip()
    wanted = [s for s in part.get("sources", []) if s["number"].upper() == number.upper()
              and (not source.strip() or s["source"] == source.strip().lower())]
    if not wanted:
        have = ", ".join(f"{s['source']} {s['number']}" for s in part.get("sources", [])) or "none"
        raise LensError(f"the part {part['mpn']} has no purchase source {number} (its sources: {have})")
    if len({s["source"] for s in wanted}) > 1:
        raise LensError(f"more sources of {part['mpn']} have the number {number}: give the source "
                        f"({', '.join(sorted({s['source'] for s in wanted}))})")
    # The store parts of the project part (each spelling of the manufacturer) that have the number.
    held = [(r["id"], s) for r in store.match_parts(part["mpn"], part["manufacturer"]) for s in wanted
            if any(x["source"] == s["source"] and x["number"].upper() == s["number"].upper()
                   for x in store.part_sources(r["id"]))] if from_store else []
    if apply:
        part["sources"] = [s for s in part["sources"] if s not in wanted]
        if not part["sources"]:
            del part["sources"]
        save(project)
        for part_id, s in held:
            store.delete_part_source(part_id, s["source"], s["number"])
    return {"part": part["mpn"], "removed": wanted, "store": bool(held)}


def _put_source(part: dict, source: str, number: str, library: str = "") -> None:
    """Add or change a purchase source in a project part (no save)."""
    number, library = check_source(source, number, library)
    entry = {"source": source, "number": number}
    if library:
        entry["library"] = library
    sources = [s for s in part.get("sources", []) if (s["source"], s["number"]) != (source, number)]
    part["sources"] = sources + [entry]


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
    if target.lower() != part["mpn"].lower() and part.get("sources"):
        # A purchase source names one orderable part: it is not a source of the new MPN.
        removed = part.pop("sources")
        part["notes"] = [f"removed the purchase sources of {part['mpn']}: "
                         + ", ".join(f"{s['source']} {s['number']}" for s in removed)
                         + f". Take {target} from a source again (adopt)."]
    # save finds the table of the part in the file by the old MPN and manufacturer.
    part["was"] = (part["mpn"], part["manufacturer"])
    part["mpn"], part["manufacturer"] = target, maker
    _refresh(project, store, part, part_id, _folder_files(project))
    try:
        save(project)
    finally:
        del part["was"]
    return part


# The text of KiCad in a BOM line of more symbols or units with different values of
# a field (INDETERMINATE_STATE, include/widgets/ui_common.h; FIELDS_EDITOR_GRID_DATA_MODEL
# in eeschema/fields_data_model.cpp, KiCad 8, 9 and 10). A KiCad in an other language
# writes its translation.
MIXED_VALUES = "-- mixed values --"


def _is_field_mpn(item: str) -> bool:
    """True if an item of a list of MPNs is an MPN: the user writes the list, thus
    each text, also with no digit (PJ-SMD; user, 2026-11-05). Not an MPN:
    MIXED_VALUES."""
    return item.strip().lower() != MIXED_VALUES


def _unique(items: list[str]) -> list[str]:
    """The items in their order, each MPN one time (no regard to signs and case)."""
    seen: set[str] = set()
    return [i for i in items if not (_norm(i) in seen or seen.add(_norm(i)))]


# A list of MPNs, in the parentheses of Value and in the field MPN
# (plans/confirmed-mpns.md, decisions 13, 14 and 23). The separators are "," and ";":
# KiCad 9 and 10 join the different values of a field of the units of one symbol
# with "," (fields_data_model.cpp). Not a slash: it is part of many MPNs
# (LM358DR/NOPB). An item in quotes can have each character ("" is a quote, as in
# CSV); a quote has this function only at the start of an item.
MPN_SEPARATORS = ",;"
# A comma with no space before three digits can be a separator or a part of the MPN:
# the packing code of NXP and Nexperia (BC847B,215, 74HC595D,118). After the digits:
# a separator, the end of the list, or the closing parenthesis of Value.
PACKING_CODE = r"(?<=[A-Za-z0-9]),\d{{3}}\s*(?:[,;{closing}]|$)"
LIST_QUOTE_HINT = "write an MPN with , ; or a parenthesis in quotes, for example \"BC847B,215\""
NOT_CLEAR = "a comma before three digits can be a separator or part of the MPN: " + LIST_QUOTE_HINT


def _list_items(text: str, start: int = 0, closing: str = "") -> tuple[list[str], int, str]:
    """The items of a list of MPNs in text, from start. closing: ")" for the
    parentheses of Value (the list ends at it; a parenthesis in an MPN needs quotes),
    empty for the field MPN (the list ends at the end of the text). Give the items,
    the index after the list, and an error text (empty: no error).

    A comma of a packing code (PACKING_CODE) in a text with no quotes: when each
    separator of the list is such a comma, the list is one MPN (BC847B,215).
    Else the list is not clear (NOT_CLEAR): the item is the full text of the list
    (no MPN is lost, and the tool does not guess), and the user writes the MPN in
    quotes. The spaces around an item do not count, also in quotes."""
    stops = MPN_SEPARATORS + (closing + "(" if closing else "")
    packing = re.compile(PACKING_CODE.format(closing=re.escape(closing)))
    items: list[str] = []
    separators: list[bool] = []  # for each separator: a comma of a packing code
    quoted = False
    i, n = start, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == '"':
            quoted, out, i = True, [], i + 1
            while True:
                if i >= n:
                    return items, i, "a quote that is not closed"
                if text[i] == '"' and text[i + 1:i + 2] == '"':
                    out.append('"')
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    out.append(text[i])
                    i += 1
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] not in MPN_SEPARATORS + closing:
                return items, i, "a text after a quote: the quotes must be around the full MPN"
            items.append("".join(out).strip())
        else:
            j = i
            while j < n and text[j] not in stops:
                j += 1
            if closing and j < n and text[j] == "(":
                return items, j, "a parenthesis in an MPN: " + LIST_QUOTE_HINT
            items.append(_filled(text[i:j]))
            i = j
        if i >= n:
            if closing:
                return items, i, "a parenthesis that is not closed"
            break
        if text[i] == closing:
            i += 1
            break
        separators.append(text[i] == "," and bool(packing.match(text, i)))
        i += 1
    if any(separators):
        body = [_filled(text[start:i - 1 if closing else i])]
        if all(separators) and not quoted:  # one MPN with a packing code: BC847B,215
            return body, i, ""
        return body, i, NOT_CLEAR
    return [item for item in items if item], i, ""


def field_list(text: str) -> tuple[list[str], str]:
    """The items of the field MPN, in their order, and an error text (see
    _list_items). "~" is an empty field (KiCad). A list that is not clear is one
    item, the full text: no MPN is lost, and the tool does not guess the items."""
    items, _, error = _list_items(_filled(text))
    return ([_filled(text)] if error else items), error


def field_items(text: str) -> list[str]:
    """The items of the field MPN (see field_list), also when the list is not clear."""
    return field_list(text)[0]


def format_mpns(items: list[str], value: bool = False) -> str:
    """The text of a list of MPNs, with quotes only where they are necessary (see
    _list_items). value: for the parentheses of Value, where a parenthesis in an
    MPN needs quotes too. The text must give the items again: else the tool writes
    quotes around each item (also "~", which is an empty field with no quotes)."""
    def reads_as(text: str) -> bool:
        if value:
            parts = split_value(f"({text})")
            return parts.mpns == items and not parts.error
        return field_list(text) == (items, "")

    def quoted(item: str) -> str:
        return '"' + item.replace('"', '""') + '"'

    special = MPN_SEPARATORS + ("()" if value else "")
    for text in (", ".join(items),
                 ", ".join(quoted(i) if any(c in i for c in special) or i.startswith('"') else i for i in items)):
        if reads_as(text):
            return text
    return ", ".join(quoted(i) for i in items)


# The texts of a list of MPNs that mean "no part": the symbol needs no datasheet (a
# generic resistor, a test point). Not a conflict. TBD (to be determined) is an MPN
# that is not selected yet: an empty field.
NO_PART = ("n/a", "na", "-", "--", "none")
NOT_SELECTED = ("tbd",)


def mpn_items(text: str) -> list[str]:
    """The items of a list of MPNs with no "no part" texts (NO_PART) and no "not
    selected" texts (NOT_SELECTED)."""
    return [i for i in field_items(text) if i.lower() not in NO_PART + NOT_SELECTED]


@dataclass
class ValueParts:
    """A Value by the convention of the parentheses (plans/confirmed-mpns.md,
    decision 13)."""
    general: str  # the text out of the parentheses, with one space between words
    mpns: list[str] | None  # the items in the parentheses (no TBD); None: no parentheses
    error: str = ""  # the text does not follow the convention: a conflict


def split_value(text: str) -> ValueParts:
    """The general value and the MPNs of a Value: "2N7002 (2N7002LT1G, BSS138LT1G)"
    or "100nF 10V (GRM155R71A104KA01D)". One text in parentheses holds the MPNs, the
    fitted part first; an MPN in quotes can have each character. More texts in
    parentheses, a parenthesis that is not closed, or a list that is not clear is
    an error. A list that is not clear is one item, the full text of the
    parentheses (no MPN is lost); after an other error, the full text is the
    general value."""
    text = _filled(text)
    start = text.find("(")
    if start < 0 and ")" not in text:
        return ValueParts(" ".join(text.split()), None)
    error, items, end = "", None, 0
    if ")" in text[:max(start, 0)] or start < 0:
        error = "a parenthesis that is not opened"
    else:
        items, end, error = _list_items(text, start + 1, ")")
        if not error and "(" in text[end:]:
            error = "more than one text in parentheses"
        elif not error and ")" in text[end:]:
            error = "a parenthesis that is not opened"
        if error and error != NOT_CLEAR:
            items = None  # the MPNs are not known
    message = (f"Value {text!r}: {error}. Write the MPNs in one text in parentheses, for example "
               "'2N7002 (2N7002LT1G, BSS138LT1G)'") if error else ""
    if items is None:
        return ValueParts(" ".join(text.split()), None, message)
    items = [i for i in items if i.lower() not in NOT_SELECTED]  # TBD: an empty field
    # A list that is not clear (NOT_CLEAR) is one item, the full text: no MPN is lost.
    return ValueParts(" ".join((text[:start] + " " + text[end:]).split()), items, message)


def _value_key(text: str) -> tuple[str, tuple[str, ...] | None]:
    """A Value for a comparison (two Value columns, Value and Value_Confirmed): the
    words of the general value and the list of the MPNs (decision 15). The spaces
    between words, the separators of the list and the quotes that are not necessary
    do not count. The words and the case count: 10 k is not 10k, and 4m7 (milliohm)
    is not 4M7 (megaohm)."""
    parts = split_value(text)
    return parts.general, None if parts.mpns is None else tuple(parts.mpns)


def _mpns(items: list[str]) -> list[str]:
    """The MPNs of the items of a list: no "no part", "not selected" and KiCad texts."""
    return [i for i in items if i.lower() not in NO_PART + NOT_SELECTED and _is_field_mpn(i)]


def _no_part(items: list[str]) -> bool:
    """True if a list of MPNs tells that the symbol needs no part: a NO_PART text and
    no MPN."""
    return not _mpns(items) and any(i.lower() in NO_PART for i in items)


@dataclass
class CadSymbol:
    """The fields of one CAD symbol (or one line of a BOM file) for check_parts.
    See plans/confirmed-mpns.md for the model."""
    reference: str = ""
    value: str = ""  # a general value, and the MPNs in parentheses: 2N7002 (2N7002LT1G, BSS138LT1G)
    mpn: str = ""  # the field MPN: the confirmed copy of the MPNs in the parentheses of Value
    # The hidden field Value_Confirmed: the Value of the last confirmation. None: the
    # CAD data has no such field (a BOM with no column Value_Confirmed).
    value_confirmed: str | None = None
    datasheet: str = ""
    other_mpns: list[tuple[str, str]] = field(default_factory=list)  # (name, text) of more MPN columns
    other_values: list[str] = field(default_factory=list)  # the texts of more Value columns
    conflict: str = ""  # a conflict that the BOM reader found (columns of one role)
    # The name of an other variant of the product (its BOM file), or empty for the
    # default variant. A variant can set an other Value, MPN or DNP.
    variant: str = ""
    # The fields that a confirmation writes (see _write_fields): the MPN columns with the
    # MPN of the line (for example "Manufacturer Part Number"; more columns hold the same
    # MPNs), and the columns of Value_Confirmed (for example the custom column of
    # bom_confirmed_column). known False: the line has no value and the BOM has more
    # names of the role; the fields are the choices, and the user selects the field of
    # the symbol.
    mpn_fields: list[str] = field(default_factory=lambda: ["MPN"])
    mpn_known: bool = True
    confirmed_fields: list[str] = field(default_factory=lambda: ["Value_Confirmed"])
    confirmed_known: bool = True
    # The JLCPCB (LCSC) part number of the symbol (C47616), as it is. None: the CAD
    # data has no such field (a BOM with no LCSC column). lcsc_fields: the names of
    # the field to write.
    lcsc: str | None = None
    lcsc_fields: list[str] = field(default_factory=lambda: [LCSC_FIELD])


@dataclass
class SymbolState:
    """The parts of a CAD symbol and what the user must do with them."""
    parts: list[str]  # the MPNs that the project must hold, the fitted one first
    value_mpns: list[str]  # the MPNs in the parentheses of Value
    mpns: list[str]  # the confirmed MPNs (the field MPN)
    change: str = ""  # a difference to the confirmed fields: the user must confirm or correct it
    new: list[str] = field(default_factory=list)  # the MPNs of Value that the field MPN does not have
    removed: list[str] = field(default_factory=list)  # the confirmed MPNs that Value does not have
    write_mpn: str = ""  # the field MPN that a confirmation or a completion writes
    select: bool = False  # the user selects a new part first, and writes it into Value
    unconfirmed: bool = False  # the field MPN or Value_Confirmed is not written yet
    # The Value that completes the fields by the convention (decision 16): the MPNs of
    # the field MPN in parentheses. Empty: no completion.
    complete: str = ""
    no_part: bool = False  # the symbol needs no part (a NO_PART text)
    extra: list[str] = field(default_factory=list)  # the parts of a conflict of more columns (not alternatives)
    no_mpn: bool = False  # a general value with no selected part (and no NO_PART text)
    conflicts: list[str] = field(default_factory=list)


def symbol_state(symbol: CadSymbol) -> SymbolState:
    """The parts of one CAD symbol by the model of plans/confirmed-mpns.md
    (decisions 13 to 16). The model needs no data of datasheet-lens: the check is
    the same with and without it.

    Value holds a general value, and the MPNs in one text in parentheses (the
    fitted part first). The field MPN is the confirmed copy of these MPNs, and
    Value_Confirmed the Value of the last confirmation:

    - Value is not equal to Value_Confirmed (_value_key): a change. The confirmed
      MPNs stay the parts until the user confirms the change.
    - Value with parentheses: the same items as the field MPN are confirmed (with
      an empty Value_Confirmed: not confirmed). An empty field MPN: not confirmed,
      the MPNs of Value are the parts. An other list: a change.
    - A Value with no parentheses and a field MPN with MPNs: the tool completes the
      fields by the convention (the parts do not change). Not an empty Value.
    - A NO_PART text (-) tells that the symbol needs no part. A general value with
      no part: no part is selected.
    - More MPN columns or Value columns with other texts, a Value that does not
      follow the convention, a NO_PART text and an MPN in one list, or the text
      MIXED_VALUES of KiCad: a conflict, which the user corrects. The MPNs of more
      MPN columns and of more Value columns are parts too. A symbol with a
      conflict gets no proposal: a write can hide the conflict.
    - TBD is an empty field, also in the parentheses.
    """
    value = _filled(symbol.value)
    split = split_value(value)
    f_items, f_error = field_list(symbol.mpn)
    f_items = [i for i in f_items if i.lower() not in NOT_SELECTED]  # TBD: an empty field
    mpns = _unique(_mpns(f_items))
    v_items = split.mpns
    v_mpns = _unique(_mpns(v_items or []))
    conflicts = [split.error] if split.error else []
    if f_error:
        conflicts.append(f"MPN {_filled(symbol.mpn)!r}: {f_error}")
    for name, items in (("MPN", f_items), ("Value", v_items or [])):
        words = [i for i in items if not _is_field_mpn(i)]
        if words:
            conflicts.append(f"{name} {', '.join(words)}: not an MPN")
        marks = [i for i in items if i.lower() in NO_PART]
        if marks and _mpns(items):  # "needs no part" and an MPN in one list
            conflicts.append(f"{name} {', '.join(items)}: {', '.join(marks)} tells that the symbol needs no part")
    # More MPN columns must hold the same MPNs (no regard to signs): an other MPN,
    # also a longer code of one part, is a conflict and a part too (no MPN of an
    # other column is lost or replaces a confirmed MPN).
    other_mpns: list[str] = []
    for name, text in symbol.other_mpns:
        other_error = field_list(text)[1]
        if other_error:
            conflicts.append(f"{name} {_filled(text)!r}: {other_error}")
        other = mpn_items(text)
        if [_norm(i) for i in other] != [_norm(i) for i in mpn_items(symbol.mpn)]:
            conflicts.append(f"MPN {_filled(symbol.mpn) or '(empty)'}, {name} {_filled(text)}")
        other_mpns += [i for i in other if _is_field_mpn(i)]
    # More Value columns: bom_parts gives the conflict of different values. The MPNs of
    # a different one are parts too; none replaces a confirmed MPN.
    other_values = [m for text in symbol.other_values if _value_key(text) != _value_key(value)
                    for m in _mpns(split_value(text).mpns or [])]
    state = SymbolState(parts=list(mpns), value_mpns=v_mpns, mpns=mpns,
                        conflicts=conflicts + ([symbol.conflict] if symbol.conflict else []))
    state.no_part = _no_part(f_items) if f_items else _no_part(v_items or [])
    has_field = symbol.value_confirmed is not None
    confirmed = _filled(symbol.value_confirmed or "")
    if state.conflicts:
        # The user corrects the fields first: a write of a proposal can hide the
        # conflict. The parts: the confirmed MPNs, else the MPNs of Value (none is lost).
        state.parts = list(mpns) or list(v_mpns)
    elif has_field and confirmed and _value_key(value) != _value_key(confirmed):
        if v_items is not None:
            state.write_mpn = format_mpns(v_items)
            state.new = [m for m in v_mpns if m not in mpns]
            state.removed = [m for m in mpns if m not in v_mpns]
        elif state.no_part:
            state.write_mpn = _filled(symbol.mpn)  # the user keeps the NO_PART text
        else:  # a new general value, or no MPN in Value now: the user selects the part
            state.select = True
            state.removed = list(mpns)  # the new part replaces the confirmed MPN
        now = (f"the confirmed MPN {_filled(symbol.mpn)!r} can be wrong now" if mpns else
               "the symbol needs no part (MPN " + repr(_filled(symbol.mpn)) + "): this can be wrong now"
               if state.no_part else "no MPN is confirmed")
        state.change = f"Value {value!r}, Value_Confirmed {confirmed!r}: {now}"
    elif v_items is not None:
        state.write_mpn = format_mpns(v_items)
        state.new = [m for m in v_mpns if m not in mpns]
        state.removed = [m for m in mpns if m not in v_mpns]
        if v_items == f_items:
            state.unconfirmed = has_field and not confirmed and bool(v_items)  # (TBD): nothing to confirm
        elif not f_items and not (has_field and confirmed):  # the first confirmation
            state.parts, state.unconfirmed = list(v_mpns), True
        else:  # also a field MPN that is empty now
            state.change = (f"Value {value!r}, confirmed MPN {_filled(symbol.mpn) or '(empty)'!r}: the MPNs in the "
                            "parentheses are not the confirmed MPNs")
    elif value and mpns and _mpns(f_items) == f_items:
        # Only MPNs in the field: no conflict to copy into Value. An empty Value can be
        # a BOM with no Value column (Altium: Comment): the tool does not know it.
        state.write_mpn = format_mpns(mpns)  # each MPN one time
        state.complete = f"{value} ({format_mpns(mpns, value=True)})"
    state.extra = [m for m in _unique(other_mpns + other_values) if not any(_norm(m) == _norm(p) for p in state.parts)]
    state.parts = _unique(state.parts + state.extra)
    state.no_mpn = (not state.parts and not state.no_part and bool(value) and not state.change
                    and not state.conflicts)
    return state


def check_parts(project: Project, store: Store, cad: list[CadSymbol | dict]) -> dict:
    """Compare the parts of the project with the parts of the CAD project. cad:
    one CadSymbol for each CAD symbol or BOM line, or a dict with its keys. Each
    part of a symbol needs its datasheet (see symbol_state). Give:
    - ok: the MPNs that the project has.
    - different: {"cad", "mpn", "manufacturer", "ambiguous"} for a project part
      that is probably the same part with a different MPN (see _near).
      ambiguous: the project has more parts with this MPN.
    - missing: the CAD parts that the project does not have, with the
      references of the CAD symbols, the Datasheet field and the documents
      that the store has for the MPN.
    - extra: the project parts that the CAD project does not have.
    - conflicts: the symbols whose fields disagree (more MPN columns, columns of
      one role, a field MPN with a text that is not an MPN).
    - changes: {"reference", "variant", "change", "new", "removed", "mpn",
      "value_confirmed", "select", "mpn_fields", "mpn_known", "confirmed_fields",
      "confirmed_known"} for each symbol whose Value is not equal to its
      confirmed fields; mpn and value_confirmed are the texts that a confirmation
      writes (into the fields of the variant, when variant is not empty), into the
      fields of mpn_fields and confirmed_fields (see CadSymbol). select: the user
      selects a new part first, and writes it into Value.
    - unconfirmed: {"reference", "variant", "new", "removed", "mpn",
      "value_confirmed", "mpn_fields", "mpn_known", "confirmed_fields",
      "confirmed_known"} for each symbol with no field MPN or Value_Confirmed.
    - complete: {"reference", "variant", "value", "mpn", "value_confirmed",
      "mpn_fields", "mpn_known", "confirmed_fields", "confirmed_known"} for
      each symbol with an MPN that is not in Value (plans/confirmed-mpns.md,
      decision 16): the fields that complete it by the convention. value_confirmed
      is None when the CAD data has no field Value_Confirmed.
    - lines: the number of CAD symbols with a part (a reference of more BOM files
      of variants counts one time).
    - no_mpn: the references of the symbols with a general value and no part.
    - no_value_confirmed: the number of symbols with no field Value_Confirmed in a
      BOM file of one or more variants (a change of a general value is not found
      for them).
    An alternative is a CAD part too, and needs its own datasheet: the Datasheet
    field of the symbol belongs to the fitted part. With more variants, each line
    needs its reference (LensError)."""
    # The BOM files of more variants give one symbol more times, the default variant
    # first: the line of a variant gives only the messages that the line of the
    # default variant of its reference does not give.
    default: dict[str, set[tuple[str, str]]] = {"conflicts": set(), "changes": set(), "unconfirmed": set(),
                                                "complete": set(), "no_mpn": set()}
    out_lists: dict[str, list] = {kind: [] for kind in default}

    def add(kind: str, symbol: CadSymbol, text: object, item) -> None:
        # The references as a set: a line of a group (Q1,Q2) and a line of one symbol
        # (Q1) of an other variant file are the same symbols.
        keys = {(ref, text) for ref in _references(symbol.reference)}
        if not symbol.variant:
            default[kind] |= keys
        elif keys and keys <= default[kind]:
            return
        out_lists[kind].append(item)

    merged: dict[str, dict] = {}
    no_confirmed = lines = 0
    # The references of the lines with a part, and of the lines with no field
    # Value_Confirmed: one time for all BOM files of the variants.
    counted: set[str] = set()
    unconfirmed_refs: set[str] = set()
    default_datasheets: dict[str, str] = {}  # reference -> the Datasheet field of the default variant
    symbols = [item if isinstance(item, CadSymbol) else CadSymbol(
        **{k: str(item.get(k) or "") for k in ("reference", "value", "mpn", "datasheet", "variant")},
        value_confirmed=None if item.get("value_confirmed") is None else str(item["value_confirmed"]),
        mpn_fields=_field_names(item.get("mpn_fields"), "MPN"),
        confirmed_fields=_field_names([item.get("confirmed_field")], "Value_Confirmed"),
        lcsc=None if item.get("lcsc") is None else str(item["lcsc"]),
        lcsc_fields=_field_names([item.get("lcsc_field")], LCSC_FIELD))
        for item in cad]
    lcsc_symbols: list[tuple[str, CadSymbol, str]] = []  # (label, symbol, the fitted MPN)
    lcsc_seen: set = set()
    # The tool finds the line of the default variant of a variant line by its
    # reference: with no reference, a message and a count come two times.
    if any(s.variant for s in symbols):
        for s in symbols:
            if not _references(s.reference) and (_filled(s.value) or _filled(s.mpn) or s.conflict):
                where = f"variant {s.variant}" if s.variant else "the default variant"
                raise LensError(f"{where}: a line with no reference (Value {_filled(s.value)!r}, MPN "
                                f"{_filled(s.mpn)!r}): with more variants, each line needs its reference")
    # The lines of the default variant first (sorted is stable): see add().
    for symbol in sorted(symbols, key=lambda s: bool(s.variant)):
        reference = symbol.reference.strip()
        refs = _references(reference)
        if (symbol.value_confirmed is None and not reference.startswith("#")
                and (_filled(symbol.value) or _filled(symbol.mpn))
                and (not refs or not refs <= unconfirmed_refs)):
            unconfirmed_refs |= refs
            no_confirmed += 1
        label = (reference or "a symbol") + (f" (variant {symbol.variant})" if symbol.variant else "")
        state = symbol_state(symbol)
        if state.parts and (not refs or not refs <= counted):
            counted |= refs
            lines += 1
        for c in state.conflicts:
            add("conflicts", symbol, c, f"{label}: {c}")
        # The fields that a confirmation writes: the MPNs of the parentheses of Value.
        write_mpn = "<the MPN of the new part>" if state.select else state.write_mpn
        value = _filled(symbol.value)
        has_field = symbol.value_confirmed is not None
        write = {"reference": label, "variant": symbol.variant, "mpn": write_mpn,
                 "value_confirmed": ("<the new Value>" if state.select else value) if has_field else None,
                 "mpn_fields": symbol.mpn_fields, "mpn_known": symbol.mpn_known,
                 "confirmed_fields": symbol.confirmed_fields, "confirmed_known": symbol.confirmed_known}
        # A variant line that writes into other fields than the default line is no
        # duplicate.
        target = (tuple(symbol.mpn_fields), symbol.mpn_known, has_field, tuple(symbol.confirmed_fields),
                  symbol.confirmed_known)
        if reference.startswith("#"):
            pass  # a power symbol (#PWR01) has no part to confirm
        elif state.change:
            add("changes", symbol, (state.change, target), {**write, "change": state.change, "new": state.new,
                                                            "removed": state.removed, "select": state.select})
        elif state.unconfirmed:
            add("unconfirmed", symbol, (write_mpn, value, target),
                {**write, "new": state.new if state.mpns else [], "removed": state.removed})
        elif state.complete:
            add("complete", symbol, (state.complete, target),
                {**write, "value": state.complete,
                 "value_confirmed": state.complete if has_field else None})
        if state.no_mpn and not reference.startswith("#"):  # not a power symbol (#PWR01)
            add("no_mpn", symbol, "", label)
        # The JLCPCB number of the fitted part (a variant line with the same number
        # and part as its default line gives no second message).
        key = (frozenset(refs) or label, (symbol.lcsc or "").strip().upper(), state.parts[0] if state.parts else "")
        if state.parts and not state.no_part and not reference.startswith("#") and key not in lcsc_seen:
            lcsc_seen.add(key)
            lcsc_symbols.append((label, symbol, state.parts[0]))
        # The Datasheet field of a variant line that is the field of the default variant
        # is inherited (KiCad 10): it is not the datasheet of an other part of the variant.
        datasheet = _filled(symbol.datasheet)
        if not symbol.variant:
            default_datasheets.update(dict.fromkeys(refs, datasheet))
        elif refs and all(default_datasheets.get(r) == datasheet for r in refs):
            datasheet = ""
        mpns = state.parts
        for i, mpn in enumerate(mpns):
            # One part with two spellings (signs, case) is one CAD part: the first spelling.
            entry = merged.setdefault(_norm(mpn), {"mpn": mpn, "references": [], "datasheet": "",
                                                    "alternative_of": mpns[0] if i and mpn not in state.extra
                                                    else ""})
            if i == 0:
                entry["alternative_of"] = ""  # a main part of a different symbol
            if reference and reference not in entry["references"]:
                entry["references"].append(reference)
            # In a change, the Datasheet field can be of the new part of Value already.
            # Not with a confirmed MPN that Value does not have: the field can be of Value.
            if i == 0 and not state.change and not state.removed and not entry["datasheet"] and datasheet:
                entry["datasheet"] = datasheet
    out = {"ok": [], "different": [], "missing": [], "extra": [], **out_lists, "no_value_confirmed": no_confirmed,
           "lines": lines}
    # First the exact matches, then the near matches of the other parts: a part
    # that is the same is not also "different".
    cad_mpns = {e["mpn"].lower() for e in merged.values()}
    exact = {id(p) for p in project.parts if p["mpn"].lower() in cad_mpns}
    out["ok"] = [e["mpn"] for e in merged.values() if any(p["mpn"].lower() == e["mpn"].lower() for p in project.parts)]
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
    # A CAD MPN that is a project part with an other MPN (different): its purchase source.
    renamed = {d["cad"].lower(): d["mpn"] for d in out["different"] if not d["ambiguous"]}
    missing = {m["mpn"].lower() for m in out["missing"]}
    out["lcsc"], out["lcsc_ok"] = _lcsc_findings(project, lcsc_symbols, any(s.lcsc is not None for s in symbols),
                                                 renamed, missing)
    return out


def _lcsc_findings(project: Project, symbols: list[tuple[str, CadSymbol, str]], bom_column: bool,
                   renamed: dict[str, str] | None = None, missing: set[str] | None = None) -> tuple[list, int]:
    """The JLCPCB (LCSC) numbers of the CAD symbols, compared with the JLCPCB
    purchase sources of the project parts and with the JLCPCB parts library. Only
    in a project that uses JLCPCB: a part has a JLCPCB purchase source, or the CAD
    data has an LCSC field. renamed: the CAD MPNs of project parts with an other
    MPN (check_parts "different"); missing: the CAD MPNs that the project does not
    have (check_parts gives them as missing: no line here about a number to
    select). The numbers compare by their ID (C047616 is C47616). Gives the
    findings ({"kind", "reference", "mpn", "bom", "source", "fields", "library"})
    and the number of symbols whose number agrees with the purchase source. The
    kinds: write (the purchase source is not in the field), no_source,
    different, not_number, not_in_library, other_mpn (the number is a different
    MPN in the library), none (no number at all)."""
    from . import jlcpcb
    from .distributors import fits, jlcpcb_ready, source_numbers

    if not (bom_column or any(source_numbers(p, "jlcpcb") for p in project.parts)):
        return [], 0
    renamed, missing = renamed or {}, missing or set()

    def normal(number: str) -> str:
        """C and the ID of a JLCPCB number, or "" for a text that is not one."""
        try:
            return f"C{jlcpcb.number_id(number)}" if number.strip() else ""
        except LensError:
            return ""

    db = None
    if jlcpcb_ready():
        with contextlib.suppress(LensError, sqlite3.Error):
            db = jlcpcb.connect()
    findings, ok = [], 0
    try:
        for label, symbol, mpn in symbols:
            part = project.find(mpn) or (project.find(renamed[mpn.lower()]) if mpn.lower() in renamed else None)
            sources = source_numbers(part, "jlcpcb") if part is not None else []
            normal_sources = [normal(s) or s for s in sources]
            text = (symbol.lcsc or "").strip()
            bom = normal(text)
            item = {"reference": label, "mpn": mpn, "part": part["mpn"] if part is not None else mpn,
                    "bom": bom or text, "source": ", ".join(sources), "fields": symbol.lcsc_fields, "library": ""}
            found: list[dict] = []  # the findings of this symbol
            if text and not bom:
                found.append({**item, "kind": "not_number"})
            elif bom and sources:
                if bom in normal_sources:
                    ok += 1
                else:
                    found.append({**item, "kind": "different"})
            elif bom and part is not None:
                found.append({**item, "kind": "no_source"})
            elif sources and not text:
                # More numbers (packagings): the user selects the one of the symbol.
                found.append({**item, "kind": "write" if len(sources) == 1 else "select"})
            elif not text and part is not None and mpn.lower() not in missing:
                found.append({**item, "kind": "none"})
            # The library: each number is the MPN of the symbol (also the purchase
            # sources when the field has a text that is not a number).
            numbers = [n for n in dict.fromkeys([bom, *(normal(s) for s in sources)]) if n]
            for number in numbers if db is not None else []:
                try:
                    offer = jlcpcb.offer_of_number(number, db)
                except (LensError, sqlite3.Error):
                    continue
                if offer is None:
                    found.append({**item, "kind": "not_in_library", "library": number})
                elif not fits(offer.mpn, mpn, True) and not (part is not None and fits(offer.mpn, part["mpn"], True)):
                    found.append({**item, "kind": "other_mpn", "library": number,
                                  "other": f"{offer.mpn} ({offer.manufacturer or 'manufacturer unknown'})"})
            # The BOM is the master. A number of the field that the library does not give
            # for this MPN cannot be processed: no source to save, the user decides.
            wrong = {f["library"] for f in found if f["kind"] in ("not_in_library", "other_mpn")}
            findings += [{**f, "bom_wrong": f["bom"] in wrong} for f in found
                         if not (f["kind"] == "no_source" and f["bom"] in wrong)]
    finally:
        if db is not None:
            db.close()
    return findings, ok


# Placeholders at the end of a symbol name: any characters (STM32F103C8Tx, nRF9151-LAXX).
PLACEHOLDER = re.compile(r"(x+|X{2,})$")


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
    stem = PLACEHOLDER.sub("", cad).lower() or key
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
# The column of the JLCPCB (LCSC) part number: the names that the KiCad plugins
# for JLCPCB use (Fabrication Toolkit, kicad-jlcpcb-bom-plugin), checked
# 2026-11-07. JLCPCB has no official list.
LCSC_COLUMNS = ("LCSC", "LCSC Part #", "LCSC Part Number", "LCSC Part", "LCSC PN", "LCSC P/N", "LCSC Part No.",
                "JLCPCB Part #", "JLCPCB Part", "JLCPCB PN", "JLCPCB P/N", "JLCPCB Part No.", "JLCPCB")
LCSC_FIELD = "LCSC"  # the field to write when the BOM has no LCSC column (decision of the user)


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
    """The names of the options column, delimiter, quote and confirmed in the
    interface of the caller, with a general name for a name that the caller does
    not give."""
    return {key: (options or {}).get(key, f"the {key} option")
            for key in ("column", "delimiter", "quote", "confirmed")}


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
    cad: list[CadSymbol]  # one item for each fitted line with a Value, an MPN or a conflict
    summary: str  # one line: the columns that the parts come from


def bom_files(files: list[str], column: str = "", delimiter: str = "", quote: str = "",
              options: dict[str, str] | None = None, confirmed_column: str = "", base: str = "") -> list[BomParts]:
    """The CAD parts of the BOM files of the variants of a product (see bom_parts):
    the first file is the default variant, and the path of each other file, as the
    caller gives it, names its variant. base: the folder of a relative path."""
    return [bom_parts(str(Path(base or ".").expanduser() / Path(f).expanduser()), column, delimiter, quote, options,
                      variant=f if i else "", confirmed_column=confirmed_column)
            for i, f in enumerate(files)]


def _write_fields(names: list[str], filled: list[int], indexes: list[int], default: str,
                  group: bool = False) -> tuple[list[str], bool]:
    """The fields that a confirmation writes into a symbol of a BOM line, and True if
    they are known. names: the column names (the BOM must name the columns as the
    fields, for example kicad-cli with no labels); filled: the columns of the role with
    a value in the line; indexes: all columns of the role; group: the line has more
    symbols. The filled columns of the line (they hold the same value, else the line
    has a conflict and no proposal); in a line of more symbols, more filled names can
    come from different symbols: not known. With no value: the one name of the role,
    or the default name when the BOM has no column of the role. More names (for
    example MP and MANUFACTURER_PART_NUMBER of two Eagle libraries): not known; the
    names are the choices, and the user selects the field that the symbol uses."""
    if filled:
        found = list(dict.fromkeys(names[i] for i in filled))
        return found, not (group and len(found) > 1)
    choices = list(dict.fromkeys(names[i] for i in indexes))
    return (choices, False) if len(choices) > 1 else (choices or [default], True)


def _field_names(names: list | None, default: str) -> list[str]:
    """The field names of a part of project_check(parts): the names with no outer
    spaces, or the default name."""
    found = [n.strip() for n in names or [] if isinstance(n, str) and n.strip()]
    return list(dict.fromkeys(found)) or [default]


def bom_parts(path: str, column: str = "", delimiter: str = "", quote: str = "",
              options: dict[str, str] | None = None, variant: str = "", confirmed_column: str = "") -> BomParts:
    """Read the CAD parts of a BOM file (CSV), for example from "kicad-cli sch
    export bom". column: the column of the MPN; empty: each column of
    MPN_COLUMNS that the BOM has: the first filled one is the field MPN of the
    line, and the others must hold the same MPNs (also more
    columns of one name, for example MPN and mpn of symbols from different
    libraries). The Value column is always read (alternatives, conflicts). A line
    with a value in the DNP column is not fitted: it gives no part. The values
    empty, 0, no and false mean fitted.

    More Value, DNP, Reference or Datasheet columns (also with different names of
    one list, for example DNP and "Do not populate"): the first filled one gives
    the value, and a line in which they are different gets a conflict. A line is
    not fitted only when each DNP column says so; thus the line of a conflict
    needs its datasheet. All MPNs of more Value columns count (see
    CadSymbol.other_values).

    delimiter, quote: the values of the options of the interface (see
    bom_format). variant: the name of an other variant of the product, when the
    file is its BOM (see CadSymbol.variant). confirmed_column: the column of the
    field Value_Confirmed, for example an unused custom column of the BOM of an
    other CAD tool; empty: Value_Confirmed. options: the names of the options
    column, delimiter, quote and confirmed in the interface of the caller, for the
    errors."""
    options = _option_names(options)
    quote_given = bool(quote)
    delimiter, quote = bom_format(delimiter, quote, options)
    names, rows, encoding = _read_bom(path, delimiter, quote, column, options, quote_given)
    if not names:
        raise LensError(f"{path}: the BOM file has no header line: export the BOM again")
    # A name that more columns have gets the number of its column.
    labels = [f"{n} (column {i + 1})" if n and names.count(n) > 1 else n for i, n in enumerate(names)]
    shown = [label for label in labels if label]
    roles = {"Value": ["Value"], "DNP": DNP_COLUMNS, "Reference": REFERENCE_COLUMNS, "Datasheet": DATASHEET_COLUMNS,
             "Value_Confirmed": [confirmed_column or "Value_Confirmed"], "LCSC": LCSC_COLUMNS}
    # Each column of each name of a role, for example DNP and "Do not populate".
    keys = [_column_key(n) for n in names]
    found = {role: list(dict.fromkeys(i for w in wanted for i in _bom_columns(keys, w)))
             for role, wanted in roles.items()}
    value_columns = found["Value"]
    if confirmed_column and not found["Value_Confirmed"]:
        raise LensError(f"{path}: no column {confirmed_column!r} for {options['confirmed']} ({', '.join(shown)})")
    if column:
        matched = _bom_columns(keys, column)
        if not matched:
            raise LensError(f"{path}: no column {column!r} ({', '.join(shown)})")
        field_columns = [i for i in matched if i not in value_columns]
        if not field_columns:
            raise LensError(f"{path}: the column {column!r} is the column Value, which the tool always reads as "
                            f"Value: give the column of the MPN with {options['column']}, or no column")
    else:
        field_columns = [i for w in MPN_COLUMNS if _column_key(w) != "value"
                         for i in _bom_columns(keys, w) if i not in value_columns]
    # The column of Value_Confirmed is not a column of an other role: Value would be
    # equal to it in each line, and each symbol would be confirmed.
    clash = next((role for role, indexes in (
        ("Value", value_columns), ("MPN", field_columns), ("Reference", found["Reference"]),
        ("Datasheet", found["Datasheet"]), ("DNP", found["DNP"])) if set(found["Value_Confirmed"]) & set(indexes)), "")
    if clash and confirmed_column:
        raise LensError(f"{path}: the column {confirmed_column!r} of {options['confirmed']} is the column of "
                        f"{clash}: give the column of the field Value_Confirmed")
    if clash:  # only the column option can name the column Value_Confirmed
        raise LensError(f"{path}: the column {column!r} of {options['column']} is the column of the field "
                        "Value_Confirmed: give the column of the MPN")
    columns = list(dict.fromkeys(labels[i] for i in field_columns + value_columns))
    if not columns:
        hint = (f"; if the file uses the quote ', give it with {options['quote']}"
                if quote == '"' and any(n.strip().startswith("'") for n in names) else "")
        raise LensError(f"{path}: no MPN column; give the column of the MPN with {options['column']} "
                        f"({', '.join(shown)}){hint}")

    # How the values of more columns of one role compare: a value by its words
    # (_value_key: 100nF and 100 nF are different; case counts: 4m7, 4M7), the references as a set (R1 R2, "R1, R2"),
    # a datasheet as text.
    compare = {"Value": _value_key,
               "Reference": _references,
               "Datasheet": lambda v: v,
               "Value_Confirmed": _value_key,
               "LCSC": lambda v: v.strip().upper()}

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
        # Two Value columns with different values are a conflict, also two codes of
        # one part (TLV755P, TLV75533PDBVR): the user corrects the columns.
        values = [(labels[i], _filled(row[i])) for i in value_columns if _filled(row[i])]
        value_conflict = ", ".join(f"{label} {text}" for label, text in values) if len(
            {compare["Value"](text) for _, text in values}) > 1 else ""
        reference, reference_conflict = role(row, "Reference")
        datasheet, datasheet_conflict = role(row, "Datasheet")
        confirmed, confirmed_conflict = role(row, "Value_Confirmed")
        confirmed = confirmed if found["Value_Confirmed"] else None
        lcsc, lcsc_conflict = role(row, "LCSC")
        # An empty DNP value is a state too (fitted).
        dnp_conflict = (", ".join(f"{labels[i]} {_filled(row[i]) or '(empty)'}" for i in found["DNP"])
                        if len(set(states)) > 1 else "")
        # The field MPN: the first filled MPN column (Eagle: each library has its own
        # attribute); the other filled ones must hold the same MPNs.
        # A NO_PART text (-) is the field MPN only when no column has an MPN.
        filled = [i for i in field_columns if mpn_items(row[i])]
        marked = [i for i in field_columns
                  if not mpn_items(row[i]) and any(x.lower() in NO_PART for x in field_items(row[i]))]
        filled_mpns = [(labels[i], _filled(row[i])) for i in filled]
        marks = [(labels[i], _filled(row[i])) for i in marked]
        field_mpn = filled_mpns[0][1] if filled_mpns else marks[0][1] if marks else ""
        # A column that tells "no part" and a column with an MPN: a conflict.
        mark_conflict = ", ".join(f"{label} {text}" for label, text in filled_mpns[:1] + marks) if (
            filled_mpns and marks) else ""
        symbol = CadSymbol(
            reference=reference, value=values[0][1] if values else "", mpn=field_mpn,
            value_confirmed=confirmed, datasheet=datasheet, other_mpns=filled_mpns[1:],
            other_values=[text for _, text in values[1:]], variant=variant,
            lcsc=lcsc if found["LCSC"] else None,
            lcsc_fields=list(dict.fromkeys(names[i] for i in found["LCSC"])) or [LCSC_FIELD],
            conflict="; ".join(c for c in (value_conflict, mark_conflict, dnp_conflict, reference_conflict,
                                           datasheet_conflict, confirmed_conflict, lcsc_conflict) if c))
        group = len(_references(reference)) > 1
        symbol.mpn_fields, symbol.mpn_known = _write_fields(names, filled or marked, field_columns, "MPN", group)
        symbol.confirmed_fields, symbol.confirmed_known = _write_fields(
            names, [i for i in found["Value_Confirmed"] if _filled(row[i])], found["Value_Confirmed"],
            "Value_Confirmed", group)
        if symbol.value or symbol.mpn or symbol.conflict:
            cad.append(symbol)
    references = ", ".join(labels[i] for i in found["Reference"])
    confirmed_columns = ", ".join(labels[i] for i in found["Value_Confirmed"])
    lcsc_columns = ", ".join(labels[i] for i in found["LCSC"])
    mpn_columns = ", ".join(dict.fromkeys(labels[i] for i in field_columns)) or "(no column)"
    values = ", ".join(dict.fromkeys(labels[i] for i in value_columns))
    summary = (f"{path}: MPN from {mpn_columns}" + (f"; Value from {values}" if values else "")
               + (f"; references from {references}" if references else "")
               + (f"; Value_Confirmed from {confirmed_columns}" if confirmed_columns else "; no column Value_Confirmed")
               + (f"; LCSC from {lcsc_columns}" if lcsc_columns else "")
               + ("; read as cp1252" if encoding == "cp1252" else ""))
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
    again = "datasheet-lens check" if cli else "project_check"
    # A new part: by its parameters (a general value such as 10k or 220nF), or by its MPN.
    codes = ("datasheet-lens pick, datasheet-lens find, datasheet-lens codes" if cli
             else "find_parts by its parameters, search_distributors and get_ordering_codes by its MPN")

    def target(item: dict) -> str:
        """Where a confirmation writes: a variant of KiCad 10 has its own fields."""
        variant = item.get("variant")
        return (f"into the fields of the symbol in the variant of the BOM file {variant} (not into the fields "
                "of the default variant)" if variant else "into the symbol")

    def assign(names: list[str], known: bool, text: str, what: str) -> list[str]:
        """The fields of one role and their text; a field that is not known names the
        choices (see CadSymbol)."""
        if known:
            return [f"{name} = '{text}'" for name in names]
        return [f"{what} = '{text}' (the BOM has the columns {', '.join(names)}: ask the user which one the "
                "symbol uses)"]

    def fields(item: dict, value: bool = False) -> str:
        """The fields to write; Value_Confirmed only when the CAD data has the field."""
        texts = (([f"Value = '{item['value']}'"] if value else [])
                 + assign(item["mpn_fields"], item["mpn_known"], item["mpn"], "the MPN field")
                 + (assign(item["confirmed_fields"], item["confirmed_known"], item["value_confirmed"],
                           "the field Value_Confirmed") if item["value_confirmed"] is not None else []))
        return (", ".join(texts[:-1]) + " and " + texts[-1]) if len(texts) > 1 else texts[0]

    for c in result.get("changes", []):
        new = f" New: {', '.join(c['new'])}." if c["new"] else ""
        removed = f" Not in Value now: {', '.join(c['removed'])}." if c["removed"] else ""
        first = (f" First select the new part with the user ({codes}), and write its MPN into Value in "
                 "parentheses ('100nF 10V (GRM155R71A104KA01D)')." if c.get("select") else "")
        lines.append(f"change in the CAD project: {c['reference']}: {c['change']}.{new}{removed} Tell the user. "
                     f"When the user confirms the change:{first} Write {fields(c)} {target(c)}, then run {again} "
                     "again: it gives the next steps for the new parts, and the parts that no symbol uses are extra. "
                     "Else the Value must change back.")
    for u in result.get("unconfirmed", []):
        new = f" Not in MPN: {', '.join(u['new'])}." if u.get("new") else ""
        removed = f" Not in Value: {', '.join(u['removed'])}." if u.get("removed") else ""
        lines.append(f"not confirmed: {u['reference']}:{new}{removed} When the user agrees: Write {fields(u)} "
                     f"{target(u)}.")
    for c in result.get("complete", []):
        lines.append(f"complete the fields: {c['reference']}: the MPN is not in Value (the convention: the MPNs in "
                     f"parentheses in Value). The parts do not change. Write {fields(c, value=True)} {target(c)}.")
    if result.get("no_value_confirmed"):
        option = "--confirmed-column" if cli else "bom_confirmed_column"
        lines.append(f"no field Value_Confirmed: {result['no_value_confirmed']} CAD parts. A change of a general "
                     "value (100nF to 220nF) is not found. Tell the user: add the hidden field Value_Confirmed to the "
                     f"symbols and to the BOM, or use an unused custom column of the BOM and give its name with "
                     f"{option}." + ("" if cli else " With parts: give value_confirmed of each part."))
    if result.get("no_mpn"):
        find = ("datasheet-lens pick, datasheet-lens find" if cli
                else "find_parts by its parameters, search_distributors by its MPN")
        lines.append(f"no part selected (a general value and no MPN): {', '.join(result['no_mpn'])}. A part that "
                     f"needs a datasheet: select it with the user ({find}), then write its MPN into Value in "
                     "parentheses ('10k (RC0603FR-0710KL)'), into the MPN field of the symbol, and the new Value into "
                     "its field Value_Confirmed. A "
                     "symbol that needs no part (a test point, a generic resistor): MPN = '-'.")
    if "lines" in result:
        lines.append(f"CAD lines with a part: {result['lines']}")
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
                    + (f"datasheet-lens add \"{m['datasheet']}\" -m \"{m['mpn']}\" -b \"<the part of the datasheet>\"" if cli
                       else f"add_document(path='{m['datasheet']}'), then {add(m['mpn'], '<base part>', '<doc>')}"))
        else:
            step = ("the store has no datasheet, and the CAD part has no Datasheet field: find the datasheet "
                    + (f'(datasheet-lens links "{m["mpn"]}")' if cli else f"(part_links(mpn='{m['mpn']}'))")
                    + ", or tell the user")
        lines.append(f"missing: {m['mpn']}{refs}: {step}")
    for mpn in result["extra"]:
        lines.append(f"extra: {mpn}: the CAD project does not have it. Tell the user; remove it only when the "
                     "user agrees.")
    lines += _lcsc_lines(result, cli)
    return lines


def _lcsc_lines(result: dict, cli: bool) -> list[str]:
    """The lines of the JLCPCB (LCSC) numbers of check_parts (_lcsc_findings)."""
    findings = result.get("lcsc", [])
    if not findings and not result.get("lcsc_ok"):
        return []
    lines = [f"LCSC: the JLCPCB numbers that agree with the purchase sources: {result.get('lcsc_ok', 0)}"]

    def adopt(number: str) -> str:
        return f"datasheet-lens adopt jlcpcb {number}" if cli else f"adopt_part(source='jlcpcb', number='{number}')"

    def remove(mpn: str, number: str) -> str:
        return (f'datasheet-lens rm "{mpn}" -s {number}' if cli
                else f"project_remove_source(mpn='{mpn}', number='{number}')")

    none = []
    for f in findings:
        ref, mpn, kind = f["reference"], f["mpn"], f["kind"]
        if kind == "write":
            write = " and ".join(f"{name} = '{f['source']}'" for name in f["fields"])
            lines.append(f"LCSC: {ref}: the field has no JLCPCB number; the purchase source of {mpn} is "
                         f"{f['source']}. Write {write} into the symbol.")
        elif kind == "no_source":
            lines.append(f"LCSC: {ref}: the field gives {f['bom']} for {mpn}, and the project has no JLCPCB purchase "
                         f"source. The BOM is the master: save it with {adopt(f['bom'])}.")
        elif kind == "different" and f.get("bom_wrong"):
            lines.append(f"LCSC: {ref}: the field gives {f['bom']}, the JLCPCB purchase source of {mpn} is "
                         f"{f['source']}, and the library does not give {f['bom']} for {mpn} (see the next line). "
                         "The BOM cannot be the master here: ask the user for the correct number.")
        elif kind == "different":
            removes = " and ".join(remove(f["part"], n) for n in f["source"].split(", "))
            lines.append(f"LCSC: {ref}: the field gives {f['bom']}, the JLCPCB purchase source of {mpn} is "
                         f"{f['source']}. The BOM is the master: save {f['bom']} with {adopt(f['bom'])}, and "
                         f"remove the purchase sources that the BOM does not use with {removes}.")
        elif kind == "select":
            lines.append(f"LCSC: {ref}: the field has no JLCPCB number; {mpn} has more JLCPCB purchase sources "
                         f"({f['source']}). Ask the user which one the board uses, and write it into "
                         + " and ".join(f["fields"]) + " of the symbol.")
        elif kind == "not_number":
            lines.append(f"LCSC: {ref}: the field has '{f['bom']}': not a JLCPCB number (C and digits, for example "
                         "C47616). Tell the user.")
        elif kind == "not_in_library":
            update = "datasheet-lens jlc update" if cli else "jlcpcb_library(action='update'), when the user agrees"
            lines.append(f"LCSC: {ref}: the JLCPCB parts library does not have {f['library']} (a wrong number, or "
                         f"an old database: {update}).")
        elif kind == "other_mpn":
            lines.append(f"LCSC: {ref}: {f['library']} is {f['other']} in the JLCPCB parts library, not {mpn}. "
                         "Tell the user: the number or the MPN is wrong.")
        elif kind == "none":
            none.append(f"{ref} ({mpn})")
    if none:
        find = "datasheet-lens pick jlcpcb, datasheet-lens find" if cli else "find_parts with source jlcpcb, " \
            "search_distributors"
        lines.append(f"LCSC: no JLCPCB number: {', '.join(none)}. For the JLCPCB assembly, select the part of the "
                     f"JLCPCB library with the user ({find}), and save it with {adopt('<C number>')}.")
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
                              "suggested", "unassigned", "unknown_parts", "problems", "misplaced", "documents",
                              "inbox_assigned", "inbox_known", "inbox_waiting", "inbox_new")}
    # 0. The inbox: its documents go into the store, linked to their parts.
    from_inbox = _inbox(project, store, apply, report)
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
                        shutil.copyfile(store.local_file(doc), tmp)
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
        _remove_from_inbox(project, from_inbox, report)
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
    lines += [f"changed: {rel} (the copy differs from the store). Ask the user: restore it (the file of the "
              "folder is replaced), or give the file a different name to import it as a new document"
              for rel in report["changed"]]
    lines += [f"missing: {rel} (the store does not have this document, and the folder does not have "
              "a file with its content). Get the file from the team (git, the team server), or add the "
              "datasheet again and link it to its part; tell the user" for rel in report["missing"]]
    lines += [f"part not in the store: {mpn}. The next sync makes it from the copies of the folder; if the "
              "line stays, link a document to the part" for mpn in report["unknown_parts"]]
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
    # The inbox (see _inbox).
    for item in report["inbox_assigned"]:
        lines.append(f"inbox: {item['file']}: the datasheet of {', '.join(item['parts'])}; its copy is in the "
                     "subfolder of the part, and the file is not in the inbox now")
    lines += [f"inbox: {rel}: the project has this document; "
              + ("the file is not in the inbox now" if apply else "the next sync takes the file from the inbox")
              for rel in report["inbox_known"]]
    for item in report["inbox_waiting"]:
        lines.append(f"inbox: {item['file']} ({item['sha256'][:12]}): no part of the project agrees"
                     + (f"; the PDF gives the part {item['proposal']}" if item["proposal"] else "")
                     + "".join(f"; {reason}" for reason in item["other"])
                     + f". Ask the user for the part (and the role, when it is not the datasheet), then assign it "
                     f"with {hint('<MPN>', item['sha256'])}. The next sync then takes the file from the inbox.")
    lines += [f"inbox: {rel}: the next sync imports it and looks for the parts that it agrees with"
              for rel in report["inbox_new"]]
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
