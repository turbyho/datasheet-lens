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
  and BOM). This consistency is critical. Get the parts from the CAD tool (its
  MCP server or its BOM export) and compare them: project_check (CLI
  `datasheet-lens check BOM.csv`). It gives the missing, extra and different
  parts with the next step. project_status (CLI `datasheet-lens ls`) gives the
  parts and documents of the file. If you cannot get a datasheet, tell the
  user, with the search links of part_links (CLI `datasheet-lens links MPN`;
  also for symbols, footprints and 3D models). Tell the user about each different or extra part, and when the MPN of
  the CAD project and the datasheet do not agree. Remove a part only when the
  user agrees: project_remove_part (CLI `datasheet-lens rm MPN`). The MCP server
  instructions give the full procedure.
- To add a datasheet: add_document with a file or a URL and the base part of the
  datasheet (for example TLV755P). Then project_add_part with the concrete
  orderable MPN of the BOM (for example TLV75533PDBVR; get_ordering_codes, CLI
  `datasheet-lens codes DOC`, gives the codes) and base. CLI:
  `datasheet-lens add PATH|URL -m MPN`.
- project_availability (CLI `datasheet-lens stock`) gives the lifecycle state
  and the stock of each part at the distributors; tell the user about each
  warning. search_distributors (CLI `datasheet-lens find MPN`) gives the
  concrete orderable MPNs of a part. They need a personal API key in the
  environment (DATASHEET_LENS_MOUSER_KEY).
- To change the MPN, the base part or the category of a part:
  project_update_part (CLI `datasheet-lens set MPN -m NEW`), not remove and add.
- project_sync (CLI `datasheet-lens sync`) keeps `datasheet-lens.toml` and
  `{folder}/` current. Do not change the copies by hand. Then project_check
  again: it must give no missing, extra or different part.
"""


def block(folder: str) -> str:
    """The block for a project with this document folder, with its markers."""
    return f"{BEGIN}\n{TEXT.format(folder=folder).rstrip()}\n{END}"


# A marker is a line of its own: a mention of a marker in a sentence or in
# inline code is not a marker.
BEGIN_RE = re.compile(r"(?m)^" + re.escape(BEGIN) + r"[ \t]*$")
END_RE = re.compile(r"(?m)^" + re.escape(END) + r"[ \t]*$")
# CLAUDE.md that imports AGENTS.md ("@AGENTS.md", "@./AGENTS.md"), outside code blocks.
INCLUDE_RE = re.compile(r"(?m)(^|\s)@(\./)?AGENTS\.md\b")
FENCE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1")


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
            action = _update_file(path, folder, root, results)
        except (OSError, UnicodeDecodeError) as e:
            action = f"not changed: {e}"
        done[real] = action
        results.append((name, action))
    return results


def _update_file(path: Path, folder: str, root: Path, results: list[tuple[str, str]]) -> str:
    text = path.read_bytes().decode("utf-8")  # keep the line ends of the file (CRLF or LF)
    # The line ends of most lines.
    newline = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"
    wanted = block(folder).replace("\n", newline)
    begin = BEGIN_RE.search(text)
    agents_ok = any(n == "AGENTS.md" and a in ("added", "updated", "current") for n, a in results)
    if path.name == "CLAUDE.md" and begin is None and agents_ok \
            and INCLUDE_RE.search(FENCE_RE.sub("", text)):
        return "not changed: it includes AGENTS.md, which has the block"
    if begin is not None:
        end = END_RE.search(text, begin.end())
        if end is None:
            return f"not changed: {BEGIN} has no {END} line"
        if text[begin.start():end.end()] == wanted:
            return "current"
        new, action = text[:begin.start()] + wanted + text[end.end():], "updated"
    else:
        base = text.rstrip("\r\n")
        new, action = (base + newline * 2 if base else "") + wanted + newline, "added"
    write_atomic(path, new.encode("utf-8"))
    return action
