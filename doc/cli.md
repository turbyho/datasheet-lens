# CLI reference

The command `datasheet-lens` has one subcommand for each task. This document gives
all subcommands and options. `datasheet-lens --help` and
`datasheet-lens COMMAND --help` give the same information in short form.

```text
datasheet-lens [-h] [--version] COMMAND ...
```

| Option | Effect |
|---|---|
| `-h`, `--help` | Show the help and stop. Each subcommand has this option. |
| `--version` | Show the version and stop. |

## Common arguments

| Argument | Meaning |
|---|---|
| `DOC`, `document` | A document of the store: its numeric document ID (from `docs`), or a prefix of its SHA-256 with 8 characters or more. The tools show the first 12 characters. |
| `MPN` | A part number. Case is ignored. |
| `-p PATH`, `--path PATH`, `--project PATH` | The project root (the folder of `datasheet-lens.toml`). Default: the current folder or its nearest parent folder with a project file. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The command is done. |
| `1` | A file cannot be read (missing or damaged), or an element map build failed during `sync`. |
| `2` | A wrong input, or a state that you can repair. The error is one line: `datasheet-lens: error: ...`. |
| `130` | The user stopped the command (Ctrl-C). |

## Setup and information

### info

```text
datasheet-lens info
```

Show the version and the location of the central store.

### init

```text
datasheet-lens init [--layout {ask,yes,no}]
```

Make the central store, and select the element map backend. See
[installation.md](installation.md).

| Option | Effect |
|---|---|
| `--layout {ask,yes,no}` | Install docling for the element map: ask (default), yes or no. |

### mcp

```text
datasheet-lens mcp
```

Run the MCP server on stdin and stdout. It is the same as `datasheet-lens-mcp`. See
[mcp-server.md](mcp-server.md).

## Documents in the store

### add

```text
datasheet-lens add [-m MPN] [-b MPN] [-y] [-p PROJECT] [--no-project]
                   [-M MANUFACTURER] [-c CATEGORY] [--role ROLE]
                   [--title TITLE] [--url URL] PATH|URL
```

Add a document to the store, link it to its part and build its index. In a project
folder, also add the part to the project, copy the document into the document folder
and build its element map. See [projects.md](projects.md).

| Argument or option | Effect |
|---|---|
| `PATH\|URL` | The path of a PDF file, or an http or https URL of a PDF to download. |
| `-m MPN`, `--mpn`, `--orderable` | The concrete orderable MPN that the design uses, for the project. |
| `-b MPN`, `--base`, `--part` | The base part of the datasheet. Without it, the tool proposes one from the PDF. |
| `-y`, `--yes` | Accept the proposed part with no question. |
| `-p PROJECT`, `--project`, `--path` | The project root. |
| `--no-project` | Add the document to the store only, not to the project. |
| `-M MANUFACTURER`, `--manufacturer` | The manufacturer of the part. |
| `-c CATEGORY`, `--category` | The function of the part, for example `ADC`, for the layout of the project. |
| `--role ROLE` | The role of the document: `datasheet` (default), `manual`, `errata`, `appnote` or `other`. |
| `--title TITLE` | The title. Default: the title in the PDF metadata. |
| `--url URL` | The source URL of a local file. A downloaded file records its URL itself. |

When the input is not a terminal, the command does not ask. Then give `-b` or `-y`.
A file that is in the store already (the same SHA-256) is not copied again.

### parts

```text
datasheet-lens parts
```

List the parts of the store, with the manufacturer and the number of documents.

### docs

```text
datasheet-lens docs [--part PART]
```

List the documents of the store: ID, SHA-256 prefix, pages, file name and parts.

| Option | Effect |
|---|---|
| `--part PART` | Only the documents of this part (MPN or alias), with their role. A concrete part also gives the documents of its base part. |

### index

```text
datasheet-lens index [--force] [documents ...]
```

Build the index of documents: the page text, the outline, the captions and the page
quality. With no document, build it for all documents. The command does not build
again an index of the current format (`INDEX_FORMAT`): a new release of the tool
alone does not build it again.

| Argument or option | Effect |
|---|---|
| `documents` | Document IDs or SHA-256 prefixes. |
| `--force` | Build again also a current index. |

### elements

```text
datasheet-lens elements [--backend {auto,docling,text}] [--force] [documents ...]
```

Build the element map of documents. With no document, build it for all documents.
The command does not build again a map that the same version and backend built.

| Argument or option | Effect |
|---|---|
| `documents` | Document IDs or SHA-256 prefixes. |
| `--backend {auto,docling,text}` | `auto` (default): the backend that `init` selected. `docling`: the layout model. `text`: the text layer. |
| `--force` | Build again also a current map. |

### codes

```text
datasheet-lens codes DOC
```

Show the orderable codes of a datasheet from its text layer: the code, its page and
its line. A code from a line of an ordering table comes first. A code from other text
has the mark `(in the text)`. A datasheet that gives only a scheme of its codes has
no list. The document must have an index.

