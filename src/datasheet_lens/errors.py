"""Errors of datasheet-lens."""


class LensError(ValueError):
    """A wrong input or a state that the user can repair (a page that is not in
    the document, a file that cannot be read). The CLI shows it as one line."""
