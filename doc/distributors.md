# Distributors

datasheet-lens can ask a distributor for the data of a part through its official
API. This document tells what the data gives, how to set the API key, and how to
select the distributors.

Supported now: Mouser. Not supported yet: DigiKey, Farnell and TME.

## What the distributor data gives

A datasheet tells what a part does. It does not tell if you can buy the part. Some
datasheets give only a scheme of their orderable codes, not a list. A code that the
scheme permits is not always made or sold. The distributor data gives, for each
concrete orderable MPN:

- The manufacturer and a description.
- The lifecycle state, for example `New Product` or `Obsolete`, and a suggested
  replacement.
- The stock, the availability and the lead time.
- The prices for different quantities.
- The package and the packing.
- The URL of the datasheet and of the product page.

A part that is obsolete, discontinued or at its end of life gets the mark
`NOT ORDERABLE`.

## Personal API keys

Each user needs a personal API key for each distributor. The terms of some
distributors do not permit one key for a team on a server. Thus datasheet-lens has no
shared key, and each person saves a personal key on each machine.

### Get a Mouser key

1. Open the [Mouser API Hub](https://www.mouser.com/api-hub/). You need a Mouser
   account.
2. Ask for a key for the Search API. The key is free.
3. Wait until Mouser approves the key. Before the approval, the API gives an error.

### Save a key

```sh
datasheet-lens key mouser
```

The command asks for the key. It does not show the key, and the key does not go into
the shell history. When the input is not a terminal, the command reads the key from
the first line of the input.

The command saves the key in the key store of the system:

- Windows Credential Manager on Windows.
- Keychain on macOS.
- Secret Service on Linux.

On a system with no key store (for example a server with no desktop, or a
container), it saves the key in the file `keys.toml` in the store folder. Only the
user can read and write this file (mode 0600).

### The order of the key sources

datasheet-lens gets a key from the first source that has it:

1. The environment variable `DATASHEET_LENS_MOUSER_KEY`. Use it for CI.
2. The key store of the system.
3. The file `keys.toml` in the store folder.

When the environment variable is set, the tool does not use a saved key.
`datasheet-lens key mouser` tells you about this. When the key store does not answer
in 10 seconds (for example a locked key store), the tool continues with the file.

The tool never writes a key into a message, a log or the database.

### Show and remove keys

```sh
datasheet-lens key                    # where each key is, with no value
datasheet-lens key mouser --delete    # remove the saved key
```

`--delete` removes the key from the key store and from the file. It does not remove
the environment variable. When a key is still available from a different source,
the command tells you where.

## Select the distributors

A user in one region does not want the data of a distributor of a different region.
Thus you can select the distributors that datasheet-lens asks:

| Command | Effect |
|---|---|
| `datasheet-lens dist` | Show the enabled distributors, where the list comes from, and the state of each key. |
| `datasheet-lens dist mouser` | Set the global list (in `config.json` of the store folder). |
| `datasheet-lens dist --local mouser` | Set the list in the project file (key `distributors`). |
| `datasheet-lens dist --local` | Remove the list from the project file. The project then uses the global list. |

The tool uses the first list that it finds:

1. The key `distributors` of the project file.
2. The global list in `config.json`.
3. The default: `mouser`.

A name that the tool does not support gives an error. A name in a list that a newer
version wrote is ignored, with a note.

## Find a part

```sh
datasheet-lens find TLV755P          # each MPN that starts with TLV755P (up to 50)
datasheet-lens find -x TLV75533PDBVR # only this MPN
```

The MCP tool is `search_distributors(mpn, exact)`. The result gives one line for each
orderable part, with its source. Use it to select the concrete MPN of the BOM, and to
make sure that it can be ordered. The start of an MPN must have 3 characters or more.

## Check the project parts

```sh
datasheet-lens stock
```

The MCP tool is `project_availability`. The result gives one line for each project
part with its lifecycle state and its stock. It gives a `WARNING` for a part that:

- Is not orderable (obsolete or discontinued), with the replacement.
- The distributor does not have (a base part, or a wrong MPN).
- Has no stock, with the availability and the lead time.

One MPN can have more offers (for example cut tape and reel). The part is orderable
when one offer is orderable, and its stock is the sum of the offers.

When the API cannot answer (no connection, rate limit, server error), the tool stops
and lists the parts that it did not check. The tool keeps each answer for one hour,
and does not send the same request again in that time.

## Distributor status

The MCP tool `distributor_status` gives the same data as `datasheet-lens dist`: the
enabled distributors and if each one has a key. It gives no key values.

## Notes for a missing key

When an enabled distributor has no key, the CLI commands and the MCP tools give a
note. The note tells the user these steps:

1. Get a key at the API Hub of the distributor.
2. Run `datasheet-lens key mouser` in a terminal, and paste the key.
3. For CI, set the environment variable `DATASHEET_LENS_MOUSER_KEY`.
4. Or disable the distributor (`datasheet-lens dist`).

The MCP server cannot ask for a key. The agent gives the note to the user. With no
key for any distributor, `find` and `search_distributors` give the search links of
`part_links` in place of the data.

When Mouser does not accept the key (the key is not correct, or Mouser did not
approve it yet), the error tells the user to examine the key at the API Hub and save
it again.

## Search links

```sh
datasheet-lens links TPS7A0233PYCHR
```

The MCP tool is `part_links(mpn)`. It gives one search link for each site:

| Group | Sites |
|---|---|
| Datasheet, price, stock | DigiKey, Mouser, Farnell, LCSC, Octopart, Findchips |
| Symbol, footprint, 3D model | Component Search Engine, SnapMagic, Ultra Librarian |

datasheet-lens does not open these links. A person or an agent with a web tool opens
them. The sites for the CAD files need a free account for the download. For a
datasheet that you find, run `datasheet-lens add URL`, or call `add_document` with
the URL.

## Terms of use

- datasheet-lens uses only the official APIs of the distributors. It does not read
  their web pages (no scraping).
- The terms of Mouser ask to name Mouser as the source of its data, and not to mix it
  with other data with no distinction. Thus each result names its source, for
  example `Source: Mouser`.
- Each user uses a personal key under the terms of the distributor.
