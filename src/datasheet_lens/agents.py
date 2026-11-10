"""The datasheet-lens block in the instruction files of agents in a project.

An LLM agent reads AGENTS.md of a project. Claude Code reads CLAUDE.md and
.claude/CLAUDE.md of the project root, and the files that they import
(https://code.claude.com/docs/en/memory). project init puts a block between the
markers BEGIN and END into AGENTS.md, and into one of the two CLAUDE.md files. A
second run changes the block only when its text is different (for example from an
older version of datasheet-lens). The text outside the markers does not change. The
tool makes no new instruction file: the files belong to the user.
"""

from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from .fileutil import write_atomic

BEGIN = "<!-- datasheet-lens -->"
END = "<!-- /datasheet-lens -->"
AGENTS_FILE = "AGENTS.md"
# The project instructions of Claude Code, in the sequence in which a file gets a new
# block. Claude Code reads the two files together, thus only one of them gets the block.
CLAUDE_FILES = ("CLAUDE.md", ".claude/CLAUDE.md")
FILES = (AGENTS_FILE, *CLAUDE_FILES)

TEXT = """\
## datasheet-lens — component datasheets

The parts of this project and their documents are in `datasheet-lens.toml`. The
copies of the documents are in `{folder}/`. datasheet-lens gives the content of
these datasheets to an agent: the MCP server `datasheet-lens`, or the CLI
`datasheet-lens`.

- Read each value (limits, pins, register bits, timing) from the original page:
  get_page_image with an element ID, a caption ("Table 57", all pages of the
  table) or a region. Do not use a value from the text layer alone or from
  memory: the text layer can lose signs, units and table structure.
- Find the content step by step: list_documents, get_index, then search_text
  (it gives the table or the region of each hit) or get_page_map, then
  get_page_image.
- Before you read a value, look for it with find_facts. Save a value that the
  work uses with save_fact, with its page and element.
- `datasheet-lens.toml` must hold the same parts as the CAD project (schematic
  and BOM). This consistency is critical. Get the parts from the CAD tool (a BOM
  file, CSV, or its MCP server) and compare them: project_check with bom or
  parts (CLI `datasheet-lens check BOM.csv`). It gives the missing, extra and
  different parts with the next step, and the changes in the CAD project. The
  convention: Value holds the general value (`2N7002`, `100nF 10V`), the field
  MPN the MPNs, the fitted part first (`2N7002LT1G, BSS138LT1G`), and the
  hidden fields Value_Confirmed and MPN_Confirmed the copies of the last
  confirmation. The confirmation of the user is the truth. For a new MPN, add
  its datasheet at once; a change is valid only when the user confirms it. Then
  write the fields that project_check gives (KiCad: set_symbol_fields of the MCP
  server kicad-workbench). The CAD BOM is the master: after each change of a
  part that the user confirms (project_update_part, adopt_part, a replacement),
  write the new or changed part into the CAD project too (MPN, MPN_Confirmed,
  Value_Confirmed, LCSC, Manufacturer, Distributor). The manufacturer of a part
  comes from its datasheet: when a tool asks (ASK THE USER), ask the user.
  project_status (CLI `datasheet-lens ls`) gives the parts and documents of the
  file. If you cannot get a datasheet, tell the user, with the search links of
  part_links (CLI `datasheet-lens links MPN`; also for symbols, footprints and
  3D models). The user saves a downloaded datasheet into the inbox
  (`_inbox` in the folder of the copies): project_sync (CLI
  `datasheet-lens sync`) gives it to its parts. Tell the user about each
  change, different or extra part, and when the MPN of the CAD project and the
  datasheet do not agree. Remove a part only
  when the user agrees: project_remove_part (CLI `datasheet-lens rm MPN`). The
  MCP server instructions give the full procedure.
- To add a datasheet: add_document with a file or a URL and the base part of the
  datasheet (for example TLV755P). Then project_add_part with the concrete
  orderable MPN of the BOM (for example TLV75533PDBVR; get_ordering_codes, CLI
  `datasheet-lens codes DOC`, gives the codes) and base. CLI:
  `datasheet-lens add PATH|URL -m MPN -b BASE` (BASE: the part of the datasheet;
  the MPN itself when the datasheet is of this MPN).
- project_availability (CLI `datasheet-lens stock`) gives one verdict for each
  part from all sources (the distributors and the JLCPCB parts library): the
  lifecycle state and the stock. Tell the user about each warning.
  search_distributors (CLI `datasheet-lens find MPN`) gives the concrete
  orderable MPNs of a part. They ask the enabled distributors
  (distributor_status; the user sets them with `datasheet-lens dist`, globally
  or with `--local` for the project). Each needs a personal API key, which the
  user saves with `datasheet-lens key mouser` or `datasheet-lens key digikey`:
  give the user each note about a missing key.
- To find a new part or a replacement part by its parameters: part_parameters,
  then find_parts, with the source digikey or jlcpcb (CLI `datasheet-lens pick
  SOURCE CATEGORY FILTER ...`). For the JLCPCB assembly, use jlcpcb: it gives
  the JLCPCB stock and the library class (Basic, Preferred, Extended). When the
  user selects a candidate: adopt_part with the number of the source or the
  MPN (CLI `datasheet-lens adopt SOURCE NUMBER`) adds its datasheet, the part
  and its purchase source. A part can have more purchase sources; remove a
  wrong one with project_remove_source when the user agrees (CLI
  `datasheet-lens rm MPN -s NUMBER`). Read each value that the design needs in
  the datasheet of the candidate. part_links
  (CLI `datasheet-lens links MPN`) gives the links to the datasheet, the symbol,
  the footprint and the 3D model: give them to the user.
- To change the MPN, the base part or the category of a part:
  project_update_part (CLI `datasheet-lens set MPN -m NEW`), not remove and add.
- project_sync (CLI `datasheet-lens sync`) keeps `datasheet-lens.toml` and
  `{folder}/` current. Do not change the copies by hand. Then project_check
  again: it must give no missing, extra or different part.
"""