## Reading

### show

```text
datasheet-lens show document
```

Show the index of a document as Markdown: the outline, the table and figure captions,
the pages with no text layer, and the pages with a raster image below 300 dpi.

### outline

```text
datasheet-lens outline --under UNDER document
```

Show the outline entries under one section, with pages.

| Option | Effect |
|---|---|
| `--under UNDER` | Necessary. The section number, for example `8.27`. |

### search

```text
datasheet-lens search (--document DOCUMENT | --part PART) [--max MAX] [-w | -r] [-a]
                      text
```

Find a text in the text layer of a document, or of all documents of a part. Each line
of the result gives the SHA-256 prefix, the page and the line.

| Argument or option | Effect |
|---|---|
| `text` | The text to find. Case is ignored. A space between a number and its unit is optional. |
| `--document DOCUMENT` | Search this document. |
| `--part PART` | Search all documents of this part. |
| `--max MAX` | The maximum number of lines (default 30). |
| `-w`, `--word` | Whole words only. |
| `-r`, `--regex` | The text is a Python regular expression. |
| `-a`, `--all` | Also show the page headers and footers (lines that repeat on many pages). |

Give `--document` or `--part`, not the two. Give `-w` or `-r`, not the two.

### page-map

```text
datasheet-lens page-map [--all] document page
```

Show the structure of a page: headings, tables, figures, formulas and captions, with
element ID, type and region in pt. The other text elements are only counted. The
document must have an element map.

| Argument or option | Effect |
|---|---|
| `page` | The page number, from 1. |
| `--all` | Also show the text elements. |

### element-text

```text
datasheet-lens element-text document ids [ids ...]
```

Show the full text of elements, with type, page and region.

| Argument | Effect |
|---|---|
| `ids` | Element IDs, for example `p7.T1`. |

### items

```text
datasheet-lens items document [{table,figure,caption}]
```

Show the tables, figures and captions of a document, with element ID, page, region
and caption. Give a type to show only that type.

### page-image

```text
datasheet-lens page-image [--element ELEMENT] [--bbox BBOX] [--max-px MAX_PX]
                          document [page]
```

Render a page, a region or an element as a PNG file in the render cache of the store.
The command shows the path of the file, the page, the region, the dpi and the size.

| Argument or option | Effect |
|---|---|
| `page` | The page number. Not necessary with `--element`. |
| `--element ELEMENT` | An element ID from the element map, for example `p9.F2`. The image shows the element with a margin of 6 pt. |
| `--bbox BBOX` | A region `x0,y0,x1,y1` in pt, origin at the top left of the page. |
| `--max-px MAX_PX` | The maximum long edge in pixels: 256 to 8000 (default 2576). |

Give `--element` or `--bbox`, not the two. A region or an element is rendered at
300 to 600 dpi, and stays within `--max-px`. A full page gets the resolution that
fits `--max-px`.

### pdf-pages

```text
datasheet-lens pdf-pages document pages
```

Write pages of a document to a new PDF file in the render cache of the store, and
show its path.

| Argument | Effect |
|---|---|
| `pages` | The pages, for example `7` or `3-5,8`. |

## Project

These commands use the project file `datasheet-lens.toml`. See
[projects.md](projects.md).

### project init

```text
datasheet-lens project init [--folder FOLDER] [--layout LAYOUT] [-p PATH]
```

Make the project file and the document folder. Put the datasheet-lens block into
`AGENTS.md` and `CLAUDE.md` when these files exist. A second run updates only the
block.

| Option | Effect |
|---|---|
| `--folder FOLDER` | The document folder, relative to the project (default `datasheets`). |
| `--layout LAYOUT` | The subfolder of each copy: a pattern with `{manufacturer}`, `{category}` and `{mpn}`, or `""` (default `{manufacturer}`). |

### project status, ls

```text
datasheet-lens project status [-p PATH]
datasheet-lens ls [-p PATH]
```

Show the parts and documents of the project, and the copies that are missing or
changed. The command changes nothing.

### project sync, sync

```text
datasheet-lens project sync [--restore] [-p PATH]
datasheet-lens sync [--restore] [-p PATH]
```

Copy the documents into the document folder, move the copies to the subfolders of the
layout, import the new files of the folder into the store, and build the element maps.

| Option | Effect |
|---|---|
| `--restore` | Copy a changed copy again from the store. |

### project add-part

```text
datasheet-lens project add-part [--manufacturer MANUFACTURER] [--category CATEGORY]
                                [--base MPN] [--document DOCUMENT] [--role ROLE]
                                [-p PATH] mpn
```

Add a part to the project, with the documents that the store links to it.

| Argument or option | Effect |
|---|---|
| `mpn` | The part number (MPN) or an alias. |
| `--manufacturer MANUFACTURER` | The manufacturer of the part. |
| `--category CATEGORY` | The function of the part, for example `ADC`, for the layout. |
| `--base MPN` | `mpn` is a concrete orderable part of this base part (the part of the datasheet). |
| `--document DOCUMENT` | Link this document (ID or SHA-256 prefix) to the part first. With `--base`, the link goes to the base part. |
| `--role ROLE` | The role of that document (default `datasheet`). |

