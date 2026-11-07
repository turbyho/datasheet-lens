<!-- mcp-name: io.github.turbyho/datasheet-lens -->

# datasheet-lens

datasheet-lens is a central store for the datasheets of electronic components. It
keeps each datasheet one time and connects it to the projects that use it. Thus each
person and each AI agent of a project uses the same correct documents.

- **Electronics design.** The project file `datasheet-lens.toml` in the repository
  of a design holds the parts of the design and their datasheets. datasheet-lens
  keeps this list the same as the parts of the CAD project (KiCad and other EDA
  tools).
- **Software and firmware.** The developers and their AI agents get the same
  datasheets as the hardware team: the same document and revision, not a copy from
  an unknown source.
- **One source for the two.** The hardware design and the software can use one
  project file, or each a project file with the same parts. The store identifies
  each PDF by its SHA-256, thus the two projects use the same document.
- **Part selection.** datasheet-lens asks the official APIs of the distributors
  (Mouser, DigiKey) and an optional local copy of the JLCPCB parts library. A
  parametric search finds new parts and replacement parts by
  their parameters. For each part, the tool gives the stock, the prices, the
  lifecycle state and the links to the datasheet, the symbol, the footprint and the
  3D model. The values that the design needs come from the datasheet.

An LLM agent reads the datasheets through an MCP server. The tool does not convert
a datasheet to text. A conversion makes errors that look correct: lost
superscripts, merged table rows, shifted columns, wrong values from graphs.
datasheet-lens builds an index to find the content, and gives the original pages
and regions to the model on demand. The model reads only the parts that the work
needs, and reads each value from the original.

Status: in development (version 0.4). The tools work and have tests. The
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
  datasheet, a manual, errata and a package drawing.
- **Facts.** A value that a session read is saved with its source (document, page,
  element), so that a later session finds it.
- **Project file.** `datasheet-lens.toml` in the repository of a design holds the
  parts of the design and their documents. The team gets it with the repository.
  `project_check` compares it with the parts of the CAD project.
- **Distributors.** The ordering codes of a datasheet, and for each orderable MPN
  the lifecycle state, the stock and the prices from the official APIs of Mouser and
  DigiKey. No scraping. `distributor_status` tells the services of each
  distributor, for example which one has a parametric search.
- **JLCPCB assembly.** An optional local copy of the JLCPCB parts library: the
  JLCPCB stock for the assembly and the library class (Basic, Preferred, Extended) of
  each part. The tool marks a part with no stock `NOT IN STOCK`. For a part with no
  stock, with a loading fee or with a lifecycle risk, it gives replacements with
  stock, Basic and Preferred first (the same MPN; resistors, capacitors and inductors
  with the same parameters). `project_check` compares the JLCPCB (LCSC)
  number of each symbol with the purchase source and with the library.
- **Parametric search.** Find new parts and replacement parts by their parameters
  at DigiKey or in the JLCPCB parts library, for example "N-channel MOSFET, Vdss 20 V to 40 V, gate drive 2.5 V,
  package 3x3". DigiKey keeps one value of a parameter at its own conditions: the
  result marks a filter that its data cannot decide (RDS(on) at a gate voltage of
  2.5 V) `NOT APPLIED`, and the agent reads that value in the datasheet. The JLCPCB
  library lists more points of a value, and the tool applies such a condition to them.
- **Take a found part.** `adopt` takes a part of a search in one step: its datasheet
  (from the site of the manufacturer first), the part, and its purchase source (the
  source and its part number). The availability check then asks for that number.
- **Inbox.** A PDF file that a person downloads goes into the folder `_inbox` of the
  project. `sync` gives it to the project parts that it agrees with.
- **Links to datasheets and CAD files.** For each MPN, the links to the datasheet,
  the symbol, the footprint and the 3D model (Component Search Engine, SnapMagic,
  Ultra Librarian). With DigiKey, also the direct links to the models of the part.
  The user opens the links: datasheet-lens does not download the CAD files.
- **Team server (optional).** One shared store for a team: the documents, parts,
  facts and the index and element map of each document. Each person keeps a local
  store that works also with no connection, and that synchronizes with the server.
  The server (`datasheet-lens[server]`) has accounts with roles and tokens, and a
  setup wizard installs it with PostgreSQL, the Garage object store, nginx and a
  Let's Encrypt certificate.

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
- Find new parts or replacement parts by their parameters, then check the
  candidates against their datasheets.
