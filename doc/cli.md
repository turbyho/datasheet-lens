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

### maker

```text
datasheet-lens maker
datasheet-lens maker alias NAME OTHER
datasheet-lens maker distinct NAME OTHER
```

Show the manufacturer registry of the store, or record how two manufacturer names
relate. A part keeps the manufacturer name of its datasheet. The registry tells if two
names are one manufacturer, so that the store does not get a second part with the
same MPN.

| Relation | Meaning |
|---|---|
| (none) | Show each manufacturer, its other names and the manufacturers that are not the same. |
| `alias NAME OTHER` | One manufacturer with two names, for example `TI` and `"Texas Instruments"`. |
| `distinct NAME OTHER` | Two manufacturers. Each can make a part with the same MPN (a second source). |

Two spellings of one name are always the same manufacturer, with no relation: the
comparison ignores case, punctuation, spaces and a legal suffix (`Microchip`,
`Microchip Inc.` and `MICROCHIP TECHNOLOGY INC.` with the alias `Microchip
Technology`). The registry does not follow the name changes and mergers of companies.

The registry knows the names of the built-in manufacturer table, and some short names
(`TI`, `ADI`, `ST`). Only you record a
relation: the registry does not learn one from the data, because two manufacturers
can make a part with the same MPN and one datasheet. With no relation, `maker` also
shows the pairs of names to examine: two parts with the same MPN and these names
have the same documents (for example `TI` and `Texas Instruments`).

When the registry cannot tell if two names are one manufacturer, a command that adds
a part stops with an error. Record the relation, then do the command again. After
`alias`, the parts with the same MPN of the two names merge into the oldest part. That
part keeps its manufacturer name.

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
datasheet-lens check [--column COLUMN] [--delimiter D] [--quote Q] [--confirmed-column NAME] [-p PATH] BOM.csv [BOM.csv ...]
```

Compare the project with a BOM file (CSV) of the CAD project. Show these items, with
the next command for each one:

- The conflicts in the CAD project: the fields or the columns of one symbol do not
  agree, for example two DNP columns with different values.
- The changes of the fields `Value` and `MPN` that the user must confirm.
- The symbols with no confirmed fields, the symbols with an MPN that is not in
  Value (the fields to complete), and the symbols with no part.
- The same, different, missing and extra parts.

| Argument or option | Effect |
|---|---|
| `BOM.csv` | The BOM as a CSV file, for example from `kicad-cli sch export bom`. More files: one for each variant of the product, the default variant first. The parts of all files count, and each file gets the check of the confirmed fields. |
| `--column COLUMN` | The MPN field. Default: each column that the file has of `MPN`, `Manufacturer Part Number`, `Manufacturer Part Number 1`, `MPN 1`, `MP`, `Mfr Part Number`, `Mfr PN`, `Manufacturer PN`; for each line, the first filled one. The `Value` column is always read too. |
| `--delimiter D` | The delimiter of the values: a text (a preset of KiCad can have more characters) or `tab`. Default: the one of `,` `;` tab `\|` that splits the header line. When more of them split it, or none (and the header is not one known column name), the command asks for this option. |
| `--quote Q` | The quote: `"` (default: CSV, Excel, kicad-cli), `'` (the Semicolons preset of KiCad), `none` (the TSV preset of KiCad), or an other text of a preset. |
| `--confirmed-column NAME` | The column of the field `Value_Confirmed`, for example an unused custom column of the BOM of an other CAD tool (default: `Value_Confirmed`). |

