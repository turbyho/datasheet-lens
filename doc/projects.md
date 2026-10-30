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
2. It makes the document folder.
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
- Check the distributors with `project_availability`.
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
distributors = ["mouser"]

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
| `distributors` | A list of distributor names. When the key is missing, the project uses the global setting. | missing |

TOML puts each key after a table header into that table. Thus the top-level keys must
come before the first `[[part]]`.

### Keys of `[[part]]`

| Key | Value |
|---|---|
| `mpn` | Necessary. The MPN of the part in the BOM: the concrete orderable MPN. |
| `manufacturer` | The manufacturer, as the project writes it (the layout uses it as a folder). It can be any name of the manufacturer that the store knows (see `datasheet-lens maker` in [cli.md](cli.md)). Necessary when two parts have the same MPN. |
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

A `[[part.document]]` block belongs to the `[[part]]` above it. If a person moves a
block, the document goes to a different part. Sync finds this case when the store
links the document to a different part, and reports it as a problem.

### The text of the user stays

datasheet-lens writes the project file with tomlkit. A write keeps the comments, the
format, the order and the keys that datasheet-lens does not know. It changes only the
values that changed, and adds new parts and documents at the end. Before a write, the
tool reads the new text back and compares it with the data. If the file changed after
the tool read it, the tool writes nothing and tells you to run the command again.

Write `part` and `part.document` as arrays of tables (`[[part]]`), not as inline
arrays. The tool cannot add to an inline array.

## Layout

The key `layout` is a pattern for the subfolder of each copy in the document folder.
It can use these placeholders:

| Placeholder | Value |
|---|---|
| `{manufacturer}` | The manufacturer of the part. |
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

1. It adds the documents that the store links to each project part (and to its base
   part). It does not add a document that the part excludes.
2. It copies each missing copy from the store into the document folder.
3. It moves each copy to the subfolder of the layout. It does not move a pinned file.
4. It imports each new PDF file of the document folder into the store.
5. It builds the index of each project document that has none.
6. It writes the project file.
7. The CLI builds the element map of each document. The MCP tool builds them in the
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

### New files in the folder

You can put a PDF file into the document folder yourself. Sync imports it into the
store. When the store links the document to a project part, sync adds it to that
part with `pinned = true`. Else sync proposes a part from the file name. Assign the
file with `datasheet-lens project add-part MPN --document DOC` (CLI) or
`project_add_part` (MCP).

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

`set` keeps the part at its place in the project file, and keeps its copies. Use it,
not a removal and a new addition. After a change, run `datasheet-lens sync` to move
the copies to the subfolders of the layout. Give a name of the manufacturer that the
store knows (`datasheet-lens maker`). With a name that the store does not relate to
the manufacturer of the part, the command stops with an error and tells how to record
the name. Thus the store does not get a second part with the same MPN.

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

### With the MCP tools

1. The agent gets the parts of the CAD project from the CAD tool, for example from its
   MCP server or its BOM export: for each symbol, the MPN field, the Value field, the
   Datasheet field and the reference, as they are.
