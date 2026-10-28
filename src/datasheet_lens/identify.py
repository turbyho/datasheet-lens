"""Proposal of the part (MPN and manufacturer) of a document, from the PDF.

The sources are the title in the PDF metadata, the text of page 1 and the file
name. A proposal is only a proposal: the user or the model confirms it.

An MPN candidate is a token with letters and digits. Its score:
- in the title: 2
- on page 1: 1
- a token of the file name: 1
- a longer token of the file name holds it (an orderable code such as
  SN74LV1T34DBVRG4 holds the base MPN SN74LV1T34): 1
- the file name is the MPN (lm20144-q1.pdf): 1
A family name with a lower-case "x" (ADC34J2x) gets no points from the title
and page 1.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

from .pdflock import pdfium_locked
from .store import Document, Store

# No "/" in a token: "AD8605/AD8606/AD8608" is a list of three MPNs.
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-+.#]*[A-Za-z0-9+#]")
# Tokens that look like an MPN but are not one: revisions, TI literature numbers
# (SLAS123A), values with units (12-Bit), packages and standards. Real MPNs such
# as ADXL345, ISO7741 and USB2514 must not match.
NOT_MPN_RE = re.compile(r"(?i)^(rev[a-z0-9.]*|s[lbncwp][a-z]{2}\d{3}[a-z]?|\d+(\.\d+)?-?[a-z]*|aec-q\d+|"
                        r"sot-?\d+|soic-?\d+|qfn-?\d+|tssop-?\d+|msop-?\d+|dfn-?\d+|bga-?\d+|"
                        r"ieee\d+|jesd\d+[a-z]?)$")

# Canonical name: names and abbreviations in a PDF or a file name (upper case).
MANUFACTURERS = {
    "Texas Instruments": ("TEXAS INSTRUMENTS", "TEXAS_INSTRUMENTS", "TI.COM"),
    "Analog Devices": ("ANALOG DEVICES", "ANALOG.COM", "ANALOG_DEVICES"),
    "Maxim Integrated": ("MAXIM INTEGRATED", "MAXIM_INTEGRATED"),
    "STMicroelectronics": ("STMICROELECTRONICS", "ST.COM"),
    "Microchip": ("MICROCHIP TECHNOLOGY", "MICROCHIP"),
    "NXP": ("NXP SEMICONDUCTORS", "NXP.COM", "NXP"),
    "Nordic Semiconductor": ("NORDIC SEMICONDUCTOR", "NORDICSEMI"),
    "Infineon": ("INFINEON TECHNOLOGIES", "INFINEON"),
    "onsemi": ("ON SEMICONDUCTOR", "ONSEMI"),
    "Renesas": ("RENESAS ELECTRONICS", "RENESAS"),
    "Micro Crystal": ("MICRO CRYSTAL", "MICROCRYSTAL"),
    "Espressif": ("ESPRESSIF SYSTEMS", "ESPRESSIF"),
    "ROHM": ("ROHM SEMICONDUCTOR", "ROHM CO"),
    "Vishay": ("VISHAY INTERTECHNOLOGY", "VISHAY"),
    "Toshiba": ("TOSHIBA ELECTRONIC", "TOSHIBA"),
    "Diodes Incorporated": ("DIODES INCORPORATED", "DIODES INC"),
    "Nexperia": ("NEXPERIA",),
    "Silicon Labs": ("SILICON LABORATORIES", "SILICON LABS", "SILABS"),
    "Murata": ("MURATA MANUFACTURING", "MURATA"),
    "TDK": ("TDK CORPORATION", "TDK-LAMBDA"),
    "Littelfuse": ("LITTELFUSE",),
    "Monolithic Power Systems": ("MONOLITHIC POWER", "MPSIC.COM"),
    "Richtek": ("RICHTEK",),
    "Bosch Sensortec": ("BOSCH SENSORTEC",),
    "Sensirion": ("SENSIRION",),
    "u-blox": ("U-BLOX",),
    "Würth Elektronik": ("WÜRTH ELEKTRONIK", "WURTH ELEKTRONIK", "WE-ONLINE"),
}


@dataclass
class Proposal:
    mpn: str = ""
    manufacturer: str = ""
    sources: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # other MPN candidates, best first
    # The parts that the datasheet covers (mpn and the others), when there are
    # more than one: ADC34J22, ADC34J23, ADC34J24, ADC34J25.
    variants: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _is_mpn(token: str) -> bool:
    return (len(token) >= 4 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
            and not NOT_MPN_RE.match(token))


def _manufacturer(store: Store, texts: list[str]) -> str:
    """The manufacturer that the texts name most often. The spelling of a part in
    the store wins (for example "TI" for Texas Instruments)."""
    # Non-overlapping matches of whole words, the longest alias first:
    # "NXP SEMICONDUCTORS" counts one time, and "NXP" in "NXPXX" does not count.
    owner = {alias: name for name, aliases in MANUFACTURERS.items() for alias in aliases}
    pattern = re.compile(r"(?<![A-Z0-9])(" + "|".join(re.escape(a) for a in sorted(owner, key=len, reverse=True))
                         + r")(?![A-Z0-9])")
    counts: dict[str, int] = {}
    for text in texts:
        for alias in pattern.findall(text.upper().replace("_", " ")):
            counts[owner[alias]] = counts.get(owner[alias], 0) + 1
    if not counts:
        return ""
    name = max(counts, key=counts.get)
    known = {r[0] for r in store.db.execute("SELECT DISTINCT manufacturer FROM parts")}
    for spelling in known:
        if spelling and (spelling.upper() in MANUFACTURERS[name] or _norm(spelling) == _norm(name)
                         or (name == "Texas Instruments" and spelling.upper() == "TI")):
            return spelling
    return name


@pdfium_locked
def propose(store: Store, doc: Document) -> Proposal:
    pdf = pdfium.PdfDocument(str(doc.path))
    try:
        meta = pdf.get_metadata_dict()
        page = pdf[0]
        textpage = page.get_textpage()
        page1 = textpage.get_text_bounded()
        textpage.close()
        page.close()
    finally:
        pdf.close()
    title = (doc.title or meta.get("Title") or "").strip()
    stem = Path(doc.filename).stem
    # The key is the normal form: "nRF54L15" (title) and "NRF54L15" (file name)
    # are one candidate, with the spelling that came first.
    scores: dict[str, float] = {}
    found: dict[str, list[str]] = {}
    spelling: dict[str, str] = {}
    order: list[str] = []

    def add(token: str, score: float, source: str) -> None:
        if not _is_mpn(token):
            return
        key = _norm(token)
        if key not in scores:
            order.append(key)
            scores[key], found[key], spelling[key] = 0, [], token
        scores[key] += score
        if source not in found[key]:
            found[key].append(source)

    def family(token: str) -> bool:
        return bool(re.search(r"\dx", token))  # ADC34J2x: a family, not one part

    # The candidates: the title, the start of page 1 (the heading), the file name.
    for token in TOKEN_RE.findall(title):
        add(token, 0 if family(token) else 2, "title")
    for token in TOKEN_RE.findall(page1[:600]):
        add(token, 0, "page 1")
    from_name = set()
    for token in TOKEN_RE.findall(stem.replace("_", " ")):
        add(token.upper(), 1, "file name")
        from_name.add(_norm(token))
    # The evidence: a candidate that page 1 holds, and a candidate that a longer
    # token of the file name holds (an orderable code holds the base MPN).
    page1_norm, stem_norm = _norm(page1), _norm(stem)
    for key in list(scores):
        if key in page1_norm and not family(spelling[key]):
            add(spelling[key], 1, "page 1")
        if key not in from_name and key in stem_norm:
            add(spelling[key], 1, "file name")
        if key == stem_norm:  # the file name is the MPN (lm20144-q1.pdf)
            add(spelling[key], 1, "file name")
    ranked = sorted((k for k in order if scores[k] > 0), key=lambda k: -scores[k])
    best = ranked[0] if ranked else ""
    variants = [spelling[k] for k in sorted(
        k for k in order if k == best or (best and _variant(spelling[best], spelling[k], found[k], k == stem_norm)))]
    return Proposal(mpn=spelling.get(best, ""),
                    manufacturer=_manufacturer(store, [title, page1[:3000], stem, meta.get("Author", ""),
                                                       meta.get("Creator", "")]),
                    sources=found.get(best, []), candidates=[spelling[k] for k in ranked[1:5]],
                    variants=variants if len(variants) > 1 else [])


def _variant(best: str, other: str, sources: list[str], file_name: bool = False) -> bool:
    """True if the candidate other is a variant of the best one. The title or
    page 1 names it, or it is the file name, and it is one of:
    - the same length, with only the last characters different (ADC34J22 and
      ADC34J24, TL071 and TL072)
    - the base with a suffix after a hyphen (LM20144 and LM20144-Q1)
    An orderable code (LM358DR, SN74LV1T34DBVRG4) is not a variant: it is
    longer, with no hyphen."""
    b, o = _norm(best), _norm(other)
    named = file_name or {"title", "page 1"} & set(sources)
    if not b or o == b or not named or re.search(r"\dx", o):
        return False
    if len(o) == len(b):
        return len(os.path.commonprefix([b, o])) >= max(4, len(b) - 2)
    shorter, longer = sorted((best.upper(), other.upper()), key=len)
    return longer.startswith(shorter + "-") and len(longer) - len(shorter) <= 4
