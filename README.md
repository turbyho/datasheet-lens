<!-- mcp-name: io.github.turbyho/datasheet-lens -->

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
  element, a region, a caption ("Table 57", with docling also when the table
  continues on more pages) or a full page at 300 to 600 dpi.
- **Central store, shared by all projects.** Each user has one store (default
  `~/.datasheet-lens`) for all design projects. Each PDF is stored one time
  (SHA-256), with its index, its element map, its parts and the facts that sessions
  read from it. A datasheet that one project added is at once available to each
  other project: it is not downloaded, indexed or read again. A part can have a
  datasheet, a manual and errata.
- **Facts.** A value that a session read is saved with its source (document, page,
  element), so that a later session finds it.
- **Project file.** `datasheet-lens.toml` in the repository of a design holds the
  parts of the design and their documents. The team gets it with the repository.
  `project_check` compares it with the parts of the CAD project.
- **Orderable parts.** The ordering codes of a datasheet, and the lifecycle state,
  stock and prices from the official API of a distributor (Mouser now). No
  scraping.

## Typical uses

datasheet-lens helps to manage the input documents of a design project, and to keep
them in the repository. It keeps the BOM equal to the CAD project. It lets the agent
check the BOM, and check the design against the original datasheets.

- Keep the datasheets of a design complete and in git, the same for each person of
  the team.
- Compare the project with the parts of the CAD project: missing, extra and
  different parts.
- Select the concrete orderable MPN of a base part, and make sure that it can be
  ordered.
- Review a schematic against the datasheets: pins, absolute maximum ratings, logic
  levels, application circuits.
- Compare a footprint with the package drawing of the datasheet.
- Read register maps, bit fields and timing values for firmware, and keep them as
  facts with their source.
- Use the same parts in more projects: the datasheets and the facts of one project
  are at once available in each other project of the same user.

datasheet-lens does not read the schematic itself. The parts come from the CAD tool,
its MCP server or a BOM file. See [doc/use-cases.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/use-cases.md) for the steps
of each use.

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

Register the MCP server (stdio) in Claude Code:

```sh
claude mcp add --scope user datasheet-lens -- datasheet-lens-mcp
```

For a different MCP client, set the command `datasheet-lens-mcp` with no
arguments. The server gives its instructions to the agent. See
[doc/installation.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/installation.md) and [doc/mcp-server.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/mcp-server.md).

## Quick start

In the root folder of the repository of the design:

```sh
datasheet-lens project init     # the project file, and a block in AGENTS.md, CLAUDE.md
datasheet-lens add PATH|URL     # add a datasheet; it proposes the MPN of the part
datasheet-lens sync             # copy the documents into the project folder
datasheet-lens check BOM.csv    # compare the project with a BOM of the CAD project
```

The agent uses the same functions through the MCP server. The block in
`AGENTS.md` or `CLAUDE.md` tells the agent the procedure. `project init` changes
only the files that exist: make one of them first if you want the block.

| Command | What it does |
|---|---|
| `add PATH\|URL` | Add a document to the store and to the project |
| `ls`, `sync` | Show the project, or synchronize its copies |
| `check BOM.csv` | Compare the project with a BOM (CSV) of the CAD project, and find conflicts between the MPN and Value fields (tested only with BOM exports of KiCad and Eagle) |
| `set MPN -m NEW` | Change the MPN, base part, category or manufacturer of a project part |
| `rm MPN` | Remove a part (or `-d DOC`, a document) from the project |
| `search TEXT --part MPN` | Find a text in the documents of a part |
| `codes DOC` | Show the orderable codes of a datasheet |
| `links MPN` | Show search links for a datasheet, stock, a symbol, a footprint, a 3D model |
| `find MPN` | Find a part at the distributors (lifecycle, stock, prices) |
| `stock` | Show the lifecycle state and the stock of each project part |
| `key NAME` | Save the API key of a distributor |
| `dist NAME ...` | Select the distributors (globally, or `--local` for the project) |

All commands and options are in [doc/cli.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/cli.md).

Each user has a personal API key for each distributor. Get a free Mouser key at
the [Mouser API Hub](https://www.mouser.com/api-hub/) (Search API), then run
`datasheet-lens key mouser`. See [doc/distributors.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/distributors.md).

## Documentation

| Document | Content |
|---|---|
| [doc/installation.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/installation.md) | Requirements, installation, `init`, docling, store location, upgrade, uninstall |
| [doc/use-cases.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/use-cases.md) | Typical uses, step by step |
| [doc/mcp-server.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/mcp-server.md) | MCP server: registration, instructions, all tools |
| [doc/agent-workflow.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/agent-workflow.md) | How an agent reads a datasheet, with an example |
| [doc/projects.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/projects.md) | Project file, layout, sync, team workflow, consistency with the CAD project |
| [doc/cli.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/cli.md) | Reference of all CLI commands and options |
| [doc/distributors.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/distributors.md) | Distributor data, API keys, distributor selection, search links |
| [doc/architecture.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/architecture.md) | For developers: design decisions and module map |
| [doc/development.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/development.md) | Development setup, tests, code and text style |

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

See [doc/development.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/development.md).

## License

MIT. See `LICENSE`.
