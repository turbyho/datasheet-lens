# MCP server

The MCP server gives the functions of datasheet-lens to an LLM agent. This document
tells how to register the server, and gives a list of all its tools. For the method
that an agent uses, see [agent-workflow.md](agent-workflow.md).

## The command

The MCP server is the command `datasheet-lens-mcp`. It has no arguments. It uses the
stdio transport: the MCP client starts the command and sends the requests on stdin.
The command `datasheet-lens mcp` starts the same server.

The server uses the central store of the user (`~/.datasheet-lens`, or the folder
in the environment variable `DATASHEET_LENS_HOME`). The project tools find the
project file `datasheet-lens.toml` in the working folder of the server or in a
parent folder. Each project tool also has the parameter `path` for a different
project folder.

## Register the server

### Claude Code

Register the server one time for all projects of the user:

```sh
claude mcp add --scope user datasheet-lens -- datasheet-lens-mcp
```

The project tools find the project file in the working folder of the server.
When that folder is not in the project, the agent must give the parameter `path`.

### Other MCP clients

Most clients have a JSON configuration with a list of servers. Add the command
`datasheet-lens-mcp` with no arguments:

```json
{
  "mcpServers": {
    "datasheet-lens": {
      "command": "datasheet-lens-mcp",
      "args": []
    }
  }
}
```

If the client does not find the command, give its full path. `pipx` puts it into
`~/.local/bin` on Linux and macOS. To use a different store folder, add
`"env": {"DATASHEET_LENS_HOME": "/path/to/store"}` to the entry.

## Server instructions

The server sends instructions to the agent when the session starts. In short, they
tell the agent:

- Read each value from the original page (`get_page_image` or `get_pdf_pages`). The
  text layer can lose signs, units, subscripts and the structure of tables.
- Keep the project file `datasheet-lens.toml` equal to the parts of the CAD project.
  Get the parts from the CAD tool, compare them with `project_check`, add the
  missing parts, tell the user about different and extra parts, then run
  `project_sync` and `project_check` again. Then run `project_availability`.
- Use `add_document` only for the documents of the parts. The store identifies a
  document by its SHA-256, not by its file name.
- Find the content step by step: the document, `find_facts`, `get_index`,
  `search_text`, `get_page_map`, `get_page_image`, then `save_fact`.

Some MCP clients cut long server instructions. Claude Code cuts them at about 4096
characters. A test keeps the instructions of datasheet-lens below 3800 characters.
The full description of each tool is in its tool description, which the client
does not cut.

## Tools

The column "Changes" tells if a tool changes the store, the project file or the
project folder. The other tools only read.

### Documents

| Tool | Function | Main parameters | Changes |
|---|---|---|---|
| `list_parts` | Give the parts in the store, with the manufacturer and the number of documents. | none | no |
| `list_documents` | Give the documents in the store, or the documents of one part. A concrete part also gets the documents of its base part. | `part` | no |
| `add_document` | Copy or download a PDF into the store, build its index, start its element map and link it to a part. Without `part`, propose an MPN from the PDF. | `path` (file or URL), `part`, `manufacturer`, `role`, `variants` | store |
| `get_ordering_codes` | Give the orderable codes of a datasheet from its text layer, with page and line. | `document` | no |

### Reading

| Tool | Function | Main parameters | Changes |
|---|---|---|---|
| `get_index` | Give the outline, the table and figure captions with pages, and the pages with no text layer or a low image resolution. | `document` | no |
| `get_outline` | Give the outline entries under one section of a large document. | `document`, `section` | no |
| `search_text` | Find a text in one document or in all documents of a part. Give the page, the element or region, and the line of each hit. | `text`, `document` or `part`, `mode`, `max_lines`, `repeated` | no |
| `get_page_map` | Give the elements of one page: ID, type, region and text. | `document`, `page`, `all_elements` | no |
| `get_element_text` | Give the full text of elements from the text layer. | `document`, `element_ids` | no |
| `list_items` | Give all tables, figures and captions of a document. | `document`, `kind` | no |
| `get_page_image` | Give a PNG image of an element, a caption, a region or a full page. | `document`, `page`, `element`, `caption`, `bbox`, `max_px` | no |
| `get_pdf_pages` | Give some pages as a new PDF file (50 pages or fewer). | `document`, `pages` | no |

