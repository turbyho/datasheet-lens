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
   (indexer, elements, pages, knowledge, identify, project, distributors)
                          |
             central store (store.py)
       SQLite database + content-addressed files
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
download the file and put it into the document folder of the project.

## Central store

The store is the knowledge of one user for all design projects. A document, its
index, its element map, its parts and its facts are in the store one time. Each
project uses them: a datasheet that one project added, and the values that a
session read from it, are at once available in each other project. A project file
keeps only the list of the parts of the design and the references (SHA-256) to the
documents.

`store.py` keeps the store in one folder (`config.py`: `~/.datasheet-lens`, or the
environment variable `DATASHEET_LENS_HOME`):

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
| `documents` | SHA-256, file name, size, pages, title, source URL. |
| `part_documents` | The links between parts and documents, with the role. |
| `builds` | The format version (`indexer.INDEX_FORMAT`, `elements.ELEMENTS_FORMAT`) and the backend of each index and element map. A new release of the package does not build them again; a new format does. |
| `pages`, `outline`, `captions` | The index. |
| `elements` | The element map. |
| `knowledge` | The facts. |

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
the values that changed. Before a write, it parses the new text and compares it with
the data. When the file changed after the read, it writes nothing. All writes are
atomic (`fileutil.py`): a temporary file, then a replace.

`sync` compares the project with the store and the document folder. It makes the
copies, moves them to the subfolders of the layout, and imports the new files. The
central store is the source of the copies. The project file is the source of the base
parts: on a new machine, sync writes them into the store.

`project_check` compares the project with the CAD parts. A CAD MPN and a project part
are "different" (probably one part) when one MPN is the base part or a variant of
the other, or one MPN starts with the other. Placeholders at the end of a CAD MPN
(`nRF9151-LAXX`, `STM32F103C8Tx`) match any characters.

## Identification of the part

`identify.py` proposes the MPN and the manufacturer of a new document. The sources
are the title in the PDF metadata, the text of page 1 and the file name. Each MPN
candidate gets a score: 2 for the title, 1 for page 1, 1 for the file name. Rules
remove tokens that look like an MPN but are not one: revisions, document numbers,
values with units, packages and standards. The manufacturer comes from known names
and MPN prefixes. The proposal is a hint only: the user or the model confirms it.

`identify.py` also gives:

- `ordering_codes`: the orderable codes of a datasheet. A code starts with a part of
  the document, or fits a family name with placeholders (`TLV755xxP` gives
  `TLV75533PDBVR`).
- `base_note`: a note when a project part is probably a base part and not a concrete
  orderable MPN.

## Distributors and keys

`distributors.py` asks the Mouser Search API (part number search, exact or by the
start of the MPN). It uses only the standard library (`urllib`). It keeps each answer
for one hour. An HTTP error never shows the request URL, because the URL holds the
key. `links.py` gives the search links of `part_links`.

`keys.py` reads a key from the environment variable, the key store of the system
(the package keyring) or the file `keys.toml`. A key never goes into a message, a log
or the database.

## Module map

| Module | Function |
|---|---|
| `__init__.py` | The version and the project URL. |
| `version_check.py` | The update check: a newer release on PyPI, and a server that runs an older version than the installed one. |
| `agents.py` | The datasheet-lens block in `AGENTS.md` and `CLAUDE.md`. |
| `cli.py` | The command line interface. |
| `config.py` | The location of the central store (`DATASHEET_LENS_HOME`). |
| `distributors.py` | The part data from the distributor APIs: search, availability, distributor list. |
| `elements.py` | The element map: docling and text backends, page map, caption search. |
| `errors.py` | `LensError`: a wrong input or a state that the user can repair. |
| `fetch.py` | The download of a PDF from a URL. |
| `fileutil.py` | Atomic file writes. |
| `identify.py` | The MPN proposal, the orderable codes and the base-part note. |
| `indexer.py` | The index: page text, outline, captions, page quality, text search. |
| `keys.py` | The API keys: environment, key store, key file. |
| `knowledge.py` | The facts: save, find, state. |
| `links.py` | The search links for a part. |
| `pages.py` | Page images, region images and PDF extracts. |
| `pdflock.py` | One lock for pdfium in a process. |
| `pdftext.py` | Text lines with positions from the PDF text layer. |
| `project.py` | The project file, sync, removal, CAD check. |
| `server.py` | The MCP server and its tools. |
| `setup.py` | The command `init`, the configuration and the docling installation. |
| `store.py` | The central store: database, files, parts, documents. |
