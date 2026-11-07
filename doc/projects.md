# Projects

A design project (a schematic and a PCB in a repository) can have a project file
`datasheet-lens.toml`. The project file holds the parts of the design and references
to their documents. A folder in the repository holds copies of these documents. The
team gets the two with the repository.

This document uses these terms:

| Term | Meaning |
|---|---|
| project root | The folder that holds `datasheet-lens.toml`. The CLI help calls it "the project folder". |
| document folder | The folder of the copies, relative to the project root (key `folder`, default `datasheets`). |
| copy | A PDF file in the document folder. |
| central store | The store of the user (see [installation.md](installation.md)). It is the source of the copies. |

The CLI commands and the MCP tools find the project root from the current folder (or
the working folder of the MCP server) or a parent folder. The option `-p PATH` and
the tool parameter `path` give a different folder.

## Make a project

In the root folder of the design repository:

```sh
datasheet-lens project init
```

The command does these steps:

1. It makes the project file `datasheet-lens.toml` with a comment header.
2. It makes the document folder, with the inbox `_inbox` in it (see
   [The inbox](#the-inbox)).
3. It puts the datasheet-lens block into `AGENTS.md` and `CLAUDE.md`.

| Option | Effect |
|---|---|
| `--folder FOLDER` | The document folder, relative to the project root (default `datasheets`). |
| `--layout LAYOUT` | The subfolder of each copy (default `{manufacturer}`). See [Layout](#layout). |
| `-p PATH` | The project root (default: the current folder). |

When you run the command in a subfolder of a project, it uses that project. When the
project file exists, the command does not change it. It only updates the block in
`AGENTS.md` and `CLAUDE.md`. To change the folder or the layout of an existing
project, edit the project file.

### The block in AGENTS.md and CLAUDE.md

An agent reads `AGENTS.md` or `CLAUDE.md` of a repository. The block tells the agent
where the project file and the copies are, and which tools to use:

- Read each value from the original page.
- Find the content step by step, and use `find_facts` and `save_fact`.
- Keep the project file equal to the CAD project (`project_check`).
- Add a datasheet with `add_document` and `project_add_part`.
- Find new parts with `find_parts`, and take a part with `adopt_part`.
- Check the distributors with `project_availability`.
- Save a downloaded datasheet into the inbox `_inbox`: `project_sync` gives it to
  its parts.
- Change a part with `project_update_part`.
- Keep the copies current with `project_sync`.

The block is between the lines `<!-- datasheet-lens -->` and
`<!-- /datasheet-lens -->`. The text outside these lines does not change. Rules:

- The tool does not make a new `AGENTS.md` or `CLAUDE.md`. It changes only the files
  that exist. Make the file first if you want the block.
- A second run replaces the block only when its text is different (for example
  from an older version).
- When `CLAUDE.md` imports `AGENTS.md` (a line `@AGENTS.md`) and `AGENTS.md` has the
  block, `CLAUDE.md` does not get a second block.
- The tool keeps the line ends of the file (LF or CRLF).

## The project file

The project file is TOML. This example shows all keys:

```toml
# datasheet-lens project file: the parts of this project and their documents.
# ... (the header that "project init" writes)

# The folder of the copies, relative to the project root.
folder = "datasheets"
# The subfolder of each copy in the folder.
layout = "{manufacturer}"
# Optional: the distributors for this project (see distributors.md).
distributors = ["mouser", "digikey"]

# A concrete orderable part, and the base part of its datasheet.
[[part]]
mpn = "TLV75533PDBVR"
manufacturer = "Texas Instruments"
base = "TLV755P"
category = "LDO"

[[part.document]]
sha256 = "<64 hexadecimal digits>"
file = "Texas Instruments/tlv755p.pdf"
role = "datasheet"

# A part whose datasheet covers more variants.
[[part]]
mpn = "ADC34J24"
manufacturer = "Texas Instruments"
category = "ADC"
variants = ["ADC34J22", "ADC34J23", "ADC34J24"]
exclude = ["<64 hexadecimal digits>"]

[[part.document]]
sha256 = "<64 hexadecimal digits>"
file = "Texas Instruments/adc34j24.pdf"

[[part.document]]
sha256 = "<64 hexadecimal digits>"
file = "my-notes/adc34j24-errata.pdf"
role = "errata"
pinned = true
```

### Top-level keys

| Key | Value | Default |
|---|---|---|
| `folder` | The document folder: a relative path with `/` and no `..`. | `datasheets` |
| `layout` | The subfolder of each copy. See [Layout](#layout). | `{manufacturer}` in a new project file. `""` when the key is missing. |
| `distributors` | A list of distributor names. When the key is missing, the project uses the global list. | missing |

TOML puts each key after a table header into that table. Thus the top-level keys must
come before the first `[[part]]`.

### Keys of `[[part]]`

| Key | Value |
|---|---|
| `mpn` | Necessary. The MPN of the part in the BOM: the concrete orderable MPN. |
| `manufacturer` | The manufacturer, as the project writes it (the layout uses its built-in name, see [Layout](#layout)). It can be any name of the manufacturer that the store knows (see `datasheet-lens maker` in [cli.md](cli.md)). Necessary when two parts have the same MPN. |
| `base` | The base part of the datasheet, when `mpn` is a concrete part of it. |
| `category` | The function of the part, for example `ADC`, `LDO`, `MCU`. The layout can use it. |
| `variants` | For information only: the parts that the same datasheet covers. The tool writes it from the store. |
| `exclude` | A list of SHA-256 values: documents that the project does not want for this part. `rm -d` writes it. |

### Keys of `[[part.document]]`

| Key | Value |
|---|---|
| `sha256` | Necessary. The SHA-256 of the document (64 hexadecimal digits). It identifies the document in the central store. |
| `file` | Necessary. The path of the copy, relative to the document folder. |
| `role` | `datasheet` (default), `manual`, `errata`, `appnote` or `other`. |
| `pinned` | `true` for a file that a person put into the folder. Sync does not move it, and a removal does not delete it. |

### Keys of `[[part.source]]`

A purchase source: a source that this project orders the part from. `datasheet-lens
adopt` writes it (see [distributors.md](distributors.md#take-a-found-part)).

| Key | Value |
|---|---|
| `source` | Necessary. `digikey`, `jlcpcb` or `mouser`. |
| `number` | Necessary. The part number of the source: a DigiKey number, a JLCPCB C number or a Mouser number. |
| `library` | JLCPCB: the library class, `Basic`, `Preferred` or `Extended`. |

```toml
[[part.source]]
source = "jlcpcb"
number = "C47616"
library = "Extended"
```

A part can have more purchase sources of one source, for example two packagings at
DigiKey. The availability check asks for each number; the JLCPCB number check of
`project_check` agrees with each of them. Remove a number with
`datasheet-lens rm MPN -s NUMBER` (`project_remove_source`).

A `[[part.document]]` block belongs to the `[[part]]` above it. If a person moves a
block, the document goes to a different part. Sync finds this case when the store
links the document to a different part, and reports it as a problem.

### The text of the user stays

datasheet-lens writes the project file with tomlkit. A write keeps the comments, the
format, the order and the keys that datasheet-lens does not know. It changes only the
values that changed, and adds new parts and documents at the end. When the tool removes
a part, a document or a purchase source, it removes its table, the comment lines
directly above its header (with no blank line between) and one blank-line gap. A comment
with a blank line after it stays. Before a write, the tool reads the new text back and
compares it with the data. If the file changed after the tool read it, the tool writes
nothing and tells you to run the command again.

Write `part` and `part.document` as arrays of tables (`[[part]]`), not as inline
arrays. The tool cannot add to an inline array.

## Layout

The key `layout` is a pattern for the subfolder of each copy in the document folder.
It can use these placeholders:

| Placeholder | Value |
|---|---|
| `{manufacturer}` | The built-in name of the manufacturer of the part. |
| `{category}` | The category (function) of the part. |
| `{mpn}` | The MPN of the part. |

Examples:

| Layout | Copy of a TLV755P datasheet |
|---|---|
| `{manufacturer}` | `datasheets/Texas Instruments/tlv755p.pdf` |
| `{category}/{manufacturer}` | `datasheets/LDO/Texas Instruments/tlv755p.pdf` |
| `""` | `datasheets/tlv755p.pdf` |

Rules:

- A missing value gives the subfolder `_unknown`.
- `{manufacturer}` gives one folder for each manufacturer. The distributors and the
  datasheets write the name of one manufacturer in different forms (`TI`,
  `Texas Instruments Inc.`). The tool uses the built-in name of the manufacturer
  registry (`Texas Instruments`). A name that is not a built-in name stays as the
  part gives it. The relations that a user records (`datasheet-lens maker`) do not
  change the folder: they differ between the machines, and the copies of a shared
  project would move with each sync.
- The tool replaces the characters that a file name cannot hold with `_`.
- The name of a copy is the file name of the document in the store. When a different
  file has that name, the tool adds the first 8 characters of the SHA-256.
- When the layout or a part changes, `sync` moves the copies to their new subfolders.
  It does not move a pinned file.

## Add parts and documents

### With the CLI

`datasheet-lens add` adds a document to the store. In a project folder, it also adds
the part to the project:

```sh
datasheet-lens add tlv755p.pdf -m TLV75533PDBVR
datasheet-lens add https://example.com/tlv755p.pdf -m TLV75533PDBVR -c LDO
```

The command does these steps:

1. It copies the file, or downloads the URL, into the store.
2. It finds the base part: the option `-b`, a part that the store links to the
   document, or a proposal from the PDF (title, page 1, file name). It asks you to
   confirm the proposal. `-y` accepts it with no question.
3. It links the document to the base part, and to each variant that the datasheet
   covers.
4. It builds the index.
5. In a project folder, it adds the concrete MPN (`-m`) as a project part with the
   base part. With no `-m` and no `-y`, it asks for the concrete MPN in a terminal.
   Enter uses the base part.
6. It copies the document into the document folder and builds the element map.

When the project has concrete parts of this base part and you give no `-m`, these
parts get the document. The base part does not become a second part.
`--no-project` adds the document to the store only.

`datasheet-lens project add-part MPN` adds a part with the documents that the store
links to it. `--document DOC` links a document to the part first, and `--base MPN`
gives the base part.

When a project part is probably a base part and not a concrete MPN, `add` and
`add-part` give a note with the orderable codes of the datasheet.

### With the MCP tools

1. `add_document(path, part, variants)` adds the document to the store and links it
   to the base part.
2. `project_add_part(mpn, base, document)` adds the concrete MPN to the project.
3. `project_sync` copies the document into the document folder.

## Synchronize

```sh
datasheet-lens sync          # the same as: datasheet-lens project sync
datasheet-lens ls            # the same as: datasheet-lens project status
```

`datasheet-lens ls` shows the parts and documents, and what a sync would change. It
changes nothing. `datasheet-lens sync` does these steps:

1. It imports each PDF file of the inbox into the store, and links it to the project
   parts that it agrees with (see [The inbox](#the-inbox)).
2. It adds the documents that the store links to each project part (and to its base
   part). It does not add a document that the part excludes.
3. It copies each missing copy from the store into the document folder.
4. It moves each copy to the subfolder of the layout. It does not move a pinned file.
5. It imports each new PDF file of the document folder into the store.
6. It builds the index of each project document that has none.
7. It writes the project file, and removes the inbox files that the project has now.
8. The CLI builds the element map of each document. The MCP tool builds them in the
   background.

The report gives a line for each change and each problem:

| Line | Meaning | Action |
|---|---|---|
| `copied`, `moved to its subfolder`, `new reference`, `imported into the store` | A change that sync did. | None. |
| `changed` | The copy is different from the document in the store. | `sync --restore` copies the document again. To keep the file as a new document, give it a different name. |
| `missing` | The store does not have the document, and the folder has no file with its content. | Get the file from the team, or remove the reference. |
| `part not in the store` | The store has no part with this MPN. | On a new machine, the first sync makes the part from the copies: run sync again. If the line stays, link a document to the part (`project add-part MPN --document DOC`). |
| `no part` | A new file of the folder that the store links to no project part. | Assign it with the command or call in the line. |
| `problem` | For example two store parts with the same MPN, or a moved `[[part.document]]` block. | Do the step in the line. |
| `inbox` | A file of the inbox: given to its parts, in the project already, or with no part. | For a file with no part: select the part, and assign the file with the command or call in the line. |

### New files in the folder

You can put a PDF file into the document folder yourself. Sync imports it into the
store. When the store links the document to a project part, sync adds it to that
part with `pinned = true`. Else sync proposes a part from the file name. Assign the
file with `datasheet-lens project add-part MPN --document DOC` (CLI) or
`project_add_part` (MCP).

### The inbox

The inbox is the folder `_inbox` in the document folder (`datasheets/_inbox/`). Put
the PDF files there that you download yourself: a datasheet that a tool could not
download (a login or a captcha), or many datasheets at one time. `adopt` gives the
path of the inbox when no URL gives the datasheet.

For each file of the inbox, sync does these steps:

1. It imports the file into the store.
2. It finds the project parts that the document agrees with. A part agrees when its
   MPN is an ordering code of the document, or when its MPN or its base part is the
   part of the PDF or one of its variants.
3. It compares the manufacturers. The manufacturer of the datasheet is the valid one:
   a part with a manufacturer gets the document only from the same manufacturer. A
   copy of a part can have the same MPN and its own datasheet.
4. It looks at the documents of the part. Only a part with no datasheet gets the
   document as its datasheet. The tool does not find the role of a document (errata,
   manual, application note, a new revision): for a part with a datasheet, you
   decide.
5. It links the document to each part that it goes to, as `add-part` does. The copy
   goes into the subfolder of the layout, and sync removes the file from the inbox.

A file with no part stays in the inbox. The store has its document, and the report
gives the part that the PDF names, and why a part that agrees did not get it. Select
the part (and the role, when it is not the datasheet), and assign the file with
`datasheet-lens project add-part MPN --document DOC` (CLI) or `project_add_part`
(MCP). The next sync removes the file from the inbox. A file of a document that the
project has already goes from the inbox too. When the project does not get a
document (for example, two parts of the store have its MPN), the file stays, and the
report gives a problem.

### PDF files outside the folder

Sync imports only the PDF files of the document folder. It also looks at the other
folders of the project root and reports the folders that hold PDF files. It does not
look into hidden folders (for example `.git`) and the folders `node_modules`,
`__pycache__`, `venv`, `build`, `dist` and `target`. It stops after 1000 files.

If the repository keeps its datasheets in a different folder, set the key `folder`
of the project file to that folder.

## Change and remove

| Task | CLI | MCP tool |
|---|---|---|
| Change the MPN of a part | `datasheet-lens set TLV755P -m TLV75533PDBVR` | `project_update_part(mpn, new_mpn)` |
| Set the base part | `datasheet-lens set TLV75533PDBVR -b TLV755P` | `project_update_part(mpn, base)` |
| Set or remove the category | `datasheet-lens set MPN -c LDO` (`-c ""` removes it) | `project_update_part(mpn, category)` |
| Change the manufacturer | `datasheet-lens set MPN -M "Texas Instruments"` | `project_update_part(mpn, new_manufacturer)` |
| Remove a part | `datasheet-lens rm MPN` | `project_remove_part(mpn)` |
| Remove a document | `datasheet-lens rm -d DOC` | `project_remove_document(document)` |
| Remove a purchase source | `datasheet-lens rm MPN -s NUMBER` | `project_remove_source(mpn, number)` |

`set` keeps the part at its place in the project file, and keeps its copies. Use it,
not a removal and a new addition. After a change, run `datasheet-lens sync` to move
the copies to the subfolders of the layout. Give a name of the manufacturer that the
store knows (`datasheet-lens maker`). With a name that the store does not relate to
the manufacturer of the part, the command stops with an error and tells how to record
the name. Thus the store does not get a second part with the same MPN.

A new MPN removes the purchase sources (`[[part.source]]`) of the part: they are the
numbers of the old MPN. The result tells the removed sources. Take the new part with
`adopt` (`adopt_part`) to save its sources.

`rm MPN` removes the part from the project file, and deletes its copies that no other
part uses. `rm -d DOC` removes a document from each part that has it, or only from
the part `MPN` when you give it. The tool writes the SHA-256 of the document into the
`exclude` list of each of these parts, so that sync does not add it again.
`project add-part MPN --document DOC` adds it again. `DOC` is the document ID, a
SHA-256 prefix of 8 characters or more, or the file name of the copy.

A removal does not delete:

- A pinned file. `--pinned` (`delete_pinned` in MCP) deletes it too.
- A file whose content is different from the document.
- A copy that a different part uses.

`-n` (`dry_run` in MCP) shows what would change and changes nothing. A removal does
not change the central store, because other projects can use the part.

## Consistency with the CAD project

The project file must hold the same parts as the CAD project. The team uses it as the
list of the datasheets of the design.

The BOM of the CAD project is the master. A change of a part goes into the two
places: after each change that the user confirms (a new part, a new MPN, a
replacement, a JLCPCB number), the agent writes the new or changed part into the
CAD project (`Value` with the MPNs in parentheses, `MPN`, `Value_Confirmed`, the
LCSC field; KiCad: `set_symbol_fields` of the MCP server kicad-workbench) and into
the project file (`project_update_part`, `adopt_part`). When the tool cannot
process a field of the BOM (a JLCPCB number of a different MPN, a text that is not
a number), the agent asks the user.

### With the MCP tools

1. The agent gets the parts of the CAD project from the CAD tool: for each symbol, the
   fields MPN, Value, Value_Confirmed and Datasheet and the reference, as they are.
   The best source is a BOM file (CSV) with one line for each symbol, for example
   from the tool `export_bom` of the MCP server kicad-workbench (with `output_path`).
   The agent then gives only the path, not the values. A list of parts from the MCP
   server of the CAD tool is the other source.
2. It calls `project_check(bom=<CSV file>)` or `project_check(parts)` with all parts.
   A relative path of a BOM file is relative to the project folder (the folder of
   `datasheet-lens.toml`), not to the working folder of the MCP server.
   The tool finds the parts of each symbol (see "MPN and Value fields" below). Each
   part with an MPN needs its datasheet, also an alternative in its Value.
3. The result gives one line for each conflict and each change in the CAD project
   (see "MPN and Value fields"), one line for each symbol that is not confirmed or
   that needs the completion of its fields, one line with the symbols that have no
   selected part, one line with the symbols that are not fitted (DNP: their parts
   stay parts of the project), the number of the same parts, and one line for each
   part that is:
   - `different`: probably one part with two MPNs (for example the base part in the
     project and the concrete MPN in the CAD project). The line gives the
     `project_update_part` call.
   - `missing`: in the CAD project, not in the project file. The line gives the
     documents of the store, the probable base part, or the Datasheet field, with
     the next call. When it finds nothing, it gives `part_links`. An alternative of
     a part is a missing part too, with the note `[alternative of MPN]`.
   - `extra`: in the project file, not in the CAD project. The agent tells the user
     and removes it only when the user agrees.

   When the two hold the same parts and no symbol needs a step of the user, the
   last line `next steps` gives `project_sync` and then `project_availability`.
4. After the changes, the agent runs `project_sync` and `project_check` again.

### MPN and Value fields

Each symbol has three fields for its parts (the model is agreed with the user,
2026-11-05):

| Field | Content |
|---|---|
| `Value` | A general value, and the MPNs in one text in parentheses: the fitted part first, then the alternatives. `2N7002 (2N7002LT1G, BSS138LT1G)`, `100nF 10V (GRM155R71A104KA01D)`. The designer changes it in KiCad. |
| `MPN` | The confirmed copy of the MPNs in the parentheses (`2N7002LT1G, BSS138LT1G`). The BOM, the distributors and the assembly services read it. |
| `Value_Confirmed` | A hidden field: a copy of the `Value` of the last confirmation. |

The convention of the parentheses:

- Only the text in parentheses gives MPNs. All other text of `Value` is the general
  value. The tool does not guess the kind of a word, and it does not use the data of
  datasheet-lens for the check. Thus the check is the same with and without
  datasheet-lens.
- The separators of the MPNs are `,` and `;`, in the parentheses and in `MPN`. A
  slash is not a separator: it is part of many MPNs (`LM358DR/NOPB`). A space needs
  no quotes (`(ABC 123)`). `~` is an empty field (KiCad).
- An MPN with `,`, `;` or a parenthesis is in quotes, as in CSV (`""` is a quote):
  `2N7002 ("BC847B,215", BSS138LT1G)`. A quote has this function only at the start
  of an MPN: `ICL7660"CPA` is one MPN. In `MPN`, a parenthesis needs no quotes. The
  tool writes quotes only where they are necessary, and the quotes that are not
  necessary do not count in a comparison. The quotes do not change the meaning of a
  word: `"-"` and `"TBD"` are the same as `-` and `TBD`. The spaces around an MPN do
  not count, also in quotes.
- A comma with no space before three digits (`BC847B,215`, the packing code of NXP
  and Nexperia) in a list with no quotes: when each separator of the list is such a
  comma, the list is one MPN. Thus `MPN = BC847B,215` stays clean for the
  distributors. Else it is a conflict: write the MPN in quotes. The tool does not
  guess the items of such a list: the full text is one item.
- One text in parentheses holds all MPNs. More texts in parentheses, a parenthesis
  that is not closed, or a parenthesis in an MPN with no quotes, is a conflict: the
  user corrects `Value`.
- Each text in parentheses is a list of MPNs: `10k (1%)` gives the MPN `1%`. Write
  such a text in an other form (`10k 1%`).
- An alternative is an other orderable code of the part, a part of an other
  manufacturer, or the part of a variant of the product. Each one needs its own
  datasheet. The Datasheet field of the symbol belongs to the fitted part.
- Each item of `MPN` is an MPN, also with no digit (`PJ-SMD`). The text
  `-- mixed values --` of KiCad (a BOM line of symbols with different values) gives
  a conflict. A KiCad in an other language writes a translation of this text: the
  tool takes it as an MPN.
- `N/A`, `NA`, `-`, `--` and `none` tell that the symbol needs no part (a test
  point, a generic resistor): no datasheet, no message. `TBD` (to be determined)
  tells that no part is selected yet, as with an empty `MPN`, also in the
  parentheses (`10k (TBD)`).
- One BOM holds the primary MPN and its alternatives together: the assembly selects
  the alternative, not the BOM. For the variants of a product in KiCad 10, see
  below.

The check of each symbol. The confirmation of the user is the truth:

- **Change**: `Value` is not equal to `Value_Confirmed`, or the MPNs in the
  parentheses are not the items of `MPN` (an other MPN, an other order, an other
  spelling). The comparison uses the words of the general value (the spaces between
  words do not count, the words and their case count: `10 k` is not `10k`, `4m7` is
  milliohm, `4M7` is megaohm) and the list of the MPNs. The tool tells the user,
  gives the new MPNs and the confirmed MPNs that are not in `Value` now, and the
  values of `MPN` and `Value_Confirmed` that a confirmation writes. Until the user
  confirms the change, the check uses the confirmed `MPN`. When the user does not
  confirm it, the `Value` must change back. For a new general value with no MPN in
  `Value`, the user selects a new part and writes it into `Value` first.
- **Confirmed**: the MPNs in the parentheses are the items of `MPN`, and `Value` is
  equal to `Value_Confirmed`. The parts are the MPNs.
- **Not confirmed**: `Value` has MPNs in the parentheses, and `Value_Confirmed` is
  empty or the CAD data has no such field (the first confirmation). When `MPN` is
  empty, the parts are the MPNs in the parentheses. The tool gives the values to
  write when the user agrees. An `MPN` that is empty after a confirmation is a
  change.
- **Complete the fields**: `Value` has no parentheses, `MPN` has MPNs, and
  `Value_Confirmed` has no parentheses. The tool gives the fields that complete the
  symbol by the convention: `Value = 10k (RC0603FR-0710KL)` and the same
  `Value_Confirmed`. The parts do not change. Not for an empty `Value` (a BOM with
  no Value column). When `Value_Confirmed` has parentheses and `Value` has none, the
  user removed the MPNs: a change.
- A general value with no MPN: no part is selected. The tool gives the references
  in one line (not the power symbols, `#PWR01`).
- **No column `Value_Confirmed`** (a BOM of an other CAD tool): the tool compares
  only the parentheses and `MPN`. The result gives one line: a change of a general
  value is not found. Each CAD tool can add a column to its BOM: add the field
  `Value_Confirmed` to the symbols and to the BOM, or use an unused custom column and
  give its name with `--confirmed-column` (MCP: `bom_confirmed_column`).
- More MPN columns (`MP` and `MANUFACTURER_PART_NUMBER` of Eagle, `MPN` and `mpn`):
  the first one with an MPN, in the order of the usual MPN names (`MPN` first), is
  the field `MPN` of the line. A column with a "no part"
  text (`-`) against a column with an MPN is a conflict. An other one with different
  MPNs is a conflict, and its MPNs are parts too: none is lost, and none replaces a
  confirmed MPN. The MPNs of more `Value` columns are parts too. The user corrects
  the columns: a confirmation does not.
- The MPNs of a conflict of more columns are parts, but not alternatives.
- A symbol with a conflict gets no change, completion or "not confirmed" line, and
  no "no part selected": a write of a proposal can hide the conflict. The user
  corrects the fields first. Its parts are the MPNs of `MPN`, else the MPNs of
  `Value`.
- Two spellings of one part in the CAD project (other signs or case:
  `LTC3588EMSE#PBF`, `LTC3588EMSEPBF`) are one part, with the first spelling. A
  spelling that is not the spelling of the project file gives a `different` line.

The variants of a product (KiCad 10) are an other way to write alternatives: KiCad
keeps the fields of each variant. A CAD tool with no variants uses the one BOM with
the alternatives in `Value` and `MPN`. Give one BOM file for each
variant, the default variant first (`project_check(bom=[...])`, `datasheet-lens check
a.csv b.csv`). The parts of all files count. Each file gets the check of the confirmed
fields: a variant can set an other `Value`, `MPN` or `Value_Confirmed`. With more
variants, each line needs its reference: the tool finds the line of the default
variant by it. A message of an other variant names its file (`Q5 (variant b.csv)`),
and the tool gives it only when the default variant does not give the same message. A
confirmation of it writes into the fields of the symbol in that variant (KiCad 10
keeps the fields of each variant), not into the fields of the default variant. A
reference with an other MPN in an other variant is not a conflict. The Datasheet
field of a variant line that is equal to the field of the default variant is
inherited: it is not the datasheet of the other part of the variant.

The result names the fields to write by the columns of the BOM, or by `mpn_fields`
and `confirmed_field` of the parts:

- Each MPN column with the MPN of the line (Eagle: `MP` and
  `MANUFACTURER_PART_NUMBER`), and the column of `Value_Confirmed` (also the column
  of `--confirmed-column`).
- With no MPN in the line: the one MPN column of the BOM, else `MPN`. With more
  names, or more filled names in a line of more symbols, the result names the
  choices: the user selects the field of the symbol.
- The BOM must name its columns as the fields of the symbols. A BOM with labels
  (kicad-cli `--labels`) gives the labels. `export_bom` of kicad-workbench gives the
  field names.

datasheet-lens does not write the fields into the schematic, and it does not call
an other tool. The agent writes the fields that the result gives, after the
confirmation of the user: in KiCad, or with the tool `set_symbol_fields` of the MCP
server kicad-workbench. Without datasheet-lens, the tool `check_symbol_fields` of
kicad-workbench does the same check of the fields (no datasheets).

### A BOM file

Export a BOM as CSV from the CAD tool, then compare it:

```sh
kicad-cli sch export bom --fields 'Reference,Value,MPN,Value_Confirmed,Datasheet,${DNP}' \
    --output bom.csv design.kicad_sch
datasheet-lens check bom.csv
```

Replace `MPN` with the name of the MPN field of your symbols. Use the default format
of kicad-cli (the CSV preset): the Semicolons and TSV presets need the option
`--quote`. Write `${DNP}` in single
quotes: kicad-cli of KiCad 8 and 9 reads `DNP` with no `${}` as a field of the
symbols, and that column is empty. Export one line for each symbol (no
`--group-by`): in a line of a group, KiCad writes DNP when only some symbols of the
line have DNP.

The MCP tool `project_check` reads the same file with `bom` (and `bom_column` in
place of `--column`). A relative path of `bom` is relative to the project folder
(`path`). The CLI command `check` and the MCP tool have the same generic
CSV reader. It finds the columns by their names.

**Tested only with BOM exports of KiCad and Eagle.** A BOM of a different CAD tool
can work when its column names are in the lists below, but nobody has tested it.

| CAD tool | State |
|---|---|
| KiCad (`kicad-cli sch export bom`) | Tested with real BOM exports. |
| Eagle | Tested with a real BOM export (semicolon, the attributes `MP` and `MANUFACTURER_PART_NUMBER` of different libraries, `Parts`, `Value`), before the reader checked RFC 4180. A value with a quote that is not between quotes (`0.1" header`) now needs `--quote none`. |
| Fusion, Altium and other tools | Not tested. The column names of Altium (`Designator`, `Manufacturer Part Number 1`) are in the lists. |

The reader uses these rules:

- The file is CSV text with the extension `.csv`, `.tsv` or `.txt`. It does not read
  XLSX files: export the BOM as CSV.
- The reader reads CSV by the rules of RFC 4180 (Excel, kicad-cli, the CSV preset of
  KiCad). It does not guess the format. The default quote is `"`.
- For an other quote, use `--quote` (MCP: `bom_quote`): `'` for the Semicolons
  preset of KiCad, `none` for its TSV preset, or the text of a preset. When each
  name of the header is between `'`, the reader asks for the quote `'`.
- The delimiter is the one of comma, semicolon, tab and `|` that splits the header
  line. When more of them split it, the reader asks for the delimiter (`--delimiter`,
  MCP: `bom_delimiter`, a text or `tab`). When none splits it, the reader asks for
  the delimiter, the quote or the column.
- A header with one column name is a file with one column only when the name is a
  known name (for example `MPN`) or the name of `--column`.
- The first line `sep=;` of Excel gives the delimiter. When LibreOffice saved the
  file again, the delimiter after the marker counts. A different delimiter in the
  option gives an error, and so does a line `sep=` that does not give one
  delimiter.
- A line with only spaces or delimiters is an empty line, also before the header.
- Each line must follow RFC 4180. A value with the quote, the delimiter or a line
  break is between quotes, and a quote in it is doubled. Each other line gives an
  error with the number of the line where its record starts.
- A line with fewer values than the header gets empty values. (Excel can leave out
  the empty values at the end of a line.)
- A line with more values gives an error when a value after the last column is not
  empty. The cause is a delimiter in a value with no quotes, which moves the
  columns.
- A column with no name is permitted, but the reader does not use it.
- More MPN columns can have one name, for example the fields `MPN` and `mpn` of
  symbols from different libraries. Each one is an MPN field (see "MPN and Value
  fields"). The comparison of the names ignores case, spaces, `_`, `.` and `-`.
- More Value, DNP, Reference or Datasheet columns are permitted, also with
  different names of one list below (for example `DNP` and `Do not populate`). The
  first filled one gives the value, and each MPN of each Value column counts.
- A line in which these columns are different gets a conflict, also a line with no
  MPN. A value is compared by the words of its general value and the list of its
  MPNs (see above), and two codes of one part (`(TLV755P)` and `(TLV75533PDBVR)`)
  are a conflict too: the user corrects the columns. References are compared
  as a set (`R1 R2`, `R1, R2` and the range `R1-R2` are the same). A datasheet is
  compared as text.
- A line is not fitted only when each DNP column says so. Thus the line of a DNP
  conflict is fitted. `~` is an empty value (KiCad).
- A preset of KiCad can have a delimiter and a quote of more characters. Give them
  with `--delimiter` and `--quote`.
- The file must be UTF-8 text, or UTF-16 with its byte order mark (Excel "Unicode
  text"). When it is not UTF-8, the reader reads it as cp1252 (a BOM file from
  Windows), and the first line of the result says `read as cp1252`.
- The reader compares the column names with no regard to case, spaces, `_`, `.` and
  `-`. Thus `Manufacturer_Part_Number` is the same as `Manufacturer Part Number`.
- The MPN fields are the option `--column` (MCP: `bom_column`), or else each column that the file has
  of: `MPN`, `Manufacturer Part Number`, `Manufacturer Part Number 1`, `MPN 1`, `MP`,
  `Mfr Part Number`, `Mfr PN`, `Manufacturer PN`. For each line, the first filled MPN
  field gives the MPN: in Eagle, each library puts the MPN into a different
  attribute (`MP` or `MANUFACTURER_PART_NUMBER`). A column `Part Number` is not
  used: it often holds a part number of the company.
- The reader always reads the `Value` column, also with `--column`: it gives the
  general value and the MPNs in parentheses, and the check finds the changes and
  the conflicts with it (see "MPN and Value fields").
- The reference column is the first of: `Reference`, `References`, `Refs`,
  `Designator`, `Designators`, `RefDes`, `Parts`.
- The Datasheet column is the first of: `Datasheet`, `Datasheet URL`,
  `Datasheet Link`.
- The DNP column is the first of: `DNP`, `Do not populate`, `Do not place`,
  `Not fitted`. A line with a value in this column is not fitted. The values
  empty, `0`, `no` and `false` mean "fitted". DNP is a setting of the CAD tool
  that the user can change at any time, and the symbol stays in the schematic.
  Thus the parts of a line that is not fitted stay parts of the CAD project, with
  their datasheets. The line `not fitted (DNP)` of the result names these symbols
  and their parts. A part that a BOM file of a different variant fits on the same
  symbol is not in this line. A symbol that is not fitted gets the same check of
  its fields (changes, confirmation, LCSC number) as a fitted symbol, because the
  user can fit it at any time.

The first line of the result gives the MPN, Value and reference columns of each BOM
file. The other lines are the lines of `project_check`, with the number of CAD
lines with a part. The
CLI command gives CLI commands as the next steps.

The reader does not depend on one CAD tool. `project_check` also takes a list of parts
(`parts`) from any source, for example the MCP server of a CAD tool.

### The JLCPCB number of a symbol

For the JLCPCB assembly, each symbol needs the JLCPCB (LCSC) number of its part, for
example `C47616`. The reader finds the LCSC column by the names that the KiCad
plugins for JLCPCB use: `LCSC`, `LCSC Part #`, `LCSC Part Number`, `LCSC Part`,
`LCSC PN`, `JLCPCB Part #` and other forms. With `parts`, give the field `lcsc` of
each part, and `lcsc_field` when the field has a different name.

The check runs only in a project that uses JLCPCB: a project part has a JLCPCB
purchase source (`[[part.source]]`), or the CAD data has an LCSC field. The lines
`LCSC:` give:

| Line | Meaning |
|---|---|
| The numbers that agree | The number of symbols whose number is the JLCPCB purchase source of the part. |
| The field has no JLCPCB number | The part has a JLCPCB purchase source: write it into the field (the column of the BOM, else `LCSC`). |
| The project has no JLCPCB purchase source | The field has a number: save it with `adopt` when the user agrees. |
| The field gives X, the purchase source is Y | One of them is wrong: tell the user. |
| Not a JLCPCB number | The text is not `C` and digits. |
| X is a different MPN in the library | The number or the MPN is wrong (needs the JLCPCB parts library). |
| The library does not have X | A wrong number, or an old database (`datasheet-lens jlc update`). |
| No JLCPCB number | Select a part of the JLCPCB library with the user, and save it with `adopt`. |

## Team workflow

Put these files into the repository of the design:

- The project file `datasheet-lens.toml`.
- The document folder (default `datasheets/`) with the copies.

Do not put the central store into the repository. It is local to each user and
machine, and it serves all projects of the user: a datasheet and the facts that one
project added are available in each other project of the same user (see
[installation.md](installation.md#the-central-store)). Between persons, the
repository gives the documents: `datasheet-lens sync` imports the copies of the
repository into the store of each person.

On a different machine, each person does these steps:

1. Install datasheet-lens and run `datasheet-lens init` (see
   [installation.md](installation.md)).
2. Clone the repository.
3. In the repository, run `datasheet-lens sync`.

Sync imports the copies of the folder into the local store, and links each document
to its part and base part from the project file. Then the index and the element map
are built. The facts are in the local store of each user: the repository does not
share them.

With a team server (see [team-server.md](team-server.md)), the team also shares the
store: the documents, the parts, the facts, and the index and element map of each
document. A new person connects the store (`datasheet-lens server connect URL`),
clones the repository, and runs `datasheet-lens sync`: it gets the files of the
documents of the project from the server.

Datasheets are documents of their manufacturers. Before you put the copies into a
public repository, make sure that the terms of each document let you do that.