### Facts

| Tool | Function | Main parameters | Changes |
|---|---|---|---|
| `find_facts` | Find values that a session read before. With `part`, also the facts of its base part. | `query`, `document`, `part`, `limit` | no |
| `save_fact` | Save a value that the agent read from the original, with its source. | `document`, `page`, `subject`, `value`, `unit`, `conditions`, `column`, `element` or `bbox`, `note`, `model`, `part` | store |
| `set_fact_status` | Set the state of a fact: `unchecked`, `checked` or `disputed`. | `fact_id`, `status`, `note` | store |

### Project

| Tool | Function | Main parameters | Changes |
|---|---|---|---|
| `project_status` | Give the parts and documents of the project file, and the state of the copies. | `path` | no |
| `project_check` | Compare the project file with the parts of the CAD project. Give the conflicts in the CAD project, and the same, different, missing and extra parts, with the next call for each. | `parts` (list of `mpn`, `value`, `datasheet`, `reference`; the fields as they are), `path` | no |
| `project_add_part` | Add a part to the project file, with the documents that the store links to it. | `mpn`, `base`, `document`, `manufacturer`, `category`, `role`, `path` | store, project file |
| `project_update_part` | Change the MPN, base part, category or manufacturer of a project part in place. | `mpn`, `new_mpn`, `base`, `category`, `new_manufacturer`, `manufacturer`, `path` | store, project file |
| `project_remove_part` | Remove a part from the project, and delete its copies that no other part uses. | `mpn`, `manufacturer`, `delete_pinned`, `dry_run`, `path` | project file, project folder |
| `project_remove_document` | Remove a document from the parts of the project. The parts stay. | `document`, `mpn`, `manufacturer`, `delete_pinned`, `dry_run`, `path` | project file, project folder |
| `project_sync` | Copy the documents into the project folder, move copies to the subfolders of the layout, and import new PDF files of the folder. | `path`, `restore` | store, project file, project folder |

### Distributors and links

| Tool | Function | Main parameters | Changes |
|---|---|---|---|
| `search_distributors` | Give the orderable parts at the enabled distributors: lifecycle state, stock, prices, package and datasheet URL. | `mpn`, `exact`, `path` | no |
| `project_availability` | Give the lifecycle state and the stock of each project part, with a warning for each problem. | `path` | no |
| `distributor_status` | Give the enabled distributors and if each one has an API key (no key values). | `path` | no |
| `part_links` | Give search links for a part: datasheet, price, stock, symbol, footprint and 3D model. | `mpn` | no |

For the distributor tools, see [distributors.md](distributors.md). For the project
tools, see [projects.md](projects.md).

## Document references

The tools identify a document with a reference: the first 12 characters of its
SHA-256. `list_documents`, `project_status` and `add_document` give it. A longer
SHA-256 prefix (8 characters or more) and the numeric document ID of the store also
operate.

## The element map build in the background

The element map of a large document can take minutes with docling. This time is
longer than the timeout of most MCP clients. Thus the server builds the element map
in a background thread:

1. `add_document` builds the index at once, then starts the element map build.
2. `add_document` waits up to 20 seconds for the build. Then it gives its result
   with the state of the build.
3. Until the map is complete, `get_page_map`, `get_element_text`, `list_items` and
   `get_page_image` with `element` or `caption` give an error that tells the state.
   The index, `search_text` and `get_page_image` with a page or a region operate
   during the build.
4. `project_sync` builds the missing maps of the project documents, one after the
   other, in one background thread.

A tool that needs the element map starts the build when the document has no map and
no build runs. A build that failed starts again only from `add_document`.

With the docling backend, the build runs in a child process (the CLI command
`elements`). A crash of docling thus does not stop the server. When the server
stops, it stops its child processes. The next request of the map then starts the
build again.

The build state is in the server process only. Two server processes (two sessions)
can build the same map at the same time.

## Errors

A wrong input, or a state that the user can repair, gives a tool error with one
line of text. The text tells the agent what to do, for example `document not found:
1a2b3c (use list_documents)`.