def block(folder: str) -> str:
    """The block for a project with this document folder, with its markers."""
    return f"{BEGIN}\n{TEXT.format(folder=folder).rstrip()}\n{END}"


# A line and its line end (CRLF, LF or CR). markdown-it counts the lines with the
# same line ends, thus the line numbers of its tokens are the numbers of these lines.
LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|\Z)")
# An import of Claude Code: "@path" after a space or at the start ("\ " is a space in
# the path). See _imports for the rules of the path.
IMPORT_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
IMPORT_START_RE = re.compile(r"[a-zA-Z0-9._-]")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BOM = "\ufeff"
_COMMONMARK = MarkdownIt("commonmark")

Span = tuple[int, int, int]  # a line: its start, the end of its text, the end of its line end


class _NoBlock(Exception):
    """The text has a marker, but no block that the tool can change: the reason."""


class _Lines:
    """The lines of a Markdown text, with the code blocks, the markers and the text
    of the paragraphs that markdown-it (CommonMark) finds.

    A marker is a line that starts an HTML block at the top level of the document,
    and that has only the marker and spaces or tabs. Thus a marker in a code block
    (also in a list item or a block quote), in a sentence or in inline code is an
    example, not a marker. The spaces and tabs after a marker are part of its line,
    not of the block.
    """

    def __init__(self, text: str):
        self.text = text
        self.spans: list[Span] = []
        pos = 0
        while pos < len(text):
            m = LINE_RE.match(text, pos)
            self.spans.append((pos, m.end(1), m.end()))
            pos = m.end()
        code: set[int] = set()  # the line numbers of the code blocks
        html_starts: set[int] = set()  # the first lines of the HTML blocks at the top level
        # The text in which Claude Code reads an import: the paragraphs and headings with
        # no code span and no HTML, and an HTML block that starts with a comment, with
        # no comment. An import in code or in an other HTML block is not an import.
        prose: list[str] = []
        for token in _COMMONMARK.parse(text):
            if token.type in ("fence", "code_block") and token.map:
                code.update(range(*token.map))
            elif token.type == "html_block":
                if token.level == 0 and token.map:
                    html_starts.add(token.map[0])
                if token.content.lstrip().startswith("<!--") and "-->" in token.content:
                    prose.append(HTML_COMMENT_RE.sub("", token.content))
            elif token.type == "inline":
                prose.append("".join(child.content if child.type == "text" else
                                     "\n" if child.type in ("softbreak", "hardbreak") else " "
                                     for child in token.children or []))
        self.prose = "\n".join(prose)
        self.markers: list[tuple[int, str]] = []  # (line number, BEGIN or END)
        self.other_markers = 0  # marker lines that are not markers, outside code
        for i, (start, stop, _) in enumerate(self.spans):
            line = text[start:stop]
            if i in code or line.strip(" \t") not in (BEGIN, END):
                continue
            if i in html_starts and line.rstrip(" \t") in (BEGIN, END):
                self.markers.append((i, line.rstrip(" \t")))
            else:
                self.other_markers += 1

    def block(self) -> tuple[Span, Span] | None:
        """The two marker lines of the block (BEGIN, END), or None with no BEGIN.

        Raises:
            _NoBlock: a marker line that the tool cannot change safely.
        """
        if self.other_markers:
            # For example a block in <details> or in a list item: a second block at the
            # top level would leave the old one.
            raise _NoBlock(f"a {BEGIN} or {END} line in an HTML block, in a list item or with spaces before "
                           "it: put the block at the top level of the file")
        begins = [i for i, (_, kind) in enumerate(self.markers) if kind == BEGIN]
        if not begins:
            return None
        if len(begins) > 1:
            raise _NoBlock(f"{len(begins)} {BEGIN} lines: keep one block")
        b = begins[0]
        # The next marker must be END: a BEGIN with no END is a block with no end.
        if b + 1 == len(self.markers) or self.markers[b + 1][1] != END:
            raise _NoBlock(f"{BEGIN} has no {END} line after it (outside a code block)")
        return self.spans[self.markers[b][0]], self.spans[self.markers[b + 1][0]]


