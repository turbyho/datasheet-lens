"""Project: the parts of a user project, with references to their documents in
the central store, and copies of the documents in the folder of the project.

The project file `datasheet-lens.toml` is in the root of the project. The central
store is the source: sync copies the documents into the folder of the project
(default `datasheets/`). A new PDF that a user puts into the folder goes into the
central store. sync adds it to a part only when the store links the document to
a part of the project. For a different file, sync proposes a part from the file
name, and the user (or the model) assigns it with add_part.

save() writes the project file again: comments that a user added are lost.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import pypdfium2 as pdfium

from .errors import LensError
from .fileutil import temp_path, write_atomic
from .store import ROLES, Document, Store, sha256_file

PROJECT_FILE = "datasheet-lens.toml"
DEFAULT_FOLDER = "datasheets"


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
    root: Path
    folder: str = DEFAULT_FOLDER
    # {"mpn", "manufacturer", "documents": [{"sha256", "file", "role"}]}
    parts: list[dict] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def folder_path(self) -> Path:
        return self.root.joinpath(*_rel_parts(self.folder, "the folder"))

    def file_path(self, rel: str) -> Path:
        """The path of a file in the folder (rel is relative to the folder)."""
        return self.folder_path.joinpath(*_rel_parts(rel, "the file"))

    def find(self, mpn: str, manufacturer: str = "") -> dict | None:
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


def init_project(root: Path | str, folder: str = DEFAULT_FOLDER) -> Project:
    root = Path(root).expanduser().resolve()
    if (root / PROJECT_FILE).exists():
        raise LensError(f"{root / PROJECT_FILE} exists already")
    project = Project(root, folder)
    project.folder_path.mkdir(parents=True, exist_ok=True)
    save(project)
    return project


def load(start: Path | str | None = None) -> Project:
    root = find_project(start)
    path = root / PROJECT_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise LensError(f"{path}: {e}") from e
    project = Project(root, _rel(data.get("folder", DEFAULT_FOLDER), "the folder"))
    parts = data.get("part", [])
    if not isinstance(parts, list):
        raise LensError(f"{path}: 'part' must be a list of tables ([[part]])")
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
            docs.append({"sha256": sha, "file": _rel(file, "the file"), "role": role})
        project.parts.append({"mpn": p["mpn"].strip(), "manufacturer": str(p.get("manufacturer", "")),
                              "documents": docs})
    return project


def _toml_str(s: str) -> str:
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def save(project: Project) -> None:
    lines = ["# datasheet-lens project: the parts and their documents. The central store",
             "# holds the documents; \"datasheet-lens project sync\" copies them into the folder.",
             "", f"folder = {_toml_str(project.folder)}"]
    for part in project.parts:
        lines += ["", "[[part]]", f"mpn = {_toml_str(part['mpn'])}",
                  f"manufacturer = {_toml_str(part['manufacturer'])}"]
        for d in part["documents"]:
            lines += ["", "[[part.document]]", f"sha256 = {_toml_str(d['sha256'])}",
                      f"file = {_toml_str(d['file'])}", f"role = {_toml_str(d['role'])}"]
    write_atomic(project.path, ("\n".join(lines) + "\n").encode("utf-8"))


# {path: (size, mtime_ns, SHA-256)}: a server process does not read a file again
# that did not change.
_hashes: dict[Path, tuple[int, int, str]] = {}


def _hash(path: Path) -> str:
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


def _copy_name(project: Project, doc: Document, files: dict[str, str]) -> str:
    """The file name of the copy of a document: a file in the folder with the same
    content, or the original file name if no different file has it."""
    for rel, sha in files.items():
        if sha == doc.sha256:
            return rel
    used = {d["file"]: d["sha256"] for part in project.parts for d in part["documents"]}
    # A name that is a valid file name on each system, and a valid project entry.
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", doc.filename).strip(" .") or f"{doc.sha256[:12]}.pdf"
    if files.get(name, doc.sha256) != doc.sha256 or used.get(name, doc.sha256) != doc.sha256:
        stem, dot, suffix = name.rpartition(".")
        name = f"{stem}-{doc.sha256[:8]}.{suffix}" if dot else f"{name}-{doc.sha256[:8]}"
    return name


def _refresh(project: Project, store: Store, part: dict, part_id: int, files: dict[str, str]) -> list[str]:
    """Add the documents that the store links to the part. Give their file names."""
    added = []
    refs = {d["sha256"]: d for d in part["documents"]}
    for row in store.documents_of_part(part_id):
        if row["sha256"] in refs:
            refs[row["sha256"]]["role"] = row["role"]
            continue
        name = _copy_name(project, store.document(row["id"]), files)
        part["documents"].append({"sha256": row["sha256"], "file": name, "role": row["role"]})
        added.append(name)
    return added


def _entry(project: Project, store: Store, part_id: int) -> dict:
    """The project part of a store part (made if necessary)."""
    row = store.part(part_id)
    part = project.find(row["mpn"], row["manufacturer"])
    if part is None:
        part = {"mpn": row["mpn"], "manufacturer": row["manufacturer"], "documents": []}
        project.parts.append(part)
    elif not part["manufacturer"]:
        part["manufacturer"] = row["manufacturer"]
    return part


def add_part(project: Project, store: Store, mpn: str, manufacturer: str = "", document: str = "",
             role: str = "datasheet") -> dict:
    """Add a part to the project, with the documents that the store links to it.
    A document (ID or SHA-256 prefix) is first linked to the part in the store."""
    if role not in ROLES:
        raise LensError(f"role must be one of {', '.join(ROLES)}")
    part_id = store.find_part(mpn, manufacturer, create=True)
    if document:
        doc = store.document(document)
        if doc is None:
            raise LensError(f"document not found: {document}")
        store.link(part_id, doc.id, role)
    part = _entry(project, store, part_id)
    _refresh(project, store, part, part_id, _folder_files(project))
    save(project)
    return part


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


def sync(project: Project, store: Store, apply: bool = True, restore: bool = False) -> dict:
    """Compare the project with the store and the folder. If apply is true: add the
    documents that the store links to the parts, copy the missing copies, import
    the new files of the folder into the store (with their index), and save the
    project file. If restore is true, a changed copy is copied again from the store.
    Give the report: lists of file names (and of document IDs in "documents")."""
    from .indexer import build_index

    report = {k: [] for k in ("ok", "copied", "restored", "changed", "missing", "imported", "added",
                              "suggested", "unassigned", "unknown_parts", "problems", "documents")}
    files = _folder_files(project)

    def import_file(rel: str) -> Document | None:
        """Import a file of the folder. A file that cannot be read is a problem of
        the report, not an error of the sync."""
        try:
            doc = store.add_document(project.file_path(rel))
            build_index(store, doc.id)
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
            report["added"] += _refresh(project, store, part, rows[0]["id"], files)

    # 2. The copies of the referenced documents.
    referenced = set()
    for part in project.parts:
        for d in part["documents"]:
            rel, doc = d["file"], store.document(d["sha256"])
            if rel in files and files[rel] == d["sha256"]:
                if doc is None and apply:  # a project from a different machine
                    doc = import_file(rel)
                    if doc is not None:
                        try:
                            store.link(store.find_part(part["mpn"], part["manufacturer"], create=True),
                                       doc.id, d["role"])
                        except LensError as e:
                            report["problems"].append(f"{rel}: not linked to a part: {e}")
                report["ok"].append(rel)
            elif doc is None:
                # The store does not have the document. A different file with this
                # name is a new document (step 3).
                report["missing"].append(rel)
                if rel in files:
                    continue
            elif rel in files and not restore:
                report["changed"].append(rel)
            else:
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

    # 3. The new files of the folder.
    known = {d["sha256"] for part in project.parts for d in part["documents"]}
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
            if part is not None:
                if apply:
                    part["documents"].append({"sha256": sha, "file": rel, "role": row["role"]})
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
    if apply:
        save(project)
    report["documents"] = sorted(set(report["documents"]))
    return report


def format_parts(project: Project) -> list[str]:
    lines = [f"project {project.root} (folder {project.folder}/): {len(project.parts)} parts"]
    for part in project.parts:
        lines.append(f"{part['mpn']} ({part['manufacturer'] or 'manufacturer unknown'})")
        for d in part["documents"]:
            lines.append(f"  {d['sha256'][:12]}  {d['role']:9} {project.folder}/{d['file']}")
    return lines


def format_report(report: dict, apply: bool, add_hint: str) -> list[str]:
    """The report of sync as lines. add_hint tells how to assign a document to a
    part, with {mpn} and {sha} in it."""
    lines = []
    for key, text in (("added", "new reference"), ("copied", "copied" if apply else "copy missing"),
                      ("restored", "copied again" if apply else "copy to restore"),
                      ("imported", "imported into the store")):
        lines += [f"{text}: {rel}" for rel in report[key]]
    lines += [f"changed: {rel} (the copy differs from the store; restore it, or give the file "
              "a different name to import it as a new document)" for rel in report["changed"]]
    lines += [f"missing: {rel} (the store does not have this document, and the folder does not have "
              "a file with its content)" for rel in report["missing"]]
    lines += [f"part not in the store: {mpn}" for mpn in report["unknown_parts"]]
    lines += [f"problem: {text}" for text in report["problems"]]
    for item in report["suggested"]:
        mpn = item["parts"][0].split(" (")[0]
        lines.append(f"no part: {item['file']} ({item['sha256'][:12]}); proposal: {', '.join(item['parts'])}; "
                     "assign it with " + add_hint.format(mpn=mpn, sha=item["sha256"][:12]))
    for item in report["unassigned"]:
        lines.append(f"no part: {item['file']} ({item['sha256'][:12]}); assign it with "
                     + add_hint.format(mpn="<MPN>", sha=item["sha256"][:12]))
    lines.append(f"{len(report['ok'])} copies are current")
    return lines
