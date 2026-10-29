# datasheet-lens

datasheet-lens gives the content of component datasheets (PDF) to an LLM agent,
for electronics design (KiCad and other CAD tools) and firmware work.

The tool does not convert a datasheet to text. A conversion makes errors that look
correct: lost superscripts, merged table rows, shifted columns, wrong values from
graphs. datasheet-lens builds an index to find the content, and gives the original
pages and regions to the model on demand. The model reads only the parts that the
work needs, and reads each value from the original.

Status: in development (version 0.1). The tools work and have tests. The
interfaces can change.

## Features

- **Drill-down into a document.** An index (outline, table and figure captions,
  page quality), a text search, a page map with element IDs, and images of an
  element, a region, a caption ("Table 57", also when the table continues on more
  pages) or a full page at 300 to 600 dpi.
- **Central store.** Each PDF is stored one time (SHA-256), with its index and its
  parts. A part can have a datasheet, a manual and errata.
- **Facts.** A value that a session read is saved with its source (document, page,
  element), so that a later session finds it.
- **Project file.** `datasheet-lens.toml` in the repository of a design holds the
  parts of the design and their documents. The team gets it with the repository.
  `project_check` compares it with the parts of the CAD project.
- **Orderable parts.** The ordering codes of a datasheet, and the lifecycle state,
  stock and prices from the official API of a distributor (Mouser now). No
  scraping.

## Installation

datasheet-lens needs Python 3.11 or newer. It works on Linux, macOS and Windows.

```sh
pipx install git+https://github.com/turbyho/datasheet-lens.git
datasheet-lens init
```

`datasheet-lens init` makes the central store (default `~/.datasheet-lens`, or the
folder in the environment variable `DATASHEET_LENS_HOME`). It can also install
docling, which gives a better element map (tables and figures). docling is
optional: it needs some GB of disk. Without it, the tool uses the text layer.

### MCP server

The MCP server is the command `datasheet-lens-mcp` (stdio). For Claude Code:

```sh
claude mcp add --scope user datasheet-lens -- datasheet-lens-mcp
```

For a different MCP client, set the command `datasheet-lens-mcp` with no
arguments. The server gives its instructions to the agent.

## Use in a design project

In the root folder of the repository of the design:

```sh
datasheet-lens project init     # the project file, and a block in AGENTS.md or CLAUDE.md
datasheet-lens add PATH|URL     # add a datasheet; it proposes the MPN of the part
datasheet-lens sync             # copy the documents into the project folder
datasheet-lens check BOM.csv    # compare the project with a BOM of the CAD project
```

The agent uses the same functions through the MCP server. The block in
`AGENTS.md` or `CLAUDE.md` tells the agent the procedure.

### Commands

| Command | What it does |
|---|---|
| `add PATH\|URL` | Add a document to the store and to the project |
| `ls`, `sync` | Show the project, or synchronize its copies |
| `check BOM.csv` | Compare the project with a BOM (CSV) of the CAD project |
| `set MPN -m NEW` | Change the MPN, base part or category of a project part |
| `rm MPN` | Remove a part (or `-d DOC`, a document) from the project |
| `search TEXT --part MPN` | Find a text in the documents of a part |
| `codes DOC` | Show the orderable codes of a datasheet |
| `links MPN` | Show search links for a datasheet, a symbol, a footprint, a 3D model |
| `find MPN` | Find a part at the distributors (lifecycle, stock, prices) |
| `stock` | Show the lifecycle state and the stock of each project part |
| `key NAME` | Save the API key of a distributor |
| `dist NAME ...` | Select the distributors (globally, or `--local` for the project) |

`datasheet-lens --help` and `datasheet-lens COMMAND --help` give all options.

### Distributor API keys

Each user has a personal API key for each distributor. Get a free Mouser key at
the [Mouser API Hub](https://www.mouser.com/api-hub/) (Search API). Then:

```sh
datasheet-lens key mouser       # asks for the key; the key is not shown
```

The key goes into the key store of the system (Windows Credential Manager, macOS
Keychain, Secret Service on Linux). On a system with no key store, it goes into a
file in the store folder that only the user can read. An environment variable
(`DATASHEET_LENS_MOUSER_KEY`) comes first, for CI.

`datasheet-lens dist` shows the enabled distributors and their keys. When an
enabled distributor has no key, the MCP tools tell the agent, and the agent
gives the user these steps.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## License

MIT. See `LICENSE`.
