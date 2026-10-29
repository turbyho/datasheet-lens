# Agent workflow

This document tells how an LLM agent reads a datasheet with datasheet-lens. The
agent uses the MCP tools (see [mcp-server.md](mcp-server.md)). The server
instructions and the tool descriptions give the agent the same method in short form.

## The principle

datasheet-lens does not convert a datasheet to text. A conversion makes errors that
look correct: lost superscripts, merged table rows, shifted columns, and wrong values
from graphs. The text layer of a PDF can also lose signs, units and subscripts.

Thus the agent uses the index and the text layer only to find the content. It reads
each value from the original page: a PNG image (`get_page_image`) or the PDF pages
(`get_pdf_pages`). It reads only the parts that the work needs.

## Read a datasheet step by step

### 1. Find the document

| Tool | Use |
|---|---|
| `project_status` | The parts and documents of the user project. |
| `list_documents(part)` | The documents of one part in the store. A part can have a datasheet, a manual, errata and application notes. |
| `list_parts` | All parts in the store. |
| `add_document` | A new document from a local PDF or a URL, when the store does not have it. |

Each result gives the document reference: the first 12 characters of the SHA-256 of
the file. The other tools use this reference.

### 2. Find known facts

Before the agent reads a value, it calls `find_facts` with the part or the document.
A fact is a value that a session read before and saved with its source. A fact is
not the source of truth. For an important value, the agent compares the fact with
the original and sets its state (see step 9).

`get_page_map` and `get_page_image` also give a line with the known facts of their
page.

### 3. Get the index

`get_index(document)` gives:

- The outline, from the PDF bookmarks or from the numbered headings, with pages.
- The table and figure captions, with page and position.
- The pages that have no text layer (read them as images).
- The pages with a raster image below 300 dpi.
- A page map of headings, for a short document or a document with no outline.

A large document (more than 300 outline entries) gives only the top two levels.
`get_outline(document, section)` gives the entries under one section, for example
`section="8.27"`. `list_items(document, kind)` gives all tables, figures and
captions with their element IDs.

### 4. Search the text

`search_text` finds a word, a register name, a pin name or a value. Give
`document` for one document, or `part` for all documents of a part.

| Parameter | Effect |
|---|---|
| `mode="text"` | A substring, case is ignored (default). `"VDD"` also finds `"AVDD"`. The space between a number and its unit is optional: `"100 nF"` also finds `"100nF"`. |
| `mode="word"` | Whole words only. `"nF"` does not find `"configuration"`. |
| `mode="regex"` | A Python regular expression, case is ignored. For example `"Pin Functions?"`. |
| `max_lines` | The maximum number of hit lines (1 to 200, default 30). |
| `repeated=true` | Also show the lines that repeat on many pages (page headers and footers). |

Each hit line gives the document reference, the page, and the table or figure that
holds the line (an element ID such as `p5.T1`) or the region of the line. The tool
gives the element or region for the hits on the first 10 pages with hits. A last
line tells how many page header and footer lines it did not show.

### 5. Get the page map

`get_page_map(document, page)` gives the elements of one page: headings, tables,
figures, formulas and captions. Each line has the element ID, the type, the region
`[x0, y0, x1, y1]` in pt (origin at the top left of the page) and the text or
caption. The other text elements are only counted. `all_elements=true` lists them
too. `get_element_text` gives the full text of headings and paragraphs.

An element ID has the form `p<page>.<code><n>`, for example `p7.T1`. The codes are:

| Code | Element |
|---|---|
| `H` | Heading |
| `P` | Paragraph or text block |
| `L` | List item |
| `T` | Table |
| `F` | Figure |
| `Q` | Formula |
| `C` | Caption |
| `N` | Footnote |
| `K` | Code |
| `R` | Page header or footer (not shown) |

With the `text` backend (no docling), the map has no tables and figures. It has
headings, text blocks and captions.

### 6. Read the original

`get_page_image` gives the original as a PNG image. Give one of these:

| Parameter | Image |
|---|---|
| `element="p7.T1"` | The region of the element, with a small margin. |
| `caption="Table 57"` | The table or figure with this caption label. A table that continues on the next pages gives one image for each page (6 images or fewer). |
| `page` and `bbox=[x0, y0, x1, y1]` | A region of the page, in pt, origin at the top left. |
| `page` only | The full page. |

An element, a caption or a region is rendered at 300 to 600 dpi. A full page gets
the resolution that fits `max_px` on its long edge (256 to 8000, default 2576). A
region also stays within `max_px`: a large region that does not fit at 300 dpi gets
a lower resolution. Prefer an element, a caption or a region, because a full page
costs more input. For a table that fills the page, a full page at `max_px=1600` is
sufficient.

With the `text` backend, a caption gives the full page of the caption, because the
map has no table regions.

`get_pdf_pages(document, pages)` gives the original pages as a new PDF, for a client
that can read a PDF. The pages are a list such as `"7"` or `"3-5,8"`, 50 pages or
fewer.

### 7. Save the facts

`save_fact` saves each value that the work uses, with its source:

- `document`, `page`, and `element` or `bbox`: where the value is.
- `subject`: what the value is, for example `"VDD maximum"`.
- `value` and `unit`: one value as the original shows it. For MIN, TYP and MAX,
  save one fact for each column, and give `column`.
