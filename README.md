# datasheet-lens

datasheet-lens gives the content of component datasheets to an LLM agent, for
electronics design and firmware work.

The tool does not convert a datasheet to text. A conversion makes errors that look
correct: lost superscripts, merged table rows, shifted columns, wrong values from
graphs. datasheet-lens builds an index to find the content, and gives the original
pages to the model on demand. The model reads only the parts that the work needs.

Status: in development (version 0.1). Not ready for use.

## Method

The agent goes into a document step by step:

1. The index: outline and captions of the document.
2. The outline of one section.
3. The page map: headings, tables, figures and formulas of one page.
4. The full text of one element.
5. The image of one element, a region or a page, or the pages as a PDF.

## License

MIT. See `LICENSE`.
