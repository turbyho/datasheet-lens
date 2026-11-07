# Typical uses

datasheet-lens helps a person and an LLM agent to manage the input documents of a
design project: the datasheets, manuals and errata of its parts. It keeps these
documents complete, in the repository, and equal to the parts of the CAD project. It
lets the agent check the BOM, and check the design against the original datasheets.

This document gives typical uses. Each use gives the situation, the steps (MCP tools
and CLI commands, in the order of use) and the result.

datasheet-lens does not read the schematic or the PCB. The parts of the CAD project
come from the CAD tool: its MCP server, its BOM export, or a BOM file (CSV). The agent
gets them from there and gives them to datasheet-lens.

## 1. Keep the datasheets of a design in the repository

**Situation.** A design repository has no complete list of the datasheets of its
parts. Some PDF files are on the disks of different persons.

**Steps.**

1. Make the project file in the root of the repository:
   `datasheet-lens project init`.
2. For each part, add its datasheet:
   `datasheet-lens add PATH|URL -m MPN` (MCP: `add_document`, then
   `project_add_part`).
3. Copy the documents into the document folder: `datasheet-lens sync`
   (MCP: `project_sync`).
4. Put `datasheet-lens.toml` and the document folder (default `datasheets/`) into
   git.

**Result.** The project file holds each part with references to its documents (by
SHA-256). The document folder holds a copy of each document, in subfolders by
manufacturer or by the layout that you select. A different person runs
`datasheet-lens sync` after the clone, and gets the same documents in the local
store. `AGENTS.md` and `CLAUDE.md` tell each agent how to use them. See
[projects.md](projects.md).

## 2. Keep the BOM equal to the CAD project

**Situation.** The schematic changes. Parts come and go, and an MPN changes. The
project file must hold the same parts as the CAD project.

**Steps.**

1. Get the parts of the CAD project: a BOM export as CSV with one line for each
   symbol (`kicad-cli sch export bom`, or `export_bom` of the MCP server
   kicad-workbench), or a list from the MCP server of the CAD tool. Give the fields
   `MPN`, `Value` and `Value_Confirmed` as they are: each part with an MPN needs its
   datasheet, also an alternative in `Value`.
2. Compare them with the project file: `project_check(bom=<CSV file>)` or
   `project_check(parts)` (CLI: `datasheet-lens check bom.csv`). The CSV reader is
   tested only with BOM exports of KiCad and Eagle.
3. For each `conflict`: tell the user. The fields or the columns of one symbol do
   not agree, for example two MPN columns with different MPNs. One of them is wrong.
4. For each `change in the CAD project`: tell the user. The MPNs in the
   parentheses of `Value` are not the field `MPN`, or `Value` is not equal to
   `Value_Confirmed`, for example a new MPN or a new general value. Write the fields
   of the line only when the user confirms the change. For each `complete the
   fields` line, write the given fields: the MPN goes into `Value` in parentheses
   (`10k (RC0603FR-0710KL)`), and the parts do not change.
5. For each `missing` part: add its datasheet and the part (`add_document`,
   `project_add_part`). The line of the result gives the next call. An alternative
   of a part (`[alternative of MPN]`) needs its datasheet too.
6. For each `different` part: find the correct MPN, then change the project part in
   place with `project_update_part` (CLI: `datasheet-lens set`), or tell the user
   to change the CAD project.
7. For each `extra` part: tell the user. Remove it only when the user agrees
   (`project_remove_part`, CLI: `datasheet-lens rm`).
8. Run `project_sync`, then `project_check` again.

**Result.** The check gives no conflict, no change and no missing, extra or different part. A `different` line
finds one part with two MPNs: the base part in one place and the concrete MPN in the
other, or a variant of the same datasheet. A CAD MPN with placeholders at its end
(`nRF9151-LAXX`, `STM32F103C8Tx`) also gives a `different` line for the project part
that it matches. A line of the BOM file with a DNP mark is a part of the project
too: the user can change DNP at any time. The line `not fitted (DNP)` names these
symbols. With no step left, the line `next steps` gives `project_sync` and then
`project_availability`.

## 3. Select the concrete orderable MPN

**Situation.** The schematic has a base part, for example `TLV755P` or `TPS7A02`. The
BOM needs the concrete orderable MPN, with package, voltage, grade and packing.

**Steps.**

1. Get the orderable codes of the datasheet: `get_ordering_codes(document)`
   (CLI: `datasheet-lens codes DOC`).