def _line_end(text: str) -> str:
    """The line end of most lines: CRLF, LF or CR (LF when there is no line end, or
    when LF is one of the most frequent)."""
    crlf = text.count("\r\n")
    counts = {"\n": text.count("\n") - crlf, "\r\n": crlf, "\r": text.count("\r") - crlf}
    return max(counts, key=lambda end: counts[end])  # max keeps the first of equal counts


def _read(path: Path) -> tuple[str, str, _Lines]:
    """The byte order mark (or ""), the text with no byte order mark, and its lines.

    Raises:
        OSError, UnicodeDecodeError: the tool cannot read the file as UTF-8.
    """
    text = path.read_bytes().decode("utf-8")  # keep the line ends of the file (CRLF, LF or CR)
    bom = BOM if text.startswith(BOM) else ""  # an editor of Windows can write one
    body = text[len(bom):]
    return bom, body, _Lines(body)


def _imports(path: Path, prose: str) -> list[Path]:
    """The files that this file imports, with the rules of Claude Code: the text
    after "#" is not part of the path, "~/" is the home folder, and a relative path
    starts in the folder of the file (not in the project root)."""
    paths = []
    for m in IMPORT_RE.finditer(prose):
        name = m.group(1).split("#", 1)[0].replace("\\ ", " ")
        if name.startswith("~/"):
            paths.append(Path.home() / name[2:])
        elif (name.startswith("/") and name != "/") or IMPORT_START_RE.match(name):
            paths.append(path.parent / name)
    return paths