### set

```text
datasheet-lens set [-m MPN] [-b MPN] [-c CATEGORY] [-M MANUFACTURER] [--of MANUFACTURER]
                   [-p PATH] MPN
```

Change a part of the project in place. The part keeps its place in the project file
and its copies. Then run `sync` to move the copies to the subfolders of the layout.

| Argument or option | Effect |
|---|---|
| `MPN` | The MPN of the project part now. |
| `-m MPN`, `--mpn` | The new MPN, for example the concrete orderable MPN. |
| `-b MPN`, `--base` | The base part of the datasheet. |
| `-c CATEGORY`, `--category` | The function of the part. `""` removes it. |
| `-M MANUFACTURER`, `--manufacturer` | The new manufacturer. |
| `--of MANUFACTURER` | The manufacturer now, when the project has more parts with the MPN. |

### rm

```text
datasheet-lens rm [-d DOC] [-M MANUFACTURER] [--pinned] [-n] [-p PATH] [MPN]
```

Remove a part, or a document, from the project. Delete the copies that no other part
uses. The central store does not change.

| Argument or option | Effect |
|---|---|
| `MPN` | The part to remove. With `-d`: remove the document only from this part. |
| `-d DOC`, `--document` | A document to remove: its ID, a SHA-256 prefix or its file name. |
| `-M MANUFACTURER`, `--manufacturer` | The manufacturer, when the project has more parts with the MPN. |
| `--pinned` | Also delete a file that you put into the folder yourself. |
| `-n`, `--dry-run` | Only show what would change. |

Give `MPN`, `-d DOC`, or the two.

### check

```text
datasheet-lens check [--column COLUMN] [-p PATH] BOM.csv
```

Compare the project with a BOM file (CSV) of the CAD project. Show the conflicts in
the CAD project (an MPN field and the Value field that name different parts), and
the same, different, missing and extra parts, with the next command for each one.

| Argument or option | Effect |
|---|---|
| `BOM.csv` | The BOM as a CSV file, for example from `kicad-cli sch export bom`. |
| `--column COLUMN` | The MPN field. Default: each column that the file has of `MPN`, `Manufacturer Part Number`, `Manufacturer Part Number 1`, `MPN 1`, `MP`, `Mfr Part Number`, `Mfr PN`, `Manufacturer PN`; for each line, the first filled one. The `Value` column is always read too. |

The command reads a CSV file with a comma, a semicolon or a tab as the separator. It
reads UTF-8, UTF-16 with its byte order mark, or cp1252 when the file is not UTF-8.
It does not read XLSX files. It
compares the column names with no regard to case, spaces, `_`, `.` and `-`. It finds
the reference, Datasheet and DNP columns by their names, and ignores the lines that
are not fitted. In the Value column it finds the MPNs: a value and an MPN, or more
MPNs (alternatives) with any separator. **It is tested only with BOM exports of
KiCad and Eagle.** A BOM of a different CAD tool (for example Altium) can work when
its column names are in the lists, but nobody has tested it. See [projects.md](projects.md#with-the-cli-and-a-bom-file) for the full
rules.

## Distributors and links

These commands are described in [distributors.md](distributors.md).

### find

```text
datasheet-lens find [-x] [-p PATH] MPN
```

Find a part at the enabled distributors: the concrete MPNs, the lifecycle state, the
stock, the prices, the package and the datasheet URL. It needs an API key.

| Argument or option | Effect |
|---|---|
| `MPN` | An MPN, or the start of the MPNs (a base part, 3 characters or more). |
| `-x`, `--exact` | Only this MPN. |

### stock

```text
datasheet-lens stock [-p PATH]
```

Show the lifecycle state and the stock of each project part, with a warning for each
part that is not orderable, that the distributor does not have, or that has no stock.

### dist

```text
datasheet-lens dist [--local] [-p PATH] [NAME ...]
```

Show or set the distributors that datasheet-lens asks. With no option and no `NAME`,
show the enabled distributors and the state of their keys.

| Argument or option | Effect |
|---|---|
| `NAME` | The distributors, for example `mouser`. They become the global setting. |
| `--local` | Set the names in the project file. With no `NAME`, remove the setting from the project file: the project then uses the global setting. |

### key

```text
datasheet-lens key [--delete] [NAME]
```

Save the API key of a distributor. The command asks for the key and does not show it.
With no `NAME`, show where each key is (no values).

| Argument or option | Effect |
|---|---|
| `NAME` | The distributor: `mouser`. |
| `--delete` | Remove the saved key from the key store and from the key file. |

When the input is not a terminal, the command reads the key from the first line of
the input.

### links

```text
datasheet-lens links MPN
```

Show search links for a part: datasheet, price and stock, and symbol, footprint and 3D
model. The command does not open the links.
