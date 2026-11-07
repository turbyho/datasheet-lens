# Architecture

This document is for developers. It tells how datasheet-lens operates inside, and why.

## Why no conversion

Many tools convert a PDF datasheet to Markdown or HTML, then give the text to an LLM.
Tests with datasheets showed that a conversion makes errors that look correct:

- An exponent becomes a plain number (`10 13` for 10<sup>13</sup>).
- Table rows merge, and columns shift.
- A layout model can reduce a large register table to one cell and lose its text.
- A vision model that transcribes a graph or a bit layout can give wrong values.

A strong model reads an original page with no loss, when the input has a good
resolution (300 dpi or more). The headings, captions, page numbers and the text layer
are reliable to find the correct place, also where a conversion loses the content.

Thus datasheet-lens does not convert. It builds an index to find the content, and
gives the original pages and regions to the model on demand. The cost follows the
work, not the size of the datasheet.

## Overview

```text
           CLI (cli.py)          MCP server (server.py)
                  \                   /
                   library modules
   (indexer, elements, pages, knowledge, identify, project, adopt,
    distributors, digikey, jlcpcb, parametric, links, makers)
                          |
             central store (store.py)
       SQLite database + content-addressed files
                          |
          team server client (teamsync.py, bundles.py)
                          |
        team server, optional (team/: API, PostgreSQL, objects)
```

The CLI and the MCP server use the same library. Most tools of the server also have a
CLI command, so that a person can do the same steps as an agent. The facts
(`save_fact`, `find_facts`, `set_fact_status`) have no CLI command.

## Download

`fetch.py` downloads a PDF from an http or https URL into a temporary folder of the
store, then `add_document` moves it into the store. datasheet-lens does not search
the web: the URL comes from the user, a distributor or the Datasheet field of a CAD
symbol. The file must start with `%PDF`: a login page or a block page gives an error.
The limit is 300 MB. When a server blocks the download, the error tells the user to
download the file and put it into the inbox `_inbox` of the project, where `sync`
gives it to its parts (see `project._inbox`).

## Central store

The store is the knowledge of one user for all design projects. A document, its
index, its element map, its parts and its facts are in the store one time. Each
project uses them: a datasheet that one project added, and the values that a
session read from it, are at once available in each other project. A project file
keeps only the list of the parts of the design and the references (SHA-256) to the
documents.

`store.py` keeps the store in one data folder (`config.py`: `~/.datasheet-lens`, or
the environment variable `DATASHEET_LENS_HOME`). The key `data` in `config.json`
can put the data folder on a different disk. `config.json` and the API keys stay
in the store folder.

- `db/lens.db`: an SQLite database in WAL mode. Many readers and one writer can use
  it at the same time.
- `blobs/<sha[:2]>/<sha>.pdf`: the files. The SHA-256 of a file is its identity. The
  database keeps the hash, the size, the type and the path, never the file content.
- `render/<sha[:16]>/`: the cache of the rendered images and PDF extracts.

The database tables:

| Table | Content |
|---|---|
| `parts` | MPN, manufacturer, category (column `family`), and `base_id` for a concrete part. |
| `part_aliases` | More names of a part. |
| `part_sources` | The purchase sources of a part: the source (`digikey`, `jlcpcb`, `mouser`), the part number of the source, the product URL, the JLCPCB library class and the date. No stock and no price. |
| `documents` | SHA-256, file name, size, pages, title, source URL. |
| `part_documents` | The links between parts and documents, with the role. |
| `builds` | The format version (`indexer.INDEX_FORMAT`, `elements.ELEMENTS_FORMAT`) and the backend of each index and element map, the versions of its tools, and its bundle on a team server. A new release of the package does not build them again; a new format does. |
| `bundles` | The bundles of derived data of a team server (from a pull). |
| `pages`, `outline`, `captions` | The index. |
| `elements` | The element map. |
| `knowledge` | The facts. |
| `maker_relations` | The relations of two manufacturer names (`alias`, `distinct`) that the users recorded, in sequence. |
| `manufacturers`, `manufacturer_aliases`, `manufacturer_distinct` | The manufacturer registry. Each store makes it from the built-in names and `maker_relations`. |
| `sync_state`, `outbox` | The local changes for a team server (see below). |

