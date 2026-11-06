"""Errors of datasheet-lens."""


class LensError(ValueError):
    """A wrong input or a state that the user can repair (a page that is not in
    the document, a file that cannot be read). The CLI shows it as one line."""


class Unavailable(LensError):
    """A distributor API cannot answer now (no connection, rate limit, server
    error): the next requests fail too."""


class Choose(LensError):
    """A text fits more choices (for example more categories): the user
    selects one. The CLI and the MCP tools give the lines as a normal result,
    not as an error."""

    def __init__(self, lines: list[str]):
        super().__init__("\n".join(lines))
        self.lines = lines