def update(root: Path, folder: str) -> list[tuple[str, str]]:
    """Put the current block into AGENTS.md and into one file of CLAUDE_FILES. Give
    (file name, what happened) for each file of FILES that exists: added, updated,
    current, or a reason why the file did not change.

    Claude Code reads CLAUDE.md and .claude/CLAUDE.md together, thus one of them gets
    the block, not the two:

    - A file that has a block (also through a symbolic link to AGENTS.md) keeps it:
      the tool updates it. Two files with a block: the result tells it.
    - When one of the two files imports AGENTS.md, and AGENTS.md has a block (also
      one that the tool cannot change), the two files get no block.
    - Else the first file of CLAUDE_FILES that exists gets the block.
    """
    actions: dict[str, str] = {}
    names: dict[Path, str] = {}  # real path -> the name of the file that the tool did
    with_block: set[Path] = set()  # real paths of the files that have the current block
    broken: set[Path] = set()  # real paths of the files with a block that the tool cannot change
    read: dict[Path, tuple[str, str, _Lines] | str] = {}  # real path -> _read, or the error

    def lines_of(real: Path) -> tuple[str, str, _Lines] | str:
        if real not in read:
            try:
                read[real] = _read(real)
            except (OSError, UnicodeDecodeError) as e:
                read[real] = f"not changed: {e}"
        return read[real]

    def has_markers(real: Path) -> bool:
        doc = lines_of(real)
        return not isinstance(doc, str) and bool(doc[2].markers or doc[2].other_markers)

    def put(name: str) -> None:
        path = root / name
        real = path.resolve()
        if real in names:
            first = names[real]
            actions[name] = f"{actions[first]} (the same file as {first})"
            return
        names[real] = name
        doc = lines_of(real)
        if isinstance(doc, str):
            action = doc
        else:
            try:
                action = _update_file(path, *doc, folder)
            except OSError as e:
                action = f"not changed: {e}"
        actions[name] = action
        if action in ("added", "updated", "current"):
            with_block.add(real)
        elif has_markers(real):
            broken.add(real)

    if (root / AGENTS_FILE).is_file():
        put(AGENTS_FILE)
    claude = [(name, (root / name).resolve()) for name in CLAUDE_FILES if (root / name).is_file()]
    for name, real in claude:
        if real in names or has_markers(real):
            put(name)
    # The files with a block (also one that the tool cannot change) that Claude Code
    # reads: real path -> how the result names it. AGENTS.md is one when a file of
    # CLAUDE_FILES imports it.
    holders: dict[Path, str] = {}
    for name, real in claude:
        if real in with_block or real in broken:
            holders.setdefault(real, name)
    agents = (root / AGENTS_FILE).resolve()
    if agents in with_block or agents in broken:
        importer = next((name for name, real in claude if agents in _imports_of(root / name, lines_of(real))), None)
        if importer is not None:
            holders.setdefault(agents, f"AGENTS.md through the import in {importer}")
    for name, real in claude:
        if name in actions:
            continue
        if real in names:  # a symbolic link to the other file
            put(name)
        elif holders:
            first, label = next(iter(holders.items()))
            which = "the block" if first in with_block else "a block that the tool cannot change"
            actions[name] = f"not changed: Claude Code also reads {label}, which has {which}"
        else:
            put(name)
            if real in with_block or real in broken:
                holders[real] = name
    # Two blocks in the context of Claude Code: tell it on each file of CLAUDE_FILES that
    # has a second block. AGENTS.md keeps its block: the other agents read it.
    order = sorted(holders, key=lambda r: r != agents)
    for real in order[1:]:
        actions[holders[real]] += (f" (Claude Code also reads the block in {holders[order[0]]}: keep one of "
                                   "the two blocks)")
    return [(name, actions[name]) for name in FILES if name in actions]


def _imports_of(path: Path, doc: tuple[str, str, _Lines] | str) -> set[Path]:
    """The real paths of the files that this file imports (none when the tool cannot
    read the file: doc is the error)."""
    return set() if isinstance(doc, str) else {p.resolve() for p in _imports(path, doc[2].prose)}


def _current(body: str, marker_lines: tuple[Span, Span], wanted: str) -> bool:
    """True when the block between the two marker lines is the wanted block (the
    spaces after the markers are not part of it)."""
    first, last = marker_lines
    return BEGIN + body[first[1]:last[0]] + END == wanted


def _update_file(path: Path, bom: str, body: str, lines: _Lines, folder: str) -> str:
    """Put the block into the file (the parts of _read). Give the action or the
    reason why the file did not change.

    Raises:
        OSError: the tool cannot write the file.
    """
    try:
        found = lines.block()
    except _NoBlock as e:
        return f"not changed: {e}"
    if found is not None:
        start, stop = found[0][0], found[1][1]  # the two marker lines, with their spaces
        outside = body[:start] + body[stop:]
        # The line end of the text of the user; the block alone: its own line end.
        newline = _line_end(outside if "\r" in outside or "\n" in outside else body)
        wanted = block(folder).replace("\n", newline)
        if _current(body, found, wanted):
            return "current"
        new, action = body[:start] + wanted + body[stop:], "updated"
    else:
        newline = _line_end(body)
        wanted = block(folder).replace("\n", newline)
        base = body.rstrip("\r\n")
        new, action = (base + newline * 2 if base else "") + wanted + newline, "added"
    # The new text must have the block as a block. The parser decides it: for example a
    # code block or an HTML block (<pre>, a comment) with no end at the end of the file
    # makes code or HTML of a block after it.
    try:
        check = _Lines(new).block()
    except _NoBlock:
        check = None
    if check is None or not _current(new, check, wanted):
        return "not changed: the end of the file is in a code block or an HTML block with no end"
    write_atomic(path, (bom + new).encode("utf-8"))
    return action
