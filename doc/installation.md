# Installation

This document tells how to install, configure, upgrade and remove datasheet-lens.

## Requirements

- Python 3.11 or newer.
- Linux, macOS or Windows.
- [pipx](https://pipx.pypa.io/) to install the tool in its own virtual environment.
  You can also use pip or uv.
- Optional: some GB of free disk for docling. See
  [The docling component](#the-docling-component).

The installation gives two commands:

| Command | Function |
|---|---|
| `datasheet-lens` | The command line interface (CLI). See [cli.md](cli.md). |
| `datasheet-lens-mcp` | The MCP server (stdio). See [mcp-server.md](mcp-server.md). |

## Install the tool

1. Install the package from PyPI:

   ```sh
   pipx install datasheet-lens
   ```

   To install the code that is not released yet, install from the GitHub
   repository: `pipx install git+https://github.com/turbyho/datasheet-lens.git`.

2. Make the central store, and select the element map backend:

   ```sh
   datasheet-lens init
   ```

3. Make sure that the installation is correct:

   ```sh
   datasheet-lens info
   ```

   The command shows the version and the location of the central store.

To use the tool with an LLM agent, register the MCP server. See
[mcp-server.md](mcp-server.md).

## The init command

`datasheet-lens init` does these steps:

1. It makes the folder of the central store, if the folder does not exist.
2. It examines if docling is installed.
3. If docling is installed, it sets the element map backend to `docling`.
4. If docling is not installed, it shows a description of docling and asks:
   `Install docling now? [y/N]`.
5. It writes the selection to `config.json` in the store folder.

You can run `datasheet-lens init` again at any time. Each run asks about docling
again when docling is not installed.

The option `--layout` gives the answer before the question:

| Option | Result |
|---|---|
| `--layout ask` | Ask the question (default). |
| `--layout yes` | Install docling with no question. |
| `--layout no` | Do not install docling. The element map uses the text layer. |

When the input is not an interactive terminal, `init` does not ask and does not
install docling. Use `--layout yes` in a script.

## The docling component

The element map gives the regions of the headings, tables, figures and formulas of
each page. An agent uses these regions to ask for a small image of one table, not for
a full page. datasheet-lens has two backends for the element map:

| Backend | Content of the element map | Cost |
|---|---|---|
| `docling` | Headings, paragraphs, tables, figures, formulas and captions, each with its region. The docling layout model finds them. | docling installs PyTorch and models (some GB of disk). It runs on a CPU or a GPU (CUDA, Apple MPS). |
| `text` | Headings, text blocks and captions from the PDF text layer. No regions of tables and figures. | No extra package. |

The tool operates with the two backends. With the `text` backend, the agent must read
more full pages, because the element map does not give the region of a table.

To install docling, `datasheet-lens init --layout yes` runs pip in the Python
environment of the tool (`docling>=2.144`). When that environment has no pip, it uses
uv. When it finds neither, it shows a command that you must run yourself.

With pipx, you can also add docling to the environment of the tool yourself, then run
`init` again to select the backend:

```sh
pipx inject datasheet-lens "docling>=2.144"
datasheet-lens init
```

## The central store

The central store holds the documents, their index, their element map and the saved
facts of all projects of a user on one machine.

The store is shared by all projects of the user:

- A document is in the store one time, also when more projects use it. The store
  identifies it by its SHA-256.
- A datasheet that one project added is at once available to each other project,
  with its index and its element map. It is not downloaded or indexed again.
- The parts, their variants and base parts, and their links to the documents are
  in the store. A second project that uses the same part gets the same documents.
- The facts (the values that a session read) are in the store. A session in a
  different project finds them with `find_facts`, and does not read the page again.
- A project keeps only its list of parts and its copies of the documents (see
  [projects.md](projects.md)).

The store is not shared between persons: each person has a store. The repository of
a design gives the documents to the team (see
[projects.md](projects.md#team-workflow)).

The default location is
`~/.datasheet-lens`. To use a different folder, set the environment variable
`DATASHEET_LENS_HOME`:

```sh
export DATASHEET_LENS_HOME=/data/datasheet-lens
```

Set the variable for the CLI and for the MCP server. If the two use different
folders, they see different documents.

The store folder holds these files:

| Path | Content |
|---|---|
| `db/lens.db` | The SQLite database: documents, parts, index, element map and facts. |
| `blobs/` | One file for each document, with its SHA-256 as the file name. |
| `render/` | The rendered page images and PDF extracts (a cache). |
| `tmp/` | Temporary files of the downloads. |
| `config.json` | The configuration: the element map backend and the global list of distributors. |
| `keys.toml` | The API keys, only on a system with no key store. See [distributors.md](distributors.md). |

The tool makes a file in `render/` again when the file is not there.

## Upgrade

1. Upgrade the package:

   ```sh
   pipx upgrade datasheet-lens
   ```

   If you installed the package from the GitHub repository, install it again:

   ```sh
   pipx install --force git+https://github.com/turbyho/datasheet-lens.git
   ```

2. Restart the MCP client, so that it starts the new MCP server.

Each index and each element map records the version of its format, not the version
of the package. A new release builds them again only when it changes the format;
its release notes then tell it. To build them again by hand:

```sh
datasheet-lens index --force
datasheet-lens elements --force
```

The first start of a new version updates the database schema of the store. An older
version of the tool cannot open a store that a newer version updated. It stops with
an error that tells you to install a newer version.

### Update check

datasheet-lens tells you when PyPI has a newer release, and when the MCP server runs
an older version than the installed one:

- The MCP server asks PyPI in a background thread, at most one time in 24 hours,
  and keeps the answer in `update_check.json` in the store folder. The tools never
  wait for the network.
- `project_status`, `project_check` and `distributor_status` (MCP) show a line
  when a newer release is available, or when the LLM client must restart after an
  upgrade. The server gives each line one time while it runs. The agent tells
  you; it does not upgrade the tool itself.
- `datasheet-lens info` and `datasheet-lens ls` (CLI) show a newer release with
  the upgrade command.
- `datasheet-lens info` asks PyPI each time, and waits a few seconds at most.
- An editable install (`pip install -e`) gets no check.

To stop the check, set the environment variable `DATASHEET_LENS_NO_UPDATE_CHECK=1`,
or add `"update_check": false` to `config.json` in the store folder.

## Uninstall

1. Remove the saved API keys (see [distributors.md](distributors.md)):

   ```sh
   datasheet-lens key mouser --delete
   ```

2. Remove the package:

   ```sh
   pipx uninstall datasheet-lens
   ```

3. Remove the MCP server from your MCP client. For Claude Code:

   ```sh
   claude mcp remove --scope user datasheet-lens
   ```

4. If you do not want the documents and the facts, delete the store folder
   (`~/.datasheet-lens` or the folder in `DATASHEET_LENS_HOME`).

The project files and the document copies in your design repositories stay.

## Check the installation

`datasheet-lens info` shows the version and the store folder:

```text
datasheet-lens 0.1.0.dev0
store: /home/user/.datasheet-lens
```

`datasheet-lens --version` shows only the version. `datasheet-lens dist` shows the
distributors and their API keys. `datasheet-lens parts` and `datasheet-lens docs`
show the content of the store.