2. When the datasheet gives only a scheme of its codes, find the ordering information
   with `search_text("ordering")`, and read it with `get_page_image`.
3. Get the concrete MPNs that the distributor sells, with state and stock:
   `search_distributors("TLV755P")` (CLI: `datasheet-lens find TLV755P`).
4. Change the project part to the concrete MPN, with the base part:
   `project_update_part(mpn="TLV755P", new_mpn="TLV75533PDBVR", base="TLV755P")`
   (CLI: `datasheet-lens set TLV755P -m TLV75533PDBVR -b TLV755P`).

**Result.** The project part has the concrete MPN in `mpn` and the base part in
`base`. It keeps the datasheet of the base part. When a project part is probably a
base part, `project_add_part` and `datasheet-lens add` give a note with the codes of
the datasheet and the call that changes the part.

## 4. Make sure that the parts can be ordered

**Situation.** Before an order or a release, the team wants to know if each part can
be bought.

**Steps.**

1. Save a personal API key one time: `datasheet-lens key mouser` or
   `datasheet-lens key digikey`.
2. Check the project: `project_availability` (CLI: `datasheet-lens stock`).
3. For each warning, find a different MPN with `search_distributors`, or a part with
   the same parameters with `find_parts` (see use 15). Then change the part with
   `project_update_part`.

**Result.** One section for each enabled distributor, with one line for each part:
its lifecycle state and its stock. A part with a purchase source (`[[part.source]]`,
see use 15) gets the offer of its number: the packaging that the project orders. A
`WARNING` shows a part that is obsolete or discontinued, a part that the distributor
does not have (often a base part or a wrong MPN), or a part with no stock. Mouser
also gives the suggested replacement, the quantities on order and the lead time.
With the JLCPCB parts library, a JLCPCB section gives the JLCPCB stock and the
library class, and replacements (see use 16). See [distributors.md](distributors.md).

## 5. Review the design against the datasheets

**Situation.** The agent must check a schematic: pin functions, absolute maximum
ratings, logic levels between voltage domains, the application circuit and the
recommended external parts (capacitors, resistors, inductors).

**Steps.**

1. Get the values that a session read before: `find_facts(part)`.
2. Find the tables and figures: `get_index`, `list_items`, `search_text` (for example
   `search_text("Absolute Maximum", mode="word")` or `search_text("VIH")`).
3. Read each value from the original: `get_page_image` with an element ID, a caption
   (`caption="Table 7"`) or a region.
4. Save each value that the review uses: `save_fact`, with page, element, conditions
   and column.
5. Compare the values with the schematic data from the CAD tool.

**Result.** Each value of the review comes from the original page, not from the text
layer and not from the memory of the model. Each fact points to its document, page
and element, so that a person can examine the source. The comparison with the
schematic is the work of the agent: datasheet-lens gives the values.

## 6. Check a footprint against the package drawing

**Situation.** A footprint of a new part must agree with the package drawing of the
datasheet: pad size, pitch, pin 1 position, and the view (top or bottom).

**Steps.**

1. Find the package drawing: `search_text("Package", part=MPN)` or
   `list_items(document, kind="figure")`.
2. Read the drawing: `get_page_image` with the caption or a region at 300 to 600 dpi.
   For small print, give a higher `max_px`.
3. Get the pads of the footprint from the CAD tool or its MCP server.
4. Compare the two. Save the dimensions with `save_fact`.

**Result.** The agent compares the footprint with an image of the original drawing,
at a resolution that shows small dimensions. When the store has no package data,
`part_links` gives links to sites with symbols, footprints and 3D models.

## 7. Firmware work

**Situation.** A firmware developer needs a register map, bit fields, reset values,
timing values, or the I2C or SPI address of a part.

**Steps.**

1. Find the register: `search_text("CTRL_REG1", mode="word")`, or
   `search_text("(slave|device) address", mode="regex")`. For a large manual,
   `get_outline(section="8.27")` gives the entries under one section.
2. See the elements of the page: `get_page_map(document, page)`.
3. Read the register table: `get_page_image` with the element ID of the table.
4. Save each value: `save_fact`, for example subject `"CTRL_REG1 reset value"`.

**Result.** The developer gets the values from the original register table. A later
session finds them with `find_facts`, with the page and the element of each value.

## 8. Use the values again

**Situation.** Different sessions need the same values, and one session must not read
a value two times.

**Steps.**