The command reads a CSV file (RFC 4180) with the delimiter and the quote of the
options (see [projects.md](projects.md#a-bom-file) for the rules). It reads UTF-8,
UTF-16 with its byte order mark, or cp1252 when the file is not UTF-8. It does not
read XLSX files. It
compares the column names with no regard to case, spaces, `_`, `.` and `-`. It finds
the reference, Datasheet and DNP columns by their names, and ignores the lines that
are not fitted. The Value column holds a general value and the MPNs in parentheses,
the fitted part first and then the alternatives, with the separators `,` and `;`
(see [projects.md](projects.md#mpn-and-value-fields)). **It is tested only with BOM exports of
KiCad and Eagle.** A BOM of a different CAD tool (for example Altium) can work when
its column names are in the lists, but nobody has tested it. See [projects.md](projects.md#a-bom-file) for the full
rules.

## Distributors and links

These commands are described in [distributors.md](distributors.md).

### find

```text
datasheet-lens find [-x] [-p PATH] MPN
```

Find a part at the enabled distributors: the concrete MPNs, the lifecycle state, the
stock, the prices, the package (DigiKey only) and the datasheet URL. With no key, it
gives a note with the steps and the search links of `links`.

| Argument or option | Effect |
|---|---|
| `MPN` | An MPN, or the start of the MPNs (a base part, 3 characters or more). |
| `-x`, `--exact` | Only this MPN. |

### pick

```text
datasheet-lens pick [-P] [-a] [-s stock|price] [-n N] [-o N] [-k WORDS] [-p PATH] SOURCE CATEGORY [FILTER ...]
```

Find parts by their parameters (a parametric search) at DigiKey or in the JLCPCB parts
library. Each search names one source. DigiKey needs DigiKey in the enabled
distributors, and the DigiKey key. The JLCPCB library needs `datasheet-lens jlc
update`. See
[distributors.md](distributors.md#find-parts-by-their-parameters) and
[distributors.md](distributors.md#jlcpcb-parts-library).

| Argument or option | Effect |
|---|---|
| `SOURCE` | `digikey` (the stock and the lifecycle state at DigiKey) or `jlcpcb` (the JLCPCB stock for the assembly and the library class). |
| `CATEGORY` | The category name, or a part of it, for example `"Single FETs, MOSFETs"` or `mosfet`. DigiKey: also the ID. JLCPCB: a subcategory name or `CATEGORY > SUBCATEGORY`, more with `\|`. A DigiKey category with subcategories, or a name that fits more than one category, gives the list of the categories to select from, with each name in quotes. |
| `FILTER` | `NAME OP VALUE [at CONDITION]`, for example `"vdss >= 20 V"` or `"package has 3x3"`. The tool does not apply a filter with a condition (after `at` or `@`): read that value in the datasheet. A filter with no value that agrees gives 0 parts and examples of the values. |
| `-P`, `--params` | Show the parameters of the category, with their short names, the number of values and examples. Give no `FILTER` with it. |
| `-a`, `--all` | Also the parts with no stock (JLCPCB: marked `NOT IN STOCK`). |
| `-s`, `--sort` | `stock` (most first, the default) or `price` (lowest first). |
| `-n`, `--limit` | The number of parts, 1 to 50 (default 20). JLCPCB: 1 to 100. |
| `-o`, `--offset` | Skip this number of parts (the next page). |
| `-k`, `--keywords` | Words that the parts must have. |

### adopt

```text
datasheet-lens adopt [-m MPN] [-b BASE] [-c CATEGORY] [-p PATH] SOURCE [NUMBER]
```

Take a part of a source into the store and the project: download its datasheet, add
the part, and save the purchase source. See
[distributors.md](distributors.md#take-a-found-part).

| Argument or option | Effect |
|---|---|
| `SOURCE` | `digikey`, `jlcpcb` or `mouser`. |
| `NUMBER` | The part number of the source: a DigiKey number, a JLCPCB C number (`C47616`) or a Mouser number. |
| `-m`, `--mpn` | The MPN, in place of `NUMBER`. The source must have one part for it. |
| `-b`, `--base` | The base part of the datasheet, for example `TLV755P`. |
| `-c`, `--category` | The function of the part, for example `MOSFET`. |

Give `NUMBER` or `--mpn`, not both. The command does not guess if a text is a number
of the source or an MPN. Outside a project folder, the command adds the part to the
store only. In a project folder, it copies the datasheet into the document folder
(as `sync`).

### jlc

```text
datasheet-lens jlc [update | status | remove]
```

Download, update, show or delete the JLCPCB parts library on this machine. With no
action: `status`. See [distributors.md](distributors.md#jlcpcb-parts-library).

| Action | Effect |
|---|---|
| `update` | Download the database (276 MB) and put it in place of the old one (2.2 GB on the disk). The old database stays when the download fails. |
| `status` | Show the path and the age of the database. |
| `remove` | Delete the database. |

### stock

```text
datasheet-lens stock [-p PATH]
```

Show the lifecycle state and the stock of each project part, with a warning for each
part that is not orderable, that the distributor does not have, or that has no stock.

### dist

```text
datasheet-lens dist [--local] [--site CC] [--currency CUR] [-p PATH] [NAME ...]
```

Show or set the distributors that datasheet-lens asks. With no option and no `NAME`,
show the enabled distributors, the state of their keys, and the services of each
distributor.

| Argument or option | Effect |
|---|---|
| `NAME` | The distributors, for example `mouser digikey`. They become the global list. |
| `--local` | Set the names in the project file. With no `NAME`, remove the list from the project file: the project then uses the global list. |
| `--site CC` | The country of the prices and the stock, for example `CZ` (global, default `US`). DigiKey uses it. |
| `--currency CUR` | The currency of the prices, for example `CZK` (global, default `USD`). DigiKey uses it. |

`--site` and `--currency` are global: the command does not accept them with `--local`.

### server

```text
datasheet-lens server [connect URL | sync | status | disconnect]
```

Connect the store to a team server, synchronize it, show the state, or disconnect it.
With no action: `status`. See [team-server.md](team-server.md).

| Action | Effect |
|---|---|
| `connect URL` | Connect the store to the server at URL (`https://...`), and do the first sync. The token comes from `datasheet-lens key team`. |
| `sync` | Send the local changes, then get the changes of the server. |
| `status` | The server, the last sync, the local changes that wait, the last error and the notes. |
| `disconnect` | Stop the synchronization. The local rows stay. |

### key

```text
datasheet-lens key [--delete] [NAME]
```

Save the API key of a distributor, or the token of the team server. The command asks
for the key and does not show it. With no `NAME`, show where each key is (no values).

| Argument or option | Effect |
|---|---|
| `NAME` | `mouser` or `digikey` (a distributor), `digikey-id` or `digikey-secret` (one part of the DigiKey key), or `team` (the token of the team server, see [team-server.md](team-server.md)). |
| `--delete` | Remove the saved key from the key store and from the key file. For `digikey`: the two parts. |

For Mouser, use the key of the Search API, not the key of the Cart API. For DigiKey,
the command asks for the Client ID and then for the Client Secret. See
[distributors.md](distributors.md).

When the input is not a terminal, the command reads the key from the first line of
the input. For DigiKey, it reads the Client ID from the first line and the Client
Secret from the second line.

With no `NAME`, the command shows the DigiKey key as two lines: `digikey-id` and
`digikey-secret`. When a distributor list exists and does not have the distributor of
the saved key, the command tells you the `dist` command that adds it.

### links

```text
datasheet-lens links MPN
```

Show search links for a part: datasheet, price and stock, and symbol, footprint and 3D
model. The command does not open the links. When DigiKey is in the global distributor list and has
a key, the command also gives the direct links of DigiKey for the MPN: the CAD models
at SnapMagic and Ultra Librarian, and the datasheets.