The tables `parts`, `part_aliases`, `part_sources`, `documents`, `part_documents`,
`knowledge` and `maker_relations` are shared tables. Each row has a global ID: the SHA-256 for a
document, a gid for the other rows. A gid is a UUID of version 7. Its text order is
the order of the creation of the rows. The column `version` keeps the version of the
row on a team server (0: not on a server).

When a store is connected to a team server (`sync_state.recording`), triggers write
each local change of a shared table into `outbox`, in the same transaction. A row
from the server, and a change that a shared decision causes (the merge of two parts
after a relation of manufacturer names), write no outbox row: each store and the
server make these changes themselves (`store.not_recorded`). A merge keeps the
oldest part: by `created`, then by gid. Thus each store keeps the same part.

A push sends only the changes of the tables that the server gives in
`/v1/account` (`tables`; an older server does not give them: `teamsync.BASE_TABLES`).
The changes of a different table stay in the outbox until the server knows the
table. A pull gets the changes after the cursor of the store, with the deleted rows.
The key `sync_full_pull` in `meta` (from the migration to version 6) makes the next
sync also get all live rows of the server one time, with no change of the cursor: an
older version ignored the rows of `part_sources`.

`bundles.py` puts the derived data of a document (index, element map) into a bundle
for the team server, and takes the canonical bundle of the server before a build. A
fact keeps the bbox of its element; `bundles.relink` links the facts to a new element
map by their regions.

The key `sync_triggers` in `meta` keeps the version of the triggers. When a new
release changes them, the first start makes them again in one write transaction.

A new version of the store schema has a migration in `MIGRATIONS`. The first start of
a new version runs the migrations in one write transaction. A store with a newer
schema than the tool gives an error.

A file that is in the store already is not copied again. `add_document` of the same
file gives the same document. A concrete part refers to its base part, and gets the
documents of the base part.

## Index

`indexer.py` builds the index with pypdfium2 (`pdftext.py` reads the text layer):

- The page text, as lines with positions. `pdftext.py` joins the text runs of one
  line, and puts three spaces at a column gap.
- The outline: the PDF bookmarks. With no bookmarks, the numbered headings of the
  text layer (for example `5.1 Absolute Maximum Ratings`).
- The table and figure captions (`Table 5`, `Figure 3-1`) with their position. A
  caption that repeats on a continued table is kept on its first page only.
- The page quality: the pages with no text layer, and the raster images below
  300 dpi.
- A fingerprint of each page: the SHA-256 of its text with no white space and no
  header and footer bands.

The text search (`search`) reads the lines of the page text with a regular
expression. It does not use SQLite FTS5: its token match differs, and not each SQLite
has it. A line that repeats on half of the pages or more (and on 3 pages or more) is
a page header or footer, and the search does not show it by default.

## Element map

`elements.py` builds the element map: the elements of each page with type, text,
region and an ID. The ID is `p<page>.<code><n>`, in reading order (top to bottom,
left to right). Two backends:

- `docling`: the docling layout model, with no OCR and no table structure (the model
  reads tables from the image). It reads the PDF in chunks of 50 pages. The region of
  a table or figure includes its caption.
- `text`: headings, captions and text blocks from the text layer. It has no regions
  of tables and figures.

docling uses pdfium inside. pdfium is not thread-safe, and the lock of the process
(`pdflock.py`) cannot reach docling. Thus the MCP server runs a docling build in a
child process (the CLI command `elements`), in a background thread. A crash of docling
does not stop the server.

`find_caption` finds a table by its caption label, also when the table continues on
the next pages. A part on the next page continues the table when it starts at the top
of its page and has no caption of its own, the same label, the word "continued" or
the same title.