- Get the links to the datasheet, the symbol, the footprint and the 3D model of a
  part.
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
pipx install datasheet-lens
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
datasheet-lens sync             # copy the documents into the project folder, and give the
                                # PDF files of datasheets/_inbox/ to their parts
datasheet-lens check BOM.csv    # compare the project with a BOM of the CAD project
```

The agent uses the same functions through the MCP server. The block in
`AGENTS.md` or `CLAUDE.md` tells the agent the procedure. `project init` changes
only the files that exist: make one of them first if you want the block.

| Command | What it does |
|---|---|
| `add PATH\|URL` | Add a document to the store and to the project |
| `ls`, `sync` | Show the project, or synchronize its copies |
| `check BOM.csv` | Compare the project with a BOM (CSV) of the CAD project, and find the changes and conflicts of the fields Value, MPN, Value_Confirmed and MPN_Confirmed (tested only with BOM exports of KiCad and Eagle), and the JLCPCB (LCSC) numbers |
| `set MPN -m NEW` | Change the MPN, base part, category or manufacturer of a project part |
| `rm MPN` | Remove a part (or `-d DOC`, a document) from the project |
| `search TEXT --part MPN` | Find a text in the documents of a part |
| `codes DOC` | Show the orderable codes of a datasheet |
| `links MPN` | Show links to the datasheet, stock, symbol, footprint and 3D model (with DigiKey: direct links) |
| `find MPN` | Find a part at the distributors (lifecycle, stock, prices) |
| `pick SOURCE CATEGORY FILTER ...` | Find parts by their parameters at `digikey` or in the `jlcpcb` library, for example `pick digikey "Single FETs, MOSFETs" "vdss >= 20 V"` |
| `adopt SOURCE NUMBER` | Take a found part: its datasheet, the part and its purchase source, for example `adopt jlcpcb C47616` or `adopt digikey --mpn CSD16340Q3` |
| `jlc update` | Download or update the JLCPCB parts library (276 MB); `jlc status`, `jlc remove` |
| `stock` | Show the lifecycle state and the stock of each project part (by its purchase source), and the JLCPCB replacements |
| `key NAME` | Save the API key of a distributor (`key mouser`, `key digikey`), or the token of the team server (`key team`) |
| `server connect URL` | Connect the store to a team server; `server sync`, `server` (state), `server disconnect` |
| `dist NAME ...` | Select the distributors (globally, or `--local` for the project) |

All commands and options are in [doc/cli.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/cli.md).

Each user has a personal API key for each distributor. Get a free Mouser key at
the [Mouser API Hub](https://www.mouser.com/api-hub/) (the Search API key, not the
Cart API key), then run
`datasheet-lens key mouser`. Get a free DigiKey key at
[developer.digikey.com](https://developer.digikey.com). The key is a Client ID and a
Client Secret of a production app. Then run `datasheet-lens key digikey`. If you set
a distributor list before, add DigiKey to it: `datasheet-lens dist mouser digikey`.
See [doc/distributors.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/distributors.md).

## Documentation

| Document | Content |
|---|---|
| [doc/installation.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/installation.md) | Requirements, installation, `init`, docling, store location, upgrade, uninstall |
| [doc/use-cases.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/use-cases.md) | Typical uses, step by step |
| [doc/mcp-server.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/mcp-server.md) | MCP server: registration, instructions, all tools |
| [doc/agent-workflow.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/agent-workflow.md) | How an agent reads a datasheet, with an example |
| [doc/projects.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/projects.md) | Project file, layout, sync, the inbox, purchase sources, team workflow, consistency with the CAD project (also the JLCPCB numbers) |
| [doc/cli.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/cli.md) | Reference of all CLI commands and options |
| [doc/distributors.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/distributors.md) | Distributor data, API keys, distributor selection, parametric search, the JLCPCB parts library, take a found part (adopt), availability and replacements, search links |
| [doc/team-server.md](https://github.com/turbyho/datasheet-lens/blob/main/doc/team-server.md) | Team server: installation, accounts and tokens, connection of a store, synchronization, API |
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
