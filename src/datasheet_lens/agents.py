"""The datasheet-lens block in the instruction files of agents in a project.

An LLM agent reads AGENTS.md or CLAUDE.md of a project. project init puts a block
between the markers BEGIN and END into each of these files that exists. A second
run changes the block only when its text is different (for example from an older
version of datasheet-lens). The text outside the markers does not change. The
tool makes no new instruction file: the files belong to the user.
"""

from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from .fileutil import write_atomic

BEGIN = "<!-- datasheet-lens -->"
END = "<!-- /datasheet-lens -->"
FILES = ("AGENTS.md", "CLAUDE.md")

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
# CLAUDE.md that imports AGENTS.md ("@AGENTS.md", "@./AGENTS.md").
INCLUDE_RE = re.compile(r"(^|\s)@(\./)?AGENTS\.md\b")
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
        # The text of the paragraphs and headings, with no code span and no HTML: Claude
        # Code reads an import only there (it removes the HTML comments, and an import
        # in code is not an import).
        prose: list[str] = []
        for token in _COMMONMARK.parse(text):
            if token.type in ("fence", "code_block") and token.map:
                code.update(range(*token.map))
            elif token.type == "html_block" and token.level == 0 and token.map:
                html_starts.add(token.map[0])
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


def update(root: Path, folder: str) -> list[tuple[str, str]]:
    """Put the current block into AGENTS.md and CLAUDE.md of the project root, if
    the file exists. Give (file name, what happened) for each file: added,
    updated, current, or a reason why the file did not change. A file that is a
    symbolic link to the other one is done one time."""
    results: list[tuple[str, str]] = []
    done: dict[Path, str] = {}  # real path -> the action
    for name in FILES:
        path = root / name
        if not path.is_file():
            continue
        real = path.resolve()
        if real in done:
            results.append((name, f"{done[real]} (the same file as {FILES[0]})"))
            continue
        try:
            action = _update_file(path, folder, results)
        except (OSError, UnicodeDecodeError) as e:
            action = f"not changed: {e}"
        done[real] = action
        results.append((name, action))
    return results


def _current(body: str, marker_lines: tuple[Span, Span], wanted: str) -> bool:
    """True when the block between the two marker lines is the wanted block (the
    spaces after the markers are not part of it)."""
    first, last = marker_lines
    return BEGIN + body[first[1]:last[0]] + END == wanted


def _update_file(path: Path, folder: str, results: list[tuple[str, str]]) -> str:
    text = path.read_bytes().decode("utf-8")  # keep the line ends of the file (CRLF, LF or CR)
    bom = BOM if text.startswith(BOM) else ""  # an editor of Windows can write one
    body = text[len(bom):]
    lines = _Lines(body)
    try:
        found = lines.block()
    except _NoBlock as e:
        return f"not changed: {e}"
    agents_ok = any(n == "AGENTS.md" and a in ("added", "updated", "current") for n, a in results)
    if path.name == "CLAUDE.md" and found is None and agents_ok and INCLUDE_RE.search(lines.prose):
        return "not changed: it includes AGENTS.md, which has the block"
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
