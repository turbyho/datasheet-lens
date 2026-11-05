# Development

This document tells how to prepare a development environment, run the tests, and
write code and text for datasheet-lens.

## Prepare the environment

1. Clone the repository:

   ```sh
   git clone https://github.com/turbyho/datasheet-lens.git
   cd datasheet-lens
   ```

2. Make a virtual environment:

   ```sh
   python3 -m venv .venv
   ```

3. Install the package in editable mode, with the test dependencies:

   ```sh
   .venv/bin/pip install -e ".[dev]"
   ```

4. Optional: install docling for the element map tests with the `docling` backend:

   ```sh
   .venv/bin/pip install -e ".[dev,layout]"
   ```

The extra `layout` installs docling, PyTorch and models (some GB of disk). Without it,
the tests of the `docling` backend skip.

The commands `.venv/bin/datasheet-lens` and `.venv/bin/datasheet-lens-mcp` then run
the code of the working tree.

## Dependencies

| Package | Use |
|---|---|
| `pypdfium2` | PDF text layer, bookmarks, rendering and page extracts. |
| `mcp` | The MCP server. |
| `tomlkit` | Writes the project file and keeps the text of the user. |
| `keyring` | The API keys in the key store of the system. |
| `docling` (extra `layout`) | The layout model of the element map. |
| `fastapi`, `uvicorn`, `asyncpg`, `boto3` (extra `server`) | The team server: API, PostgreSQL, S3 object store. |
| `pytest` (extra `dev`) | The tests. |

The PNG encoder and the HTTP client (also the client of the team server) use only the
standard library.

## Run the tests

```sh
.venv/bin/python -m pytest -q
```

To find files and database connections that the code does not close, make a
`ResourceWarning` an error:

```sh
.venv/bin/python -m pytest -q -W error::ResourceWarning
```

Each test runs in an empty folder, with an empty store in that folder
(`DATASHEET_LENS_HOME`). The tests remove the API key variables and disable the key
store. Thus a test cannot change the store, the keys or the projects of the user. The
tests make their own small PDF files.

### Regression tests

`tests/test_regression.py` uses real datasheets of third parties. These files are not
in the public repository. A test skips when its file is not present.

The slow tests run only when `DATASHEET_LENS_SLOW=1`:

```sh
DATASHEET_LENS_SLOW=1 .venv/bin/python -m pytest -q tests/test_regression.py
```

They read a large reference manual (more than 1000 pages), and build an element map
with docling (docling must be installed).

### Tests of the team server

The tests of the team server and of its client use a real PostgreSQL and a real S3
object store (Garage). They run only with these environment variables; else pytest
skips them:

| Variable | Content |
|---|---|
| `DSLENS_TEST_PG` | `postgresql://user@host:port` with no database: each test run makes its own database and removes it. The user needs `CREATEDB`. |
| `DSLENS_TEST_S3` | The endpoint of the object store, for example `http://127.0.0.1:3900`. |
| `DSLENS_TEST_S3_KEY_ID`, `DSLENS_TEST_S3_SECRET` | An access key with read, write and owner on the bucket. |
| `DSLENS_TEST_S3_BUCKET` | The bucket (default `dslens-test`). |

A local Garage for the tests: the Docker image `dxflrs/garage`, one node, and a bucket
with a key (`garage bucket create`, `garage key create`, `garage bucket allow`).

## Code style

- The library modules hold the functions. `cli.py` and `server.py` only read the
  arguments and format the results. Add a new function to the library first, then to
  the two interfaces.
- A wrong input, or a state that the user can repair, raises `LensError` with one line
  of text. The CLI shows it with no traceback (exit code 2). The MCP server gives it to
  the model as a tool error. The text tells what to do next.
- pdfium is not thread-safe. Each function that opens a PDF holds the lock of
  `pdflock.py` (`pdfium_locked`) until it closes the PDF.
- Write the files of the store and of a user project with the atomic functions of
  `fileutil.py`.
- Close each `Store`. Use `with Store() as store:`.

## Text style

All English texts use ASD-STE100 Simplified Technical English: docstrings, comments,
CLI help, error messages, tool descriptions, the server instructions, documentation
and commit messages. Some rules:

- One fact or one instruction in each sentence. Use the active voice.
- Use one name for one concept in all texts (for example "base part", "concrete
  orderable MPN", "central store").
- No Latin abbreviations and no phrasal verbs.

The tool descriptions and the parameter descriptions of `server.py` go to the model.
Write them for a model that knows nothing about the tool: what the tool does, when to
use it, and what it gives. A test makes sure that each tool and each parameter has a
description.

## CLI commands

Prefer short commands and short options. A long form is an alias. For example, the
project commands have short forms (`ls`, `sync`, `set`, `rm`, `check`) and long forms
under `project` (`project status`, `project sync`). The option `-p` has the aliases
`--path` and `--project`.

## Length of the MCP server instructions

Some MCP clients cut the server instructions. In a test, Claude Code cut a text of
4162 characters after character 4091. The test
`test_instructions_fit_the_client_limit` in `tests/test_server.py` keeps `INSTRUCTIONS`
below 3800 characters. Put the details into the tool descriptions, not into the
instructions.

## The block for agents

`agents.py` holds the block that `project init` writes into `AGENTS.md` and
`CLAUDE.md`. When you change its text, the next `project init` in a user project
updates the block.

## Store schema

When you change the database schema of `store.py`:

1. Change `SCHEMA` for a new store.
2. Increase `SCHEMA_VERSION`.
3. Add the change of an older store to `MIGRATIONS`, with the new version as the key:
   one SQL statement, or a function for more steps (`_add_columns` adds the columns
   that a table does not have).
4. A new column of a shared table (`parts`, `documents`, `part_documents`,
   `part_aliases`, `knowledge`, `maker_relations`): add it to `_ROW_JSON` when the
   team server must have it, and to `team/sync.py` (`SCHEMA`, `VALUES`, `TYPES`). The
   triggers come from `SYNC_STATEMENTS`: a change of them gives a new `SYNC_VERSION`,
   and each store makes its triggers again.

When a change of the code changes the index or the element map, increase its format
version (`indexer.INDEX_FORMAT`, `elements.ELEMENTS_FORMAT`): a store builds a map of
an older format again (`datasheet-lens index`, `datasheet-lens elements`), and a team
server keeps a bundle for each format. A new release with the same format builds
nothing again.