1. Before you read a value, call `find_facts` with the part, the document or a text.
2. When you compare a fact with the original, set its state with `set_fact_status`:
   `checked`, or `disputed` with the reason.
3. When you save a new fact, read the note of `save_fact` about the facts of the
   document that it can be a copy of.

**Result.** A session uses the values of earlier sessions, and knows how far to trust
each one. A concrete MPN also finds the facts of the datasheet of its base part.
`get_page_map` and `get_page_image` show the known facts of each page.

The facts are in the central store of the user. All sessions and all projects of the
user on that machine see them. The repository does not share them with other persons.

## 9. A part with no datasheet in the store

**Situation.** `project_check` gives a missing part, and the store has no datasheet.

**Steps.**

1. When the CAD symbol has a Datasheet field with a URL, add it:
   `add_document(path=URL)` (CLI: `datasheet-lens add URL -m MPN`).
2. When a distributor has a datasheet URL, `search_distributors` gives it.
3. Else get search links: `part_links(mpn)` (CLI: `datasheet-lens links MPN`). Open
   one with a web tool, or give the links to the user.
4. When a server blocks the download, download the file in a browser and put it into
   the inbox of the project (`datasheets/_inbox/`), then run `project_sync` (CLI:
   `datasheet-lens sync`). Sync gives the file to the project parts that it agrees
   with (see [projects.md](projects.md#the-inbox)). A file with no part stays in the
   inbox: the report gives the part of the PDF, and you assign it with
   `project_add_part`.

**Result.** The part gets its datasheet, or the user gets the MPN, the reason and the
search links.

## 10. Errata, manuals and application notes

**Situation.** A part has a datasheet, a reference manual and an errata sheet.

**Steps.**

1. Add each document with its role: `datasheet-lens add errata.pdf -b MPN --role
   errata` (MCP: `add_document(path, part, role="errata")`).
2. Search all documents of the part: `search_text(text, part=MPN)`.

**Result.** The project file lists each document of the part with its role. The
search finds a register in the manual and its known problems in the errata.

## 11. One datasheet for more parts

**Situation.** One datasheet covers a family, for example `ADC34J22`, `ADC34J23` and
`ADC34J24`.

**Steps.**

1. Add the datasheet once, with the variants:
   `add_document(path, part="ADC34J24", variants=["ADC34J22", "ADC34J23"])`. The CLI
   command `add` proposes the variants from the PDF.
2. Add the variant that the design uses to the project.

**Result.** The store links the datasheet to each variant. The project part lists the
variants for information. Another project that uses `ADC34J22` gets the same
document with no new download.

## 12. A team with one project file

**Situation.** More persons work on one design, each on a different machine.

**Steps.**

1. Each person installs datasheet-lens and runs `datasheet-lens init`.
2. Each person saves a personal API key.
3. After each pull, each person runs `datasheet-lens sync`.

**Result.** Each person has a local store, and the team shares one project file and
one document folder through git. Sync imports the copies of the repository into the
local store, with their parts and base parts. Each person keeps the facts in the
local store, and the API keys in the key store of the system (see
[distributors.md](distributors.md)).

With a team server, each person also connects the local store to the server
(`datasheet-lens key team`, then `datasheet-lens server connect URL`). The team then
shares the facts and the decisions about manufacturer names too, and a datasheet
that one person added is found by each other person. See
[team-server.md](team-server.md).

## 13. A changed or new revision of a datasheet

**Situation.** A person puts a new revision of a datasheet into the document folder,
or a copy changes.

**Steps.**

1. Run `datasheet-lens ls` to see the state of the copies.
2. A copy with a different content than the store gives the line `changed`. To get
   the document of the store again, run `datasheet-lens sync --restore`. To keep the
   new file as a new document, give it a different file name, then run `sync`.
3. Assign a new document to its part with `project add-part MPN --document DOC`.
4. Remove the old document from the part with `datasheet-lens rm -d DOC`.

**Result.** The project file references the revision that the team uses. The facts
of the old revision stay linked to the old document.

## 14. Use the same parts in more projects

**Situation.** A person works on more designs, and the designs use the same parts:
the same microcontroller, LDO or sensor.

**Steps.**

1. In the first project, add the datasheet and the part (`add_document`,
   `project_add_part`). Read the values that the work needs, and save them with
   `save_fact`.
2. In the second project, add the part with `project_add_part`. The central store
   has the datasheet already: the tool does not download or index it again, and
   `project_sync` copies it into the second project.
3. Before you read a value, look for it with `find_facts(part)`. The facts of the
   first project are there, with their page and element.

**Result.** Each datasheet is in the store one time, with its index, its element
map and its facts. Each project of the person uses them at once. The second project
starts with the knowledge of the first one.

## 15. Find a new part or a replacement part by its parameters

**Situation.** The design needs a part with given parameters, for example an
N-channel MOSFET for a 12 V load of 5 A with PWM and a gate drive of 3.3 V from a
microcontroller. Or a project part is not orderable, and the team needs a
replacement with the same parameters.

**Steps.**

1. Make sure that DigiKey is enabled and has a key: `distributor_status` (CLI:
   `datasheet-lens dist`). It also tells the services of each distributor.
2. Find the category: `part_parameters("digikey", "mosfet")` (CLI: `datasheet-lens
   pick digikey mosfet`). A part of a name that fits more categories gives the list of
   these categories, with their names in quotes.
3. Get the names of the parameters of the selected category: `part_parameters(
   "digikey", "Single FETs, MOSFETs")` (CLI: `datasheet-lens pick digikey "Single FETs,
   MOSFETs" -P`).
4. Find the candidates: `find_parts("digikey", "Single FETs, MOSFETs", ["channel =
   N-Channel", "vdss >= 20 V", "drive <= 2.5V", "package has 3x3"])` (CLI:
   `datasheet-lens pick digikey "Single FETs, MOSFETs" ...`).
5. For each candidate that the design can use, take it with `adopt_part(source,
   number)` (CLI: `datasheet-lens adopt SOURCE NUMBER`): it adds the datasheet, the
   part and its purchase source. Read each value that the design needs, for example
   RDS(on) at a gate voltage of 2.5 V, with `get_page_image`. Save it with
   `save_fact`.
6. Give the user the links to the symbol, the footprint and the 3D model of the
   selected part: `part_links(mpn)` (CLI: `datasheet-lens links MPN`).
7. Remove the candidates that the design does not use from the project
   (`project_remove_part`).

For a board that JLCPCB assembles, search in the JLCPCB parts library: give the source
`jlcpcb` (CLI: `datasheet-lens pick jlcpcb ...`). The result gives the JLCPCB stock and the library class
of each part. The user downloads the library one time with `datasheet-lens jlc update`.

**Result.** A short list of candidates with stock, price and lifecycle state, and
the values that the design needs, from the datasheets. At DigiKey, the result marks
a filter that the DigiKey data cannot decide `NOT APPLIED` (a condition such as
`at Vgs<=2.5V`, or a filter that the DigiKey API does not apply), and a part that
does not agree with such a filter `NOT AGREE`: read that value in the datasheet. The
JLCPCB library applies a condition to the points that its data lists. See
[distributors.md](distributors.md#find-parts-by-their-parameters).

## 16. Prepare the BOM for the JLCPCB assembly

**Situation.** JLCPCB assembles the board. Each part needs a JLCPCB (LCSC) number,
JLCPCB must have the part in stock, and the team wants few parts with a loading fee
(Extended).

**Steps.**

1. Download the JLCPCB parts library one time: `datasheet-lens jlc update` (MCP:
   `jlcpcb_library(action="update")`, only when the user asks).
2. Save the JLCPCB number of each part as its purchase source:
   `adopt_part(source="jlcpcb", number="C47616")` (CLI:
   `datasheet-lens adopt jlcpcb C47616`).
3. Check the availability: `project_availability` (CLI: `datasheet-lens stock`). The
   JLCPCB section gives the stock and the library class of each part, and up to 3
   replacements for a part with no stock or with a loading fee: the same MPN, and
   resistors, capacitors and inductors with the same parameters. Read the datasheet
   of a replacement before you propose it.
4. Check the CAD project: `project_check` with the BOM. The lines `LCSC:` compare the
   JLCPCB number of each symbol (the LCSC column of the BOM) with the purchase source
   and with the library: the number to write into a symbol, a number with no
   purchase source, a different number, and a number of a different MPN.
5. Write the numbers into the symbols (KiCad: `set_symbol_fields` of the MCP server
   kicad-workbench), and check again.

**Result.** Each symbol has the JLCPCB number of its part, the number agrees with
the purchase source and with the library, and the parts with no stock or with a
loading fee have replacements to select from. See
[distributors.md](distributors.md#purchase-sources-and-replacements) and
[projects.md](projects.md#the-jlcpcb-number-of-a-symbol).