2. It calls `project_check(parts)` with all parts. The tool finds the MPNs (see "MPN
   and Value fields" below). Each part with an MPN needs its datasheet, also a
   passive part with an MPN in its Value.
3. The result gives one line for each conflict in the CAD project, the number of the
   same parts, and one line for each part that is:
   - `different`: probably one part with two MPNs (for example the base part in the
     project and the concrete MPN in the CAD project). The line gives the
     `project_update_part` call.
   - `missing`: in the CAD project, not in the project file. The line gives the
     documents of the store, the probable base part, or the Datasheet field, with
     the next call. When it finds nothing, it gives `part_links`. An alternative of
     a part is a missing part too, with the note `[alternative of MPN]`.
   - `extra`: in the project file, not in the CAD project. The agent tells the user
     and removes it only when the user agrees.
4. After the changes, the agent runs `project_sync` and `project_check` again.

### MPN and Value fields

The MPN of a symbol can be in an MPN field, in the Value field, or in both. The tool
uses these rules (the MCP tool and the CLI command are the same):

- The first filled MPN field gives the main MPN. With no MPN field, the first MPN in
  the Value field gives it.
- The Value field can hold a value (`100nF`), an MPN, a value and an MPN
  (`390uF (EEU-FS1H391L)`, `10uF/10V (GRM188R61A106KE69D)`) or more MPNs
  (`AP2205-33YR-13 / AP7370-33Y-13`). The tool finds each MPN in it. A value with a
  unit (`100nF`, `4.7uF/10V`, `>=120R`) and the name of a generic symbol
  (`Conn_01x05_Socket`) are not MPNs.
- Each person can use a different separator between MPNs: `/`, `,`, `;`, `|`, a
  word, or a different sign. Each character that an MPN does not have (an MPN has
  letters, digits and `- / . + # =`) separates. A slash with no spaces separates two
  MPNs only when the part after it is a full MPN with a digit:
  `AP2205-33YR-13/AP7370-33Y-13` gives two MPNs, but `LM358DR/NOPB` and
  `MCP1827S-3302E/EB` are one MPN.
- Words of one part give one MPN, the longest: `OPA2134 (OPA2134PA)` gives
  `OPA2134PA`. A word with `_` is the name of a generic symbol, not an MPN
  (`USB_C_Receptacle_GCT_USB4105`).
- The other MPNs of the Value field are alternatives (a second source). An
  alternative has 6 or more characters: an ordering suffix such as `N16R8` of
  `ESP32-S3-WROOM-1 N16R8` is not an alternative. Each alternative needs its own
  datasheet: the Datasheet field of the symbol belongs to the main part.
- A conflict: an MPN field and the Value field (or two MPN fields) name different
  parts, for example `MP BSS138` and `Value 2N7002`. In Eagle, this occurs when a
  person changes the Value and the attributes of the old library part stay. One of
  the fields is wrong: the agent tells the user. The check uses the main MPN.
- Not a conflict: the same part with different signs (`LTC3588EMSE#PBF` and
  `LTC3588EMSEPBF`), a longer code of the same part (`TMP112D0` and
  `TMP112D0IDPWR`, `TPS7A02` and `TPS7A0233PYCHR`), or the base part of a code with
  digits in it (`TLV755P` and `TLV75533PDBVR`): a symbol name in the Value and the
  orderable code in the MPN field are normal.

### With the CLI and a BOM file

Export a BOM as CSV from the CAD tool, then compare it:

```sh
kicad-cli sch export bom --fields "Reference,Value,MPN,Datasheet,DNP" \
    --labels "Reference,Value,MPN,Datasheet,DNP" --output bom.csv design.kicad_sch
datasheet-lens check bom.csv
```

Replace `MPN` with the name of the MPN field of your symbols.

The command has a generic CSV reader. It finds the columns by their names.

**Tested only with BOM exports of KiCad and Eagle.** A BOM of a different CAD tool
can work when its column names are in the lists below, but nobody has tested it.

| CAD tool | State |
|---|---|
| KiCad (`kicad-cli sch export bom`) | Tested with real BOM exports. |
| Eagle | Tested with a real BOM export (semicolon, the attributes `MP` and `MANUFACTURER_PART_NUMBER` of different libraries, `Parts`, `Value`). |
| Fusion, Altium and other tools | Not tested. The column names of Altium (`Designator`, `Manufacturer Part Number 1`) are in the lists. |

The command reads the BOM with these rules:

- The file is CSV. The command finds the column separator: a comma, a semicolon or a
  tab. It does not read XLSX files: export the BOM as CSV.
- The file must be UTF-8 text, or UTF-16 with its byte order mark (Excel "Unicode
  text"). When it is not UTF-8, the command reads it as cp1252 (a BOM file from
  Windows), and the first line of the result says `read as cp1252`.
- The command compares the column names with no regard to case, spaces, `_`, `.` and
  `-`. Thus `Manufacturer_Part_Number` is the same as `Manufacturer Part Number`.
- The MPN fields are the option `--column`, or else each column that the file has
  of: `MPN`, `Manufacturer Part Number`, `Manufacturer Part Number 1`, `MPN 1`, `MP`,
  `Mfr Part Number`, `Mfr PN`, `Manufacturer PN`. For each line, the first filled MPN
  field gives the MPN: in Eagle, each library puts the MPN into a different
  attribute (`MP` or `MANUFACTURER_PART_NUMBER`). A column `Part Number` is not
  used: it often holds a part number of the company.
- The command always reads the `Value` column, also with `--column`: it gives the
  MPN when no MPN field has one, the alternatives and the conflicts (see "MPN and
  Value fields").
- The reference column is the first of: `Reference`, `References`, `Refs`,
  `Designator`, `Designators`, `RefDes`, `Parts`.
- The Datasheet column is the first of: `Datasheet`, `Datasheet URL`,
  `Datasheet Link`.
- The DNP column is the first of: `DNP`, `Do not populate`, `Do not place`,
  `Not fitted`. A line with a value in this column is not fitted, and the command
  ignores it. The values empty, `0`, `no` and `false` mean "fitted".

The first line of the result gives the MPN columns, the reference column and the
number of lines with an MPN. The other lines are the same as the lines of
`project_check`, with CLI commands as the next steps.

The MCP tool `project_check` does not read a file. It takes a list of parts from any
source: the MCP server of a CAD tool, a BOM export, or a different tool. Thus it does
not depend on one CAD tool.

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

Datasheets are documents of their manufacturers. Before you put the copies into a
public repository, make sure that the terms of each document let you do that.
