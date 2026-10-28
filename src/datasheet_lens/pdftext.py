"""Text lines with positions from the PDF text layer (pypdfium2).

pdfium gives the text in rectangles: one rectangle for each run of text on a line.
This module joins the rectangles of one line, from left to right, into a line with
its bbox. A large gap between two rectangles becomes three spaces, as a column gap
in a layout text. Coordinates are in pt, origin top left.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c


@dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    # (offset in text, x0 in pt) of each run, to find the x of a text position
    runs: list[tuple[int, float]]

    def x_at(self, offset: int) -> float:
        """The x position of the run that holds the character at offset."""
        x = self.x0
        for start, run_x in self.runs:
            if start > offset:
                break
            x = run_x
        return x


@dataclass
class PageText:
    page: int
    width: float
    height: float
    lines: list[Line]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


WORD_GAP = 0.25     # gap / char height: a space between two words
COLUMN_GAP = 1.5    # gap / char height: a column gap (three spaces)


def page_lines(page: pdfium.PdfPage, number: int) -> PageText:
    """The lines of a page, from the char positions only (as a layout text): the
    chars are grouped by their vertical center, and sorted from left to right in a
    line. The order of the chars in the PDF content does not matter."""
    width, height = page.get_size()
    textpage = page.get_textpage()
    count = textpage.count_chars()
    chars = []
    space_before = False
    for i in range(count):
        # One char for each index. get_text_range() expands ligatures ("ﬁ" to "fi"),
        # and then its string index is not the char index of get_charbox().
        code = pdfium_c.FPDFText_GetUnicode(textpage.raw, i)
        ch = chr(code) if code else ""
        if 0xFB00 <= code <= 0xFB06:
            # Only the Latin ligatures ("ﬁ" to "fi"). A full NFKC would also change
            # superscripts ("²" to "2"), "µ", "Ω" and "…".
            ch = unicodedata.normalize("NFKC", ch)
        if not ch or ch.isspace():
            space_before = True
            continue
        # The loose box has the height of the font, the same for all chars of a
        # line. The tight box (glyph outline) splits a line by ascenders.
        left, bottom, right, top = textpage.get_charbox(i, loose=True)
        if right <= left and top <= bottom:
            continue  # a char that pdfium made (no box)
        # pdfium puts a space where it finds a word gap; keep it as a flag, because
        # the loose boxes of some fonts leave no measurable gap between words.
        chars.append((left, height - top, right, height - bottom, ch, space_before))
        space_before = False
    textpage.close()

    chars.sort(key=lambda c: ((c[1] + c[3]) / 2, c[0]))
    groups: list[list] = []
    for c in chars:
        center, size = (c[1] + c[3]) / 2, max(1.0, c[3] - c[1])
        if groups and abs(groups[-1][0] - center) <= 0.45 * max(size, groups[-1][1]):
            groups[-1][2].append(c)
        else:
            groups.append([center, size, [c]])

    lines = []
    for _center, _size, members in groups:
        members.sort(key=lambda c: c[0])
        out, runs, prev = "", [], None
        for c in members:
            size = max(1.0, c[3] - c[1])
            if prev is not None:
                gap = c[0] - prev[2]
                if gap > COLUMN_GAP * size:
                    out += "   "
                elif gap > WORD_GAP * size or c[5]:
                    out += " "
            if prev is None or out.endswith(" "):
                runs.append((len(out), c[0]))
            out += c[4]
            prev = c
        lines.append(Line(out, min(c[0] for c in members), min(c[1] for c in members),
                          max(c[2] for c in members), max(c[3] for c in members), runs))
    return PageText(number, width, height, lines)


def raster_min_dpi(page: pdfium.PdfPage, min_area_pt2: float = 6000.0) -> float | None:
    """Give the lowest effective dpi of the raster images of a page that are larger
    than min_area_pt2 (logos and icons are smaller), or None."""
    lowest = None
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=5):
        w_px, h_px = obj.get_px_size()
        left, bottom, right, top = obj.get_bounds()
        w_pt, h_pt = right - left, top - bottom
        if w_pt <= 0 or h_pt <= 0 or w_pt * h_pt < min_area_pt2:
            continue
        dpi = min(w_px / (w_pt / 72), h_px / (h_pt / 72))
        lowest = dpi if lowest is None else min(lowest, dpi)
    return lowest