## Page rendering

`pages.py` renders with pypdfium2, and writes the PNG file with the standard library:

- A full page gets the resolution that fits `max_px` on its long edge (default 2576,
  the maximum long edge of the current Claude models). The maximum is 600 dpi.
- A region gets 300 to 600 dpi. A region too large for 300 dpi at `max_px` gets the
  resolution that fits.
- The coordinates are in pt, with the origin at the top left of the page as a viewer
  shows it (after `/Rotate` and the CropBox).
- The rendered files stay in `render/`. The same request does not render again.

## Knowledge

`knowledge.py` keeps the facts: values that a model or a person read from the
original. A fact keeps the document, page, element or region, subject, value, unit,
conditions, column, note, the model or author, the date and a state (`unchecked`,
`checked`, `disputed`). A fact is never the source of truth: the original page is.
The state tells how much to trust the fact.

## Project file

`project.py` reads the project file with `tomllib` and writes it with tomlkit. A write
keeps the text of the user: comments, format, order and unknown keys. It changes only
the values that changed. A removed table takes with it the comment lines directly above
its header and one blank-line gap (`_remove_tables`). Before a write, it parses the new
text and compares it with the data. When the file changed after the read, it writes
nothing. All writes are atomic (`fileutil.py`): a temporary file, then a replace.

The CLI and the MCP server can work on one project at the same time. `lock.py` gives
an exclusive lock of the project folder, between processes and between threads. A
command takes it before it reads the project file (`open_project`), and holds it
until the folder and the project file are done. The functions that write the project
file or change the folder also take it (`locked`). The order of the locks is always
the same, so that no two calls wait for each other:

1. The lock of the project folder.
2. The lock of the MCP server (`_add_lock`).
3. A write transaction of the store.

A call does not take the lock of the folder when it holds the lock of the MCP server
or a write transaction of the store. An open connection to the store with no write is
no problem: `project_sync` opens the store before it takes the lock of the folder.

The lock is a file in the store folder (`locks/`), not in the project. Its name is
the hash of the path of the project folder. The index of a new document comes after
the lock (`build_indexes`). Only a file of the inbox gets its index in the lock,
because sync reads it to find its parts.

`sync` compares the project with the store and the document folder. First it takes
the PDF files of the inbox `_inbox`: it imports each one, and links it to the project
parts that it agrees with (their MPN is an ordering code of the document, or their
MPN or base part is the part of the PDF; the same manufacturer; a part with no
datasheet). Then it makes the copies, moves them to the subfolders of the layout,
and imports the new files. The central store is the source of the copies. The
project file is the source of the base parts: on a new machine, sync writes them
into the store.

A part of the project file can have purchase sources (`[[part.source]]`): the source
(`digikey`, `jlcpcb`, `mouser`), its part number and for JLCPCB the library class.
The store keeps them too (table `part_sources`, shared through the team server).
`adopt.py` takes a found part in one step: the datasheet, the part and its purchase
source.

`project_check` compares the project with the CAD parts. A CAD MPN and a project part
are "different" (probably one part) when one MPN is the base part or a variant of
the other, or one MPN starts with the other. Placeholders at the end of a CAD MPN
(`nRF9151-LAXX`, `STM32F103C8Tx`) match any characters. In a project that uses
JLCPCB, `project_check` also compares the JLCPCB (LCSC) number of each symbol with
the purchase source and with the JLCPCB parts library.

## Identification of the part

`identify.py` proposes the MPN and the manufacturer of a new document. The sources
are the title in the PDF metadata, the text of page 1 and the file name. Each MPN
candidate gets a score: 2 for the title, 1 for page 1, 1 for the file name. Rules
remove tokens that look like an MPN but are not one: revisions, document numbers,
values with units, packages (also the JEDEC outlines of diodes and transistors, for
example `SOD-123F`, `DO-214AC`, `TO-220AB`) and standards. The manufacturer comes from known names
and MPN prefixes. The proposal is a hint only: the user or the model confirms it.

