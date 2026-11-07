# Distributors

datasheet-lens can ask a distributor for the data of a part through its official
API. This document tells what the distributor data gives and how to set the API keys.
It also tells how to select the distributors, how to find parts by their parameters,
and how to use the JLCPCB parts library on your machine.

Supported now: Mouser and DigiKey, and the JLCPCB parts library (a local database).
Not supported yet: Farnell and TME.

## What the distributor data gives

A datasheet tells what a part does. It does not tell if you can buy the part. Some
datasheets give only a scheme of their orderable codes, not a list. A code that the
scheme permits is not always made or sold. The distributor data gives, for each
concrete orderable MPN:

- The manufacturer and a description.
- The lifecycle state, for example `New Product` or `Obsolete`. Mouser also gives a
  suggested replacement.
- The stock. Mouser also gives the quantities on order with their dates, the stock of
  the factory, and the lead time.
- The prices for different quantities: a number with the currency code (`0.223 CZK`).
  Mouser gives the text of a price in the format of the account (`0,223 Kč`), and
  the number: the tool uses the number. When Mouser gives no number, the price is
  its text with its source (`Mouser: 0,223 Kč (CZK)`).
- The package, from DigiKey and the JLCPCB parts library. Mouser gives no package.
- The URL of the datasheet and of the product page.

A part that is obsolete, discontinued or at its end of life gets the mark
`NOT ORDERABLE`. An offer with no part number to order (Mouser gives `N/A` for some
manufacturers of an MPN) gets it too.

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
| `datasheet-lens dist --site US --currency USD` | Set the default country and currency again (the United States, US dollars). |

The tool uses the first list that it finds:

1. The key `distributors` of the project file.
2. The global list in `config.json`.
3. The default: each distributor with a key (in an environment variable, the key
   store or the key file). With no key at all: `mouser`, so that you get the note
   with the steps.

A name that the tool does not support gives an error. A name in a list that a newer
version wrote is ignored, with a note.

DigiKey gives the prices and the stock for the country and in the currency of the
settings. The default is `US` and `USD`: `datasheet-lens dist --site US --currency USD`
sets it again. The MCP tool `set_price_locale(site, currency)` sets them too, when the
user asks the agent. Mouser gives them for the
country of the Mouser account, and ignores these settings.

## Find a part

```sh
datasheet-lens find TLV755P          # each MPN that starts with TLV755P (up to 50)
datasheet-lens find -x TLV75533PDBVR # only this MPN
```

The MCP tool is `search_distributors(mpn, exact)`. The result gives one line for each
orderable part, with its source. Use it to select the concrete MPN of the BOM, and to
make sure that it can be ordered. The start of an MPN must have 3 characters or more.

