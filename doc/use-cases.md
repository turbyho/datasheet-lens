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

1. Get the parts of the CAD project: from the CAD tool or its MCP server, or a BOM
   export as CSV (`kicad-cli sch export bom`). Give the MPN and Value fields as they
   are: each part with an MPN needs its datasheet, also a capacitor with
   `390uF (EEU-FS1H391L)` in its Value.
2. Compare them with the project file:
   `project_check(parts)` (CLI: `datasheet-lens check bom.csv`). The MCP tool takes
   a list of parts from any source. The CLI command reads a CSV file. It is tested
   only with BOM exports of KiCad and Eagle.
3. For each `conflict`: tell the user. The MPN field and the Value field of one
   symbol name different parts, for example a new Value and the attributes of the
   old library part. One of the fields is wrong.
4. For each `missing` part: add its datasheet and the part (`add_document`,
   `project_add_part`). The line of the result gives the next call. An alternative
   of a part (`[alternative of MPN]`) needs its datasheet too.
5. For each `different` part: find the correct MPN, then change the project part in
   place with `project_update_part` (CLI: `datasheet-lens set`), or tell the user
   to change the CAD project.
6. For each `extra` part: tell the user. Remove it only when the user agrees
   (`project_remove_part`, CLI: `datasheet-lens rm`).
7. Run `project_sync`, then `project_check` again.

**Result.** The check gives no conflict and no missing, extra or different part. A `different` line
finds one part with two MPNs: the base part in one place and the concrete MPN in the
other, or a variant of the same datasheet. A CAD MPN with placeholders at its end
(`nRF9151-LAXX`, `STM32F103C8Tx`) also gives a `different` line for the project part
that it matches. The CLI command `check` ignores a line of the BOM file with a DNP
mark.

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

1. Save a personal API key one time: `datasheet-lens key mouser`.
2. Check the project: `project_availability` (CLI: `datasheet-lens stock`).
3. For each warning, find a different MPN with `search_distributors`, and change the
   part with `project_update_part`.

**Result.** One line for each part with its lifecycle state and stock. A `WARNING`
shows a part that is obsolete or discontinued (with the suggested replacement), a
part that the distributor does not have (often a base part or a wrong MPN), or a part
with no stock (with the lead time). See [distributors.md](distributors.md).

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
3. When you save a new fact, read the note of `save_fact` about similar facts of the
   same page.

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
4. When a server blocks the download, download the file in a browser, put it into the
   document folder, and run `datasheet-lens sync`. Then assign it to the part with
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
local store, with their parts and base parts. Each person keeps the API keys and the
facts in the local store.

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