`identify.py` also gives:

- `ordering_codes`: the orderable codes of a datasheet. A code starts with a part of
  the document, or fits a family name with placeholders (`TLV755xxP` gives
  `TLV75533PDBVR`).
- `base_note`: a note when a project part is probably a base part and not a concrete
  orderable MPN.

## Distributors and keys

`distributors.py` asks the Mouser Search API (part number search, exact or by the
start of the MPN), and selects the distributors and their keys. It uses only the
standard library (`urllib`). It keeps each answer in memory for one hour. An HTTP
error never shows the request URL, because the URL holds the key. `links.py` gives
the search links of `part_links`.

`digikey.py` asks the DigiKey Product Information API v4: the parametric search, the
offers of an MPN and the direct links to the CAD models. It gets a token with the
client credentials flow of OAuth2. It keeps the DigiKey data only in memory, because
the terms of DigiKey do not permit a local database of DigiKey data. `parametric.py` holds the filter
model of the parametric search (`NAME OP VALUE`), for each source.

`jlcpcb.py` downloads the database of the jlcparts project (a split zip of one SQLite
file) on a command of the user. It reads the parts with a stream: zlib unpacks the
data, and the tool checks the CRC-32 and the size. Then it adds its indexes and puts
the file in place of the old one. The searches open the database read-only.

`keys.py` reads a key (the API key of a distributor, or the token of the team server)
from the environment variable, the key store of the system (the package keyring) or
the file `keys.toml`. A key never goes into a message, a log
or the database.

## Module map

| Module | Function |
|---|---|
| `__init__.py` | The version and the project URL. |
| `adopt.py` | Take a found part: the datasheet, the part and the purchase source. |
| `bundles.py` | The bundles of derived data (index and element map) on the team server. |
| `version_check.py` | The update check: a newer release on PyPI, and a server that runs an older version than the installed one. |
| `agents.py` | The datasheet-lens block in `AGENTS.md` and `CLAUDE.md`. |
| `cli.py` | The command line interface. |
| `config.py` | The location of the central store (`DATASHEET_LENS_HOME`), `config.json`, and the data folder (key `data`). |
| `distributors.py` | The part data from the distributor APIs: search, availability, distributor list, services. |
| `digikey.py` | The DigiKey API: parametric search, offers, direct links. |
| `jlcpcb.py` | The JLCPCB parts library: download of the split zip, parametric search, offers, replacements. |
| `elements.py` | The element map: docling and text backends, page map, caption search. |
| `errors.py` | `LensError`: a wrong input or a state that the user can repair. `Unavailable`: a distributor API that cannot answer now. |
| `fetch.py` | The download of a PDF from a URL. |
| `fileutil.py` | Atomic file writes. |
| `identify.py` | The MPN proposal, the orderable codes and the base-part note. |
| `indexer.py` | The index: page text, outline, captions, page quality, text search. |
| `keys.py` | The API keys: environment, key store, key file. |
| `knowledge.py` | The facts: save, find, state. |
| `links.py` | The search links for a part. |
| `lock.py` | The lock of a project folder between processes and threads. |
| `makers.py` | The manufacturer registry: the names of each manufacturer, the built-in names. |
| `pages.py` | Page images, region images and PDF extracts. |
| `parametric.py` | The filter model of the parametric search, for each source. |
| `pdflock.py` | One lock for pdfium in a process. |
| `pdftext.py` | Text lines with positions from the PDF text layer. |
| `project.py` | The project file, sync, removal, CAD check. |
| `server.py` | The MCP server and its tools. |
| `setup.py` | The command `init`, the configuration and the docling installation. |
| `store.py` | The central store: database, files, parts, documents, purchase sources. |
| `teamsync.py` | The client of the team server: push of the outbox, pull of the changes. |
| `team/` | The team server: API (`app.py`), synchronization (`sync.py`), database, object store, accounts, setup wizard. |
