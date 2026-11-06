# Distributors

datasheet-lens can ask a distributor for the data of a part through its official
API. This document tells what the distributor data gives and how to set the API keys.
It also tells how to select the distributors and how to find parts by their
parameters.

Supported now: Mouser and DigiKey. Not supported yet: Farnell and TME.

## What the distributor data gives

A datasheet tells what a part does. It does not tell if you can buy the part. Some
datasheets give only a scheme of their orderable codes, not a list. A code that the
scheme permits is not always made or sold. The distributor data gives, for each
concrete orderable MPN:

- The manufacturer and a description.
- The lifecycle state, for example `New Product` or `Obsolete`. Mouser also gives a
  suggested replacement.
- The stock. Mouser also gives the availability and the lead time.
- The prices for different quantities.
- The package, from DigiKey only. Mouser gives no package.
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

> CAUTION: Use the key of the **Search API**, not the key of the Cart API (the
> Order API). The API Hub gives a different key for each API. With the Cart key,
> Mouser answers `Invalid unique identifier`, as for a key that is not correct.

### Get a DigiKey key

The DigiKey key has two parts: a Client ID and a Client Secret. They belong to an
application that you make on the DigiKey developer portal. The portal has no
separate button that makes a key.

1. Make an account at [developer.digikey.com](https://developer.digikey.com). You
   can use your DigiKey account.
2. Click **Organizations** and make an organization (for example with your name).
   Only an organization can make a production app.
3. Open the organization. Under **Operations**, click **Production Apps**, then
   **Create Production App**.
4. Type a name, for example `datasheet-lens`.
5. For **OAuth Callback**, type `https://localhost`. datasheet-lens does not use the
   callback, but the form must have a value that starts with `https://`.
6. Select the API product **Product Information V4**, and save the app.
7. In the list of production apps, click the name of the app. Its page shows the
   **Client ID** and the **Client Secret**.

The key is free. The limits are 120 requests each minute and 1000 requests each day.
Do not use a sandbox app: its data is not complete.

### Save a key

```sh
datasheet-lens key mouser
datasheet-lens key digikey    # asks for the Client ID, then for the Client Secret
```

The command asks for the key. It does not show the key, and the key does not go into
the shell history. When the input is not a terminal, the command reads the key from
the first line of the input. For DigiKey, it reads the Client ID from the first line
and the Client Secret from the second line. `datasheet-lens key digikey-id` and
`datasheet-lens key digikey-secret` save one part of the DigiKey key.

If you set a distributor list before (`datasheet-lens dist`), the new distributor is
not in it. The command `key` then tells you the command that adds it, for example
`datasheet-lens dist mouser digikey`.

The command saves the key in the key store of the system:

- Windows Credential Manager on Windows.
- Keychain on macOS.
- Secret Service on Linux.

On a system with no key store (for example a server with no desktop, or a
container), it saves the key in the file `keys.toml` in the store folder. Only the
user can read and write this file (mode 0600).

### The order of the key sources

datasheet-lens gets a key from the first source that has it:

1. The environment variable. Use it for CI:
   - Mouser: `DATASHEET_LENS_MOUSER_KEY`.
   - DigiKey: `DATASHEET_LENS_DIGIKEY_ID` and `DATASHEET_LENS_DIGIKEY_SECRET`.
2. The key store of the system.
3. The file `keys.toml` in the store folder.

When the environment variable is set, the tool does not use a saved key.
`datasheet-lens key NAME` tells you about this. The DigiKey key is complete only
with the two parts. When the key store does not answer
in 10 seconds (for example a locked key store), the tool continues with the file.

The tool never writes a key into a message, a log or the database.

### Show and remove keys

```sh
datasheet-lens key                    # where each key is, with no value (DigiKey: digikey-id, digikey-secret)
datasheet-lens key mouser --delete    # remove the saved key
datasheet-lens key digikey --delete   # remove the two parts of the DigiKey key
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
| `datasheet-lens dist mouser digikey` | Set the global list (in `config.json` of the store folder). |
| `datasheet-lens dist --local mouser digikey` | Set the list in the project file (key `distributors`). |
| `datasheet-lens dist --local` | Remove the list from the project file. The project then uses the global list. |
| `datasheet-lens dist --site CZ --currency CZK` | Set the country and the currency of the prices and the stock. These settings are global: the command does not accept them with `--local`. |

The tool uses the first list that it finds:

1. The key `distributors` of the project file.
2. The global list in `config.json`.
3. The default: each distributor with a key (in an environment variable, the key
   store or the key file). With no key at all: `mouser`, so that you get the note
   with the steps.

A name that the tool does not support gives an error. A name in a list that a newer
version wrote is ignored, with a note.

DigiKey gives the prices and the stock for the country and in the currency of the
settings. The default is `US` and `USD`. Mouser gives them for the country of the
Mouser account, and ignores these settings.

## Find a part

```sh
datasheet-lens find TLV755P          # each MPN that starts with TLV755P (up to 50)
datasheet-lens find -x TLV75533PDBVR # only this MPN
```

The MCP tool is `search_distributors(mpn, exact)`. The result gives one line for each
orderable part, with its source. Use it to select the concrete MPN of the BOM, and to
make sure that it can be ordered. The start of an MPN must have 3 characters or more.

## Find parts by their parameters

The parametric search finds new parts or replacement parts by their parameters. It
uses DigiKey, because Mouser gives no parameters through its API. It needs DigiKey in
the enabled distributors, and the DigiKey key.

```sh
datasheet-lens pick mosfet                     # an error with the categories that fit "mosfet"
datasheet-lens pick 278 -P                     # the parameters of category 278, with examples of values
datasheet-lens pick 278 "channel = N-Channel" "vdss >= 20 V" "drive <= 2.5V" "package has 3x3"
```

The MCP tools are `part_parameters(category)` and `find_parts(category, filters)`.

A category with subcategories, or a name that fits more than one category, gives an
error with a list of the categories. Select one ID from the list.

A filter is `NAME OP VALUE`:

| Part | Values |
|---|---|
| `NAME` | The full name of a parameter, its short name, or a part of the name that only one parameter has. The short names for the FET categories: `vdss`, `rds_on`, `vgs_th`, `id`, `qg`, `drive`, `channel`. For all categories: `package`, `case`, `mounting`. `-P` shows the short names. |
| `OP` | `=`, `<`, `<=`, `>`, `>=`, `has` (the value holds the text). |
| `VALUE` | A number with its unit (`20 V`, `10 mOhm`, `5A`), or a text. `=` and `has` accept more values with `\|` (one of them), for example `package has 3x3\|3.3x3.3`. |

A numeric filter compares the first number of a value. For example, the drive
voltage `2.5V, 8V` gives 2.5 V, the lowest gate voltage that the datasheet specifies.
`4.5mOhm @ 20A, 8V` gives 4.5 mOhm. The text after `@` holds the conditions.

A filter with no value that agrees gives 0 parts and examples of the values.

DigiKey keeps one value of a parameter, at its own conditions. For example, it keeps
RDS(on) at the highest drive voltage only. Thus the tool does not apply a filter
with a condition after `at` or `@` (`rds_on <= 10 mOhm at Vgs=2.5V`). The result
marks it `NOT APPLIED`. Read that value in the datasheet of each part.

The result gives each filter with its status. For each part, it gives:

- The MPN, the manufacturer and the DigiKey number.
- The lifecycle state, the stock and the price for 1 piece.
- The filtered parameters and the package.
- The URLs of the datasheet and of the product page.

| Option | Effect |
|---|---|
| `-a`, `--all` | Also the parts with no stock. |
| `-s`, `--sort` | `stock` (most first, the default) or `price` (lowest first). |
| `-n`, `--limit` | The number of parts, 1 to 50 (default 20). |
| `-o`, `--offset` | Skip this number of parts (the next page). |
| `-k`, `--keywords` | Words that the parts must have. |

The values come from the distributor. They can be different from the datasheet. Read
each value that the design needs in the datasheet, and save it with `save_fact`.

DigiKey ends some large answers early. The tool then sends the request again. It
sends a request 3 times at most.

## Check the project parts

```sh
datasheet-lens stock
```

The MCP tool is `project_availability`. The result has one section for each enabled
distributor with a key (`Source: Mouser`, `Source: DigiKey`). Each section gives one
line for each project part with its lifecycle state and its stock. It gives a
`WARNING` for a part that:

- Is not orderable (obsolete or discontinued), with the replacement (Mouser).
- The distributor does not have (a base part, or a wrong MPN).
- Has no stock, with the availability and the lead time (Mouser).
- Has the Mouser state `Restricted Availability` or the DigiKey state
  `Discontinued at Digi-Key`: the distributor does not sell the part now. This is not
  the lifecycle state of the manufacturer. Ask the manufacturer or a different
  distributor.
- Has no offer from the manufacturer of the part, when the MPN has offers of two or
  more manufacturers: the line then gives the data of a different manufacturer.

Mouser gives a lifecycle state only for some parts (for example `New Product`,
`End of Life`, `Obsolete`). A line with no state is a part with no such state.
Mouser gives the lead time in the language of the account. The tool shows it in
English (`182 days`), and does not show a lead time of 0.

One MPN can have more offers (for example cut tape and reel). The part is orderable
when one offer is orderable, and its stock is the sum of the offers.

Two manufacturers can make a part with the same MPN (a second source, for example
`LM358DR` of TI and ROHM). The tool then uses only the offers of the manufacturer of
the project part. It compares the names through the manufacturer registry (see
`datasheet-lens maker` in [cli.md](cli.md)). Each distributor writes the names in its
own form. A distributor name does not change the registry.

When the API of a distributor cannot answer (no connection, rate limit, server
error), the tool stops for that distributor and lists the parts that it did not
check. The other distributors continue. The tool keeps each answer in memory for one
hour, and does not send the same request again in that time.

## Distributor status

The MCP tool `distributor_status` gives the same data as `datasheet-lens dist`: the
enabled distributors and if each one has a key. It gives no key values. It also
gives the services of each supported distributor, so that an agent can select the
distributor for a task:

| Distributor | Services | Not available |
|---|---|---|
| Mouser | MPN search: stock, prices, lifecycle state, suggested replacement of an obsolete part, datasheet URL | Parametric search, package |
| DigiKey | Parametric search. MPN search: stock, prices, lifecycle state, package, datasheet URL. Direct links to the CAD models and the datasheets | - |

## Notes for a missing key

When an enabled distributor has no key, the CLI commands and the MCP tools give a
note. The note tells the user these steps:

1. Get a key at the API portal of the distributor.
2. Run `datasheet-lens key mouser` or `datasheet-lens key digikey` in a terminal, and
   paste the key.
3. For CI, set the environment variables (see "The order of the key sources").
4. Or disable the distributor (`datasheet-lens dist`).

The MCP server cannot ask for a key. The agent gives the note to the user. With no
key for any distributor, `find` and `search_distributors` give the search links of
`part_links` in place of the data.

When Mouser does not accept the key (`Invalid unique identifier`), the error tells
the user to examine the key at the API Hub and save it again. There are three
frequent causes:

- The key is the key of the Cart API, not of the Search API.
- Mouser did not approve the key yet.
- The key is not correct (for example, a part of it is missing).

## Search links

```sh
datasheet-lens links TPS7A0233PYCHR
```

The MCP tool is `part_links(mpn)`. It gives one search link for each site:

| Group | Sites |
|---|---|
| Datasheet, price, stock | DigiKey, Mouser, Farnell, LCSC, Octopart, Findchips |
| Symbol, footprint, 3D model | Component Search Engine, SnapMagic, Ultra Librarian |

When DigiKey is in the global distributor list and has a key, the result also gives
the direct links of DigiKey for the MPN (`Direct links (Source: DigiKey)`). They go to
the CAD models at SnapMagic and Ultra Librarian, and to the datasheets. The list of
the project file has no effect here.

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
- The terms of DigiKey ask to name DigiKey as the source of its data
  (`Source: DigiKey`). They do not permit a local database of DigiKey data. Thus the
  tool keeps the DigiKey data only in the memory of the process, for one hour, and
  never writes it to a file.
- Each user uses a personal key under the terms of the distributor.