Each line gives the part number of the distributor (for example
`Mouser 511-USBLC6-2SC6`, `DigiKey 497-5235-1-ND`, `JLCPCB C7519`). Give this number
to `adopt` (see [Take a found part](#take-a-found-part)). An MPN of more manufacturers
(a second source, or a copy of a part) gives one line for each manufacturer.

## Find parts by their parameters

The parametric search finds new parts or replacement parts by their parameters. Each
search names its source, because the categories, the parameters and the stock differ
between the sources:

- `digikey`: the stock and the lifecycle state at DigiKey. It needs DigiKey in the
  enabled distributors, and the DigiKey key. Mouser gives no parameters through its
  API.
- `jlcpcb`: the JLCPCB stock for the assembly and the library class. It needs the
  JLCPCB parts library on your machine (see
  [JLCPCB parts library](#jlcpcb-parts-library)).

The tool does not search more sources in one search.

```sh
datasheet-lens pick digikey mosfet                     # the categories that fit "mosfet", to select from
datasheet-lens pick digikey "Single FETs, MOSFETs" -P   # the parameters of the category, with examples of values
datasheet-lens pick digikey "Single FETs, MOSFETs" "channel = N-Channel" "vdss >= 20 V" "drive <= 2.5V" "package has 3x3"
```

Give the category by its name. A part of a name that fits more categories gives the
list of these categories, with each name in quotes to copy, its ID and its place in
the category tree. The DigiKey ID (for example `278`) also works. A name that more
categories have (for example `Accessories`) needs the ID.

The MCP tools are `part_parameters(source, category)` and `find_parts(source, category, filters)`.

A category with subcategories, or a name that fits more than one category, gives the
list of the categories to select from (not an error). Copy the name of one, with its
quotes.

A filter is `NAME OP VALUE`:

| Part | Values |
|---|---|
| `NAME` | The full name of a parameter, its short name, or a part of the name that only one parameter has. A part of the name that more parameters have gives a list of these parameters (full name in quotes, short name, number of parts), and the search does not run. This is not an error: select one, and give its full name or short name. The short names for the FET categories: `vdss`, `rds_on`, `vgs_th`, `id`, `qg`, `drive`, `channel`. For all categories: `package`, `case`, `mounting`. `-P` shows the short names. |
| `OP` | `=`, `<`, `<=`, `>`, `>=`, `has` (the value holds the text). |
| `VALUE` | A number with its unit (`20 V`, `10 mOhm`, `5A`), or a text. `=` and `has` accept more values with `\|` (one of them), for example `package has 3x3\|3.3x3.3`. |

A numeric filter compares the first number of a value. For example, the drive
voltage `2.5V, 8V` gives 2.5 V, the lowest gate voltage that the datasheet specifies.
`4.5mOhm @ 20A, 8V` gives 4.5 mOhm. The text after `@` holds the conditions.

At DigiKey, a filter with no value that agrees gives 0 parts and examples of the
values. The JLCPCB library gives the number of parts that agree after each filter.

DigiKey keeps one value of a parameter, at its own conditions. For example, it keeps
RDS(on) at the highest drive voltage only. Thus the tool does not apply a filter
with a condition after `at` or `@` (`rds_on <= 10 mOhm at Vgs=2.5V`). The result
marks it `NOT APPLIED`. Read that value in the datasheet of each part.

The DigiKey API does not apply a filter on some parameters: it gives no part,
although it gives parts for the values. For example, the parameters whose values are
counts (`Unidirectional Channels`, `Number of Regulators`). When a search gives no
part, the tool does a check of each filter alone. A filter that DigiKey does not apply
gets the mark `NOT APPLIED`, and the tool does the search again without it. The
number of parts is then the number for the applied filters. The tool compares the
value of that parameter in each part line: a part that does not agree gets the mark
`NOT AGREE`. The tool keeps the result of each check for one hour.

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

## JLCPCB parts library

The JLCPCB parts library is an optional local database for the JLCPCB assembly
service. It tells for each part the stock of JLCPCB for the assembly and the library
class. Use it when JLCPCB assembles the board.

### Download and update

```sh
datasheet-lens jlc update    # download or update the database
datasheet-lens jlc           # show its age and its path
datasheet-lens jlc remove    # delete it
```

The database comes from the [jlcparts](https://github.com/yaqwsx/jlcparts) project,
which gets it from the official JLCPCB Components API. It has 3.8 million parts. The
download is 276 MB, and the database needs 2.2 GB on the disk. The tool puts it into
`jlcpcb/` in the data folder of the store. Each machine downloads it.

The jlcparts project updates the data three times each day. The tool downloads it
only on your command. A result tells you when the database is older than 1 day.

An agent can start the update for you with the MCP tool
`jlcpcb_library(action="update")`. The update runs in the background, and the tool
answers at once. `jlcpcb_library(action="status")` gives the progress, the result or
the error, and the age of the database. One update runs at a time. The agent starts
an update only when you ask for it.

> CAUTION: The data of jlcparts has no license statement. The values come from the
> LCSC listings: some are typical values, not maximum values, and some parts copy the
> values of other parts. Read each value that the design needs in the datasheet.

### Stock and library class

The result gives two different properties of each part:

- **The JLCPCB stock.** A part with stock can go to the assembly now. A part with no
  stock has the mark `NOT IN STOCK`: the assembly needs an order of the part first.
- **The library class.** It sets the assembly fee: Basic and Preferred parts have no
  loading fee, Extended parts have one. The class does not tell the stock.

### Parametric search in the JLCPCB library

```sh
datasheet-lens pick jlcpcb mosfet -P
datasheet-lens pick jlcpcb "MOSFET|MOSFETs" "channel = N-Channel" "vdss >= 20 V" "rds_on <= 10 mOhm at Vgs<=2.5V"
datasheet-lens pick jlcpcb MOSFETs "library = Basic|Preferred"
```

The MCP tools are `part_parameters("jlcpcb", category)` and
`find_parts("jlcpcb", category, filters)`.

- **Category.** A subcategory name selects each category with that name, for example
  `MOSFETs`. `CATEGORY > SUBCATEGORY` selects one. More names go with `|`. The result
  also names the other categories with a name that holds the text.
- **Parameters.** The parameters of the LCSC listings, and `Package`, `Manufacturer`
  and `Library` (Basic, Preferred, Extended). `-P` shows them with examples.
- **Conditions.** Some values have more points, for example `4mΩ@4.5V, 6mΩ@2.5V`. A
  filter with a condition uses only the points with a condition that agrees. The
  result tells the number of parts that give no value at that condition. Read that
  value in their datasheet.
- **Stock.** By default, only the parts with JLCPCB stock. `-a` (MCP: `in_stock=False`)
  adds the parts with no stock.

The result gives for each part: the LCSC number, the MPN, the manufacturer, the JLCPCB
stock, the library class, the price of the first quantity (USD), the filtered
parameters, the package, the datasheet URL, and the links to the part at JLCPCB and at
LCSC.

`find` (`search_distributors`) and `stock` (`project_availability`) also give the
JLCPCB stock and the library class of a part, when the database is on your machine.

### Purchase sources and replacements

`stock` (`project_availability`) uses the purchase source of a part
(`[[part.source]]`, see [Take a found part](#take-a-found-part)): it asks for its
number (the JLCPCB C number, the DigiKey number, the Mouser number), thus for the
packaging that the project orders. A part with no purchase source gets the offers of
its MPN. A number of a different MPN gives a warning, and its stock is not the stock
of the part.

For a part with no JLCPCB stock, with a loading fee (Extended), or with a lifecycle
risk of its manufacturer (see [Check the project parts](#check-the-project-parts)),
the lines `replacement` give up to 3 replacements with JLCPCB stock, Basic and
Preferred first:

- The same MPN with a different C number (an other manufacturer or listing). For a
  part with a lifecycle risk, the same MPN of the manufacturer of the part is not a
  replacement: the manufacturer registry compares the names of the library and of
  the project file. The same MPN of an other manufacturer is one, with the note
  that its lifecycle state is not checked.
- For a resistor, a capacitor (MLCC) or an inductor: a part with the same package,
  the same value, and parameters that are not worse (tolerance, power, voltage,
  dielectric, current, DC resistance). A part that does not give a parameter is not
  a replacement: the tool cannot verify it.

For a part with stock, only Basic and Preferred parts are replacements, because an
other Extended part does not decrease the fee. For a part with a lifecycle risk, an
Extended part is a replacement too. The tool
does not replace semiconductors and ICs: find them with `pick jlcpcb`
(`find_parts`) and the values of the datasheet. The values of a replacement come
from the LCSC listings: read its datasheet before you use it.

## Take a found part

When you select a part of a search, take it in one step:

```sh
datasheet-lens adopt jlcpcb C47616 -c MOSFET
datasheet-lens adopt digikey 296-25646-1-ND -b CSD16340
datasheet-lens adopt mouser --mpn CSD16340Q3
```

The MCP tool is `adopt_part(source, number, mpn, base, category)`. Give one of two
inputs:

- `number`: the part number of the source, as the search gives it: a DigiKey number,
  a JLCPCB C number (`C` and digits) or a Mouser number.
- `mpn`: the MPN. The source must have one part for it. With more parts, the result
  gives their numbers to select from.

The tool does not guess if a text is a number of the source or an MPN. The command
does these steps:

1. It gets the part from the source: the MPN, the manufacturer, the package (not
   from Mouser) and the product page.
2. It gives the lifecycle state of the manufacturer: the state of the source, and
   the state at each other enabled distributor with a key (only the offers of the
   manufacturer of the part). A lifecycle risk (obsolete, last time buy, not for new
   designs) gives a `WARNING`, also when the source has stock: the JLCPCB library has
   no lifecycle state. With no distributor that gives a state, the line
   `lifecycle: not checked` gives the reasons.
3. It gets the datasheet. A datasheet of the part, or of its base part, in the store
   comes first: the tool does not download a second copy. Else it downloads one: a
   URL on the site of the manufacturer comes first, a copy at a distributor next. The
   tool asks the other sources for URLs only when it needs them. It uses only the
   URLs of the parts of the same manufacturer: a copy of a part with the same MPN has
   its own datasheet. The manufacturer registry compares the names (see
   `datasheet-lens maker` in [cli.md](cli.md)). The result gives the names of the
   manufacturers whose URLs the tool did not use. When one of them is a different
   name of the manufacturer, record it with `datasheet-lens maker alias NAME OTHER`.
   The result tells the
   URL that it used, and the URLs that failed. When no URL gives a PDF, the part comes
   with no datasheet. The result then gives the URLs to open in a web browser and,
   in a project, the path of its inbox: save the file there, and run `sync` (see
   [projects.md](projects.md#the-inbox)).
4. It adds the document and the part to the store, and in a project folder to the
   project. The CLI copies the datasheet into the document folder of the project. With
   the MCP tool, run `project_sync` after it.
5. It saves the purchase source: in the store (the team server shares it) and in the
   project file (`[[part.source]]`, see [projects.md](projects.md)). The purchase
   source has the source, the part number, and for JLCPCB the library class. The
   store also keeps the product page and the date. It has no stock and no price:
   these change, and the terms of DigiKey do not permit an own database of DigiKey
   data. A second `adopt` of the same part with a different number adds a second
   purchase source (for example cut tape and reel), and does not replace the first:
   `datasheet-lens rm MPN -s NUMBER` (`project_remove_source`) removes one.

Then read the values that the design needs in the datasheet, and save them with
`save_fact`.

## Check the project parts

```sh
datasheet-lens stock
```

The MCP tool is `project_availability`. The sources are the enabled distributors with
a key and the JLCPCB parts library when it is on the machine (see
[Purchase sources and replacements](#purchase-sources-and-replacements)). The result
gives one verdict for each project part, from all sources:

```text
CAP-OLD (C1001, the purchase source): WARNING: OBSOLETE at the manufacturer (Mouser: End of Life; DigiKey: Obsolete). Orderable at the purchase source (JLCPCB). Select a replacement with the user (find_parts with the values of the datasheet)
  Mouser: End of Life, stock 0. WARNING: not orderable: End of Life
  DigiKey: Obsolete, stock 0. WARNING: not orderable: Obsolete
  JLCPCB: stock 12000, library Basic (C1001)
  replacement: C1002 CAP-NEW (Maker A), library Basic, JLCPCB stock 50000, 0805 (the same package and parameters): read its datasheet before you use it
RES-10K (C2001, the purchase source): orderable at the purchase source (JLCPCB). Mouser: does not have this MPN; DigiKey: does not have this MPN; JLCPCB: stock 400000, library Basic (C2001)
```

The verdict has these parts:

1. A lifecycle risk of the manufacturer at any distributor: `OBSOLETE`, `LAST TIME
   BUY` or `NOT FOR NEW DESIGNS`, with the state of each distributor, thus a
   difference between the distributors shows. The verdict asks for a replacement
   also when a source has stock.
2. For a part with a purchase source: `orderable at the purchase source`, or a
   `WARNING` with the reasons of each number (not checked, the number is not there,
   the number is an other MPN, not orderable, no stock) and the other sources with
   stock. A purchase source at a source that the tool cannot ask (a distributor that
   is not enabled or has no key, or the JLCPCB library that is not on the machine)
   is not checked: the verdict tells it.
3. For a part with no purchase source: `orderable` with the sources that have stock,
   or a `WARNING`: no stock at any source, not orderable at any source, no offer of
   the manufacturer of the part, or no source has the MPN (a base part, or a wrong
   MPN).
4. The sources that the tool did not check (`Not checked: DigiKey`).

A part that no symbol fits (DNP) is not placed by the assembly: its availability is
information, not a step. `project_check` gives these parts in its line `next steps`
(`project_availability(not_fitted=[...])`, CLI: `stock --dnp MPN ...`). Their verdict
starts with `not fitted (DNP), information only` and has no `WARNING`.

The offers of a different manufacturer (when the MPN has offers of two or more
manufacturers), and a purchase source number of a different MPN, are not offers of
the part: they give no stock and no lifecycle risk to the verdict. The details show
their data with a warning.

A part with no warning gets one line, with the details of each source after the
verdict. A part with a warning gets one indented line for each source. A source that
does not have an MPN gives only `does not have this MPN`: a different source can
sell it (for example a part of a Chinese manufacturer at JLCPCB). The details of a
source give a `WARNING` for a part that:

- Is not orderable (obsolete or discontinued), with the replacement (Mouser).
- Has a purchase source number that the source does not have, or that is a
  different MPN.
- Has no stock, with the quantities on order and their dates, and the lead time
  (Mouser).
- Has the Mouser state `Restricted Availability` or the DigiKey state
  `Discontinued at Digi-Key`: the distributor does not sell the part now. This is not
  the lifecycle state of the manufacturer. Ask the manufacturer or a different
  distributor.
- Has no offer from the manufacturer of the part, when the MPN has offers of two or
  more manufacturers: the line then gives the data of a different manufacturer.
- Has no JLCPCB stock: the assembly needs an order of the part.

After the parts, the result gives the lines of the sources: the parts with no
purchase source of a source, a distributor that did not answer, and the age of the
JLCPCB library.

The tool finds a lifecycle risk by the words of the state: `Obsolete`, `End of
Life`, `EOL`, `Discontinued` (obsolete); `Last Time Buy`, `LTB` (last time buy); `Not
For New Designs`, `Not Recommended for New Designs`, `NRND` (not for new designs).
The documentation of the APIs gives no list of the states: a state with none of
these words shows as the distributor gives it, with no risk. The states of the
orderable offers come first: an obsolete packaging of an MPN that has an orderable
packaging is not a state of the MPN. Of these offers, the most severe risk wins.

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
check, in one line after the parts. The other distributors continue. The tool keeps each answer in memory for one
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
| JLCPCB library | Parametric search with more points of a value. MPN search: JLCPCB stock for the assembly, library class | Lifecycle state, values of a known quality |

The status also tells if the JLCPCB database is on this machine, and its age.

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
`part_links`, and the data of the JLCPCB parts library when it is on the machine.

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
the CAD models at SnapMagic and Ultra Librarian, and to the datasheets. An MPN of more
manufacturers gives the links of each manufacturer, with its name
(`Direct links (Source: DigiKey, STMicroelectronics)`). The list of the project file
has no effect here.

datasheet-lens does not open these links. A person or an agent with a web tool opens
them. The sites for the CAD files need a free account for the download. For a
datasheet that you find, run `datasheet-lens add URL`, or call `add_document` with
the URL.

## Terms of use

- datasheet-lens uses only the official APIs of the distributors. It does not read
  their web pages (no scraping).
- The terms of Mouser ask to name Mouser as the source of its data, and not to mix it
  with other data with no distinction. Thus each value names its source: a search
  gives a section `Source: Mouser`, and `project_availability` gives each value after
  the name of its source (`Mouser: End of Life, stock 0`). The verdict of a part
  names the sources that it uses (`orderable (Mouser, JLCPCB)`). The tool does not
  add the stock of two sources.
- The terms of DigiKey ask to name DigiKey as the source of its data
  (`Source: DigiKey`, or `DigiKey:` in `project_availability`). They do not permit a local database of DigiKey data. Thus the
  tool keeps the DigiKey data only in the memory of the process, for one hour, and
  never writes it to a file.
- Each user uses a personal key under the terms of the distributor.
- The JLCPCB parts library comes from the jlcparts project. Its data has no license
  statement. The tool downloads it only on a command of the user.
