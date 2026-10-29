"""Search links for a part: datasheets, prices and availability, CAD files.

datasheet-lens does not read these pages (no scraping). The links help the
model and the user when the store has no datasheet, and they are the way to the
CAD files (symbol, footprint, 3D model), which no site gives through an open API.

The URL formats were checked on 2026-10-29 with TPS7A0233PYCHR: Mouser, Farnell,
LCSC and Findchips gave the search page. Component Search Engine, SnapMagic,
Octopart and DigiKey refused a request with no browser (HTTP 403): their formats
are the formats of their search forms. The part search of Ultra Librarian needs
a login.
"""

from __future__ import annotations

from urllib.parse import quote

# (group, site, URL with {mpn}, note)
SITES = (
    ("datasheet, price, stock", "DigiKey", "https://www.digikey.com/en/products/result?keywords={mpn}", ""),
    ("datasheet, price, stock", "Mouser", "https://www.mouser.com/c/?q={mpn}", ""),
    ("datasheet, price, stock", "Farnell", "https://www.farnell.com/search?st={mpn}", ""),
    ("datasheet, price, stock", "LCSC", "https://www.lcsc.com/search?q={mpn}", ""),
    ("datasheet, price, stock", "Octopart", "https://octopart.com/search?q={mpn}", "many distributors"),
    ("datasheet, price, stock", "Findchips", "https://www.findchips.com/search/{mpn}",
     "stock and price at many distributors"),
    ("symbol, footprint, 3D model", "Component Search Engine", "https://componentsearchengine.com/search?term={mpn}",
     "a free account for the download"),
    ("symbol, footprint, 3D model", "SnapMagic", "https://www.snapeda.com/search/?q={mpn}&SearchType=parts",
     "a free account for the download"),
    ("symbol, footprint, 3D model", "Ultra Librarian", "https://app.ultralibrarian.com/search?queryText={mpn}",
     "the search needs a free account"),
)


def part_links(mpn: str) -> list[tuple[str, str, str, str]]:
    """The search links for an MPN: (group, site, URL, note)."""
    key = quote(mpn.strip(), safe="")
    return [(group, site, url.format(mpn=key), note) for group, site, url, note in SITES]


def format_links(mpn: str) -> list[str]:
    """The search links as lines, one group after the other."""
    lines, group = [], ""
    for g, site, url, note in part_links(mpn):
        if g != group:
            lines.append(f"{g}:")
            group = g
        lines.append(f"  {site}: {url}" + (f" ({note})" if note else ""))
    return lines