- `conditions`: for example `"TA = 25 °C, VDD = 3.3 V"`.
- `note`: for example a footnote of the table.
- `model`: the model ID of the agent.
- `part`: the part, when the document covers more parts.

A new fact has the state `unchecked`. When the page has a fact with the same subject
or element, the result gives a note. If the new fact is a copy, set its state to
`disputed`.

### 8. Find facts later

A later session calls `find_facts` with a text, a document or a part. A concrete
MPN also finds the facts of the datasheet of its base part. Each line gives the fact
number, subject, value, column, conditions, source, state and author.

### 9. Check a fact

After the agent compares a fact with the original, it calls `set_fact_status`:

| State | Meaning |
|---|---|
| `unchecked` | Nobody compared the fact with the original. |
| `checked` | The agent compared the fact with the original. |
| `disputed` | The original or a different source does not agree. |

The `note` is added to the existing note of the fact.

## Base part and concrete MPN

A datasheet describes a base part, for example `TLV755P`. A BOM needs a concrete
orderable MPN, with its package, voltage, grade and packing, for example
`TLV75533PDBVR`. datasheet-lens keeps the two:

- In the store, the datasheet is linked to the base part. A concrete part refers to
  its base part and gets its documents.
- In the project file, a part has the concrete MPN in `mpn` and the base part in
  `base`.

To find the concrete MPN:

1. `get_ordering_codes(document)` gives the orderable codes in the text layer, with
   page and line. A line of an ordering table comes first. It can give the package,
   the packing, the temperature range and the state (`ACTIVE`, `NRND`).
2. Some datasheets give only a scheme of their codes, with an example. Then
   `get_ordering_codes` gives no list. Find the ordering information with
   `search_text("ordering")`, and read it with `get_page_image`.
3. A code of the datasheet is not always sold. `search_distributors(mpn)` gives the
   concrete MPNs that a distributor has, with lifecycle state and stock. See
   [distributors.md](distributors.md).

When a project part is probably a base part, `project_add_part` gives a note. The
note gives the codes that the datasheet text has, and the call that changes the
part, for example
`project_update_part(mpn='TLV755P', new_mpn='TLV75533PDBVR', base='TLV755P')`.

A datasheet can cover more parts, for example `ADC34J22`, `ADC34J23` and `ADC34J24`.
Give one of them as `part` and the others as `variants` to `add_document`. The store
then links the datasheet to each variant.

## Search links

When the store has no datasheet and a distributor gives none, `part_links(mpn)` gives
search links:

- Datasheet, price and stock: DigiKey, Mouser, Farnell, LCSC, Octopart, Findchips.
- Symbol, footprint and 3D model: Component Search Engine, SnapMagic, Ultra
  Librarian.

datasheet-lens does not open these pages. The agent opens one with a web tool, or
gives the links to the user. For a datasheet that it finds, the agent calls
`add_document` with the URL.

## Example

The user asks: "What is the maximum supply voltage of the OPA2134, and what is the
offset voltage at 25 °C?" The values in this example are placeholders.

```text
agent:  find_facts(part="OPA2134")
server: No facts.

agent:  list_documents(part="OPA2134")
server: 3f9c0a1b2c4d  35 pages  opa2134.pdf  (datasheet)

agent:  get_index(document="3f9c0a1b2c4d")
server: # opa2134.pdf
        ## Outline
        - 1 Features (p.1)
        ...
        - 6.1 Absolute Maximum Ratings (p.4)
        - 6.5 Electrical Characteristics (p.5)
        ## Tables and figures
        ...

agent:  search_text(text="Supply voltage", document="3f9c0a1b2c4d", mode="word")
server: 3f9c0a1b2c4d p.4 p4.T1: Supply voltage, VS = (V+) – (V–)  ...
        3f9c0a1b2c4d p.5 p5.T1: ...

agent:  get_page_image(document="3f9c0a1b2c4d", element="p4.T1")
server: 3f9c0a1b2c4d page 4, region [...], 412 dpi, 2400 x 610 px
        <image>

agent:  save_fact(document="3f9c0a1b2c4d", page=4, element="p4.T1",
                  subject="Supply voltage maximum", value="<value>", unit="V",
                  column="MAX", model="<model ID>")
server: fact #1 saved (unchecked)

agent:  get_page_map(document="3f9c0a1b2c4d", page=5)
server: p5.H1     section_header [...]  6.5 Electrical Characteristics
        p5.T1     table          [...]  ...
        + 4 text elements (p5.P1 … p5.P4); ...

agent:  get_page_image(document="3f9c0a1b2c4d", element="p5.T1")
server: 3f9c0a1b2c4d page 5, region [...], 300 dpi, ...
        <image>

agent:  save_fact(document="3f9c0a1b2c4d", page=5, element="p5.T1",
                  subject="Input offset voltage", value="<value>", unit="mV",
                  column="TYP", conditions="TA = 25 °C", model="<model ID>")
server: fact #2 saved (unchecked)
```

The agent then gives the two values to the user, with the page and the table of
each value. A later session finds them with `find_facts(part="OPA2134")`.
