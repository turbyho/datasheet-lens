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

from .errors import LensError
from .makers import MANUFACTURERS
from .pdflock import pdfium_locked
from .store import Document, Store

# No "/" in a token: "AD8605/AD8606/AD8608" is a list of three MPNs.
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-+.#]*[A-Za-z0-9+#]")
# A package: SOT23-5, X2SON, LGA-4, DSBGA, WLCSP, MFF2 (the SIM package).
PACKAGE = (r"[a-z]{0,2}\d?(son|qfn|dfn|bga|lga|csp|sop|ssop|soic|qfp|sot|dip|mff)-?\d*(-\d+)?"
           r"|(to|sc)-\d+(-\d+)?")  # TO-92 and SC-70, but SC4215 is an MPN
# Tokens that look like an MPN but are not one: revisions and versions (v1.4),
# TI literature numbers (SLAS123A), values with units (12-Bit, 1.65V-3.6V,
# 98.01mm2), sizes (10x10mm), numbered text (1.This), packages, standards and
# document numbers (DS000-3159232, 3361126). Real MPNs such as ADXL345,
# ISO7741, USB2514, 2N7002, DS1307 and V23079 must not match.
NOT_MPN_RE = re.compile(r"(?i)^(rev[a-z0-9.]*|v\d+(\.\d+)+|s[lbncwp][a-z]{2}\d{3}[a-z]?|"
                        r"\d+(\.\d+)?[a-z]*(-\d*(\.\d+)?[a-z]*)?|\d+(\.\d+)?(mm|cm|um|m|in)[23]|"
                        r"\d+(\.\d+)?[x*]\d+(\.\d+)?[a-z]*|\d+\.[a-z]{2,}|aec-q\d+|am\d(\.\d+)?g?|"
                        rf"{PACKAGE}|ieee\d+|jesd\d+[a-z]?|ds\d{{8}}[a-z]?|ds\d+-\d{{4,}})$")
# Words of a document name, not of an MPN: "MCP1827-Data-Sheet-DS20002001",
# "GD25LE64E-Rev1.3", "DS-AT25QL641-130", "BST-BMI323-DS000-10" (a DS number
# after a hyphen: DS1307 is an MPN).
DOC_WORD_RE = re.compile(r"(?i)(^|-)(data|sheet|datasheet|page|manual|errata|rev[\d.]*|ds)(-|$)|-ds\d+(-|$)")

# The manufacturer of an MPN prefix, when the text does not name it (the name
# is only in the logo): nRF52805 is from Nordic Semiconductor.
MPN_PREFIXES = {
    "NRF": "Nordic Semiconductor",
    "GD25": "GigaDevice",
    "GD32": "GigaDevice",
    "MX25": "Macronix",
    "MX66": "Macronix",
    "RV-": "Micro Crystal",
    "STM32": "STMicroelectronics",
    "ESP32": "Espressif",
}
OWNER = {alias: name for name, aliases in MANUFACTURERS.items() for alias in aliases}
# Non-overlapping matches of whole words, the longest alias first: "NXP
# SEMICONDUCTORS" counts one time, and "NXP" in "NXPXX" does not count.
ALIAS_RE = re.compile(r"(?<![A-Z0-9])(" + "|".join(re.escape(a) for a in sorted(OWNER, key=len, reverse=True))
                      + r")(?![A-Z0-9])")


@dataclass
class Proposal:
    """The part that a PDF proposes: the MPN, the manufacturer, the sources of the MPN
    (title, page 1, file name), other candidates and the variants."""
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
    """True if a token can be an MPN: letters and digits, 4 characters or more, and
    not a revision, a literature number, a value with a unit, a package or a standard."""
    return (len(token) >= 4 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
            and not NOT_MPN_RE.match(token) and not DOC_WORD_RE.search(token))


def _manufacturer(texts: list[str], mpn: str = "") -> str:
    """The manufacturer that the texts name most often, else the manufacturer of
    the prefix of mpn. Any spelling is good for the store: its manufacturer
    registry knows the names of one manufacturer (makers.py)."""
    counts: dict[str, int] = {}
    for text in texts:
        for alias in ALIAS_RE.findall(text.upper().replace("_", " ")):
            counts[OWNER[alias]] = counts.get(OWNER[alias], 0) + 1
    if counts:
        return max(counts, key=counts.get)
    return next((m for p, m in MPN_PREFIXES.items() if mpn.upper().startswith(p)), "")


@pdfium_locked
def propose(doc: Document) -> Proposal:
    """Propose the part of a document from its PDF (see the module docstring). It is
    only a proposal: the user or the model confirms it."""
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
        token = re.sub(r"(?i)\.pdf$", "", token)  # a title that is a file name
        if not _is_mpn(token) or token.upper() in OWNER:  # 1GLOBAL is a manufacturer
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
    # No rule for short forms such as "MCP1827/S": the manufacturers write them in
    # different ways ("MCP6001/1R/1U/2/4", "ADS1113/4/5"), and a rule gives MPNs
    # that do not exist. The model reads such a list from the original.
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
                    manufacturer=_manufacturer([title, page1[:3000], stem, meta.get("Author", ""),
                                                meta.get("Creator", "")], spelling.get(best, "")),
                    sources=found.get(best, []), candidates=[spelling[k] for k in ranked[1:5]],
                    variants=variants if len(variants) > 1 else [])


def project_base_note(store: Store, part: dict, how: str) -> str:
    """The note for a project part that is probably not a concrete orderable MPN,
    or "": a part with no base part that is probably the base part of its
    datasheet, or a part with a base part that is probably not complete
    (TMP112D0 of TMP112D0IDPWR). how tells how to give the concrete MPN, with
    {mpn}, {code} and {base} in it. An error of the hint gives no note: the hint
    is optional."""
    rows = store.match_parts(part["mpn"], part["manufacturer"])
    if len(rows) != 1:
        return ""
    base = part.get("base", "")
    if base.lower() == part["mpn"].lower():  # not a concrete part
        base = ""
    try:
        return base_note(store, rows[0]["id"], part["mpn"], how, base)
    except (LensError, OSError, pdfium.PdfiumError):
        return ""


def base_note(store: Store, part_id: int, mpn: str, how: str, base: str = "") -> str:
    """A note for a part that is probably a base part, or "" (see base_hint).
    With base, mpn is a concrete part: a note when the text has longer codes."""
    probable, codes = base_hint(store, part_id, mpn, concrete=bool(base))
    if not probable:
        return ""
    check = ("Read the ordering information of the original, and check with a distributor that the code is "
             "sold. Then: " + how.format(code=codes[0][0] if codes else "<CODE>", base=base or mpn, mpn=mpn))
    listed = ", ".join(f"{c} ({p})" for c, p in codes)
    if base:
        return (f"note: {mpn} is probably not a complete orderable MPN (package, packing): the datasheet text "
                f"has longer codes: {listed}. {check}")
    found = (f"; orderable codes in the datasheet text: {listed}" if codes
             else "; the datasheet gives only a scheme of its codes, with an example")
    return (f"note: {mpn} is probably the base part of its datasheet. The BOM needs the concrete orderable "
            f"MPN (package, voltage, grade, packing){found}. {check}")


# A token after the MPN that is not an orderable code of the part: an
# evaluation board or a kit.
NOT_ORDERABLE_RE = re.compile(r"(?i)(EVM|EVK|EVB|EVAL|DEMO|KIT|BOARD)")
# The words of a line of an ordering table (not "active-low").
ORDER_WORDS = r"\b(active|obsolete|nrnd|preview|tape|reel|t&r|tube|rail|tray)\b(?!-)"
# The example of a scheme of codes: "Example: RV-8263-C8 TA QC", "PART NUMBERING
# GUIDE: Example ECS-320-10-48-CKY-TR" (not "Example: gyroscope"). The code
# starts as the MPN, or the line names the numbering or the ordering.
SCHEME_RE = re.compile(r"(?i:\bexample\b)[\s:]+([A-Z][A-Z0-9]*-[A-Z0-9][A-Z0-9\-]*)")
SCHEME_WORDS_RE = re.compile(r"(?i)numbering|ordering|part number")
# A concrete part with a longer code: TMP112D0 of TMP112D0IDPWR. A shorter
# difference is a packing (nRF9151-LACA-R and -R7, SN74LV1T34DBVR and -G4,
# MX25R8035FM1IL0 and -TR).
CONCRETE_EXTRA = 3


def base_hint(store: Store, part_id: int, mpn: str, concrete: bool = False) -> tuple[bool, list[tuple[str, str]]]:
    """Is mpn probably the base part of its datasheet, and not a concrete
    orderable MPN? Give (probably a base part, orderable codes in the text with
    their page). The codes are the tokens of the text layer that start with mpn
    and go on (ADC34J24 -> ADC34J24IRGZR on page 81, MCP1827 -> MCP1827-ADJE/ET).
    mpn is a concrete code when a line of an ordering table starts with it.
    Else, with no codes, mpn is probably a base part when it is the MPN that the
    PDF proposes and the text gives an example of a scheme of codes. It is a hint
    only: a code of the scheme is not always sold. A distributor tells which
    codes exist.
    concrete: mpn has a base part. Only codes that are CONCRETE_EXTRA characters
    longer count, and the scheme rule does not apply.
    Not a code: a document number (BST-BMI323-DS000-10, GD25LE64E-Rev1.3), a
    footnote mark of one digit directly after mpn, when mpn ends with a digit
    (RR123-1H02-612 with the mark 1; a mark after a longer code stays in it),
    a placeholder (TMP112DX) and a different part of the same datasheet (a variant)."""
    docs = [r for r in store.documents_of_part(part_id) if r["role"] == "datasheet"]
    if not docs or not mpn.strip():
        return False, []
    key = mpn.strip()
    # The variants: the parts of the datasheet that are not concrete parts.
    siblings = {p["mpn"].upper() for d in docs for p in store.parts_of_document(d["id"]) if p["base_id"] is None}
    # Not after a hyphen: "DS-AT25QL641-130H" is a document number.
    rx = re.compile(rf"(?<![A-Za-z0-9\-]){re.escape(key)}[A-Za-z0-9\-][A-Za-z0-9/\-]{{0,14}}", re.I)
    order_line = re.compile(rf"(?im)^\s*{re.escape(key)}(?![A-Za-z0-9/\-]).*{ORDER_WORDS}")
    codes: dict[str, str] = {}
    for row in store.db.execute(
            f"SELECT document_id, page, text FROM pages WHERE document_id IN ({','.join('?' * len(docs))})"
            " AND instr(ulower(text), ?) > 0 ORDER BY document_id, page", (*[d["id"] for d in docs], key.lower())):
        if order_line.search(row["text"]):
            return False, []
        where = f"p.{row['page']}" if len(docs) == 1 else f"{store.document(row['document_id']).sha256[:12]} p.{row['page']}"
        for code in rx.findall(row["text"]):
            code = code.rstrip("-/").upper()
            suffix = code[len(key):]
            if (not suffix or NOT_ORDERABLE_RE.search(suffix) or DOC_WORD_RE.search(code)
                    or re.fullmatch(r"X+", suffix) or (key[-1].isdigit() and re.fullmatch(r"\d", suffix))
                    or (concrete and len(suffix.lstrip("-/")) < CONCRETE_EXTRA) or code in siblings
                    or code in codes):
                continue
            codes[code] = where
    if codes:
        return True, list(codes.items())[:8]
    if concrete:
        return False, []
    family, codes = _family_codes(store, docs, key)
    if family:
        return True, codes
    proposed = {_norm(propose(store.document(d["id"])).mpn) for d in docs}
    if _norm(key) not in proposed:
        return False, []
    rows = store.db.execute(
        f"SELECT text FROM pages WHERE document_id IN ({','.join('?' * len(docs))})"
        " AND instr(ulower(text), 'example') > 0", [d["id"] for d in docs])
    return any(m and (m.group(1)[:2].upper() == key[:2].upper() or SCHEME_WORDS_RE.search(line))
               for r in rows for line in r["text"].splitlines() for m in [SCHEME_RE.search(line)]), []


def _family_codes(store: Store, docs: list, key: str, limit: int = 8) -> tuple[bool, list[tuple[str, str]]]:
    """The codes of a family name with placeholders (a lower-case x): TI names
    TLV755P "TLV755xxP", and its codes are TLV75533PDBVR and others. Give (the
    text has the family name, the first limit codes with their page)."""
    if len(key) < 4:
        return False, []
    ids = [d["id"] for d in docs]
    rows = store.db.execute(
        f"SELECT document_id, page, text FROM pages WHERE document_id IN ({','.join('?' * len(ids))})"
        " AND instr(ulower(text), ?) > 0 ORDER BY document_id, page", (*ids, key[:4].lower())).fetchall()
    token_rx = re.compile(rf"(?<![A-Za-z0-9\-]){re.escape(key[:3])}[A-Za-z0-9\-]*x[A-Za-z0-9\-]*", re.I)
    family = ""
    for row in rows:
        family = next((t for t in token_rx.findall(row["text"])
                       if "x" in t and re.sub(r"x+", "", t).upper() == key.upper()), "")
        if family:
            break
    if not family:
        return False, []
    pattern = "".join(f"[0-9A-Z]{{{len(p)}}}" if p.startswith("x") else re.escape(p.upper())
                      for p in re.split(r"(x+)", family) if p)
    code_rx = re.compile(rf"(?<![A-Za-z0-9\-]){pattern}[A-Z0-9][A-Z0-9/\-]{{0,14}}")
    codes: dict[str, str] = {}
    for row in rows:
        where = f"p.{row['page']}" if len(ids) == 1 else f"{store.document(row['document_id']).sha256[:12]} p.{row['page']}"
        for code in code_rx.findall(row["text"]):
            code = code.rstrip("-/")
            if not NOT_ORDERABLE_RE.search(code) and code not in codes:
                codes[code] = where
    return True, list(codes.items())[:limit]


MAX_CODES = 100


def ordering_codes(store: Store, doc_id: int) -> list[tuple[str, int, str, bool]]:
    """The orderable codes in the text layer of a document: (code, page, line,
    table). A code starts with a part of the document (ADC34J24 ->
    ADC34J24IRGZR), or it fits a family name with placeholders (TLV755xxP ->
    TLV75533PDBVR). table: the line starts with the code, as a line of an
    ordering table does; it can give the package, the packing, the temperature
    and the state. These codes come first. A list such as TMP112A/B/D/N is not
    a code, and see base_hint. The parts of the document are the parts that the
    store links to it, or else the proposal from the PDF."""
    keys = sorted({p["mpn"] for p in store.parts_of_document(doc_id) if p["base_id"] is None}, key=len)
    if not keys:
        proposal = propose(store.document(doc_id)).mpn
        keys = [proposal] if proposal else []
    order_rx = re.compile(ORDER_WORDS, re.I)
    variants = {k.upper() for k in keys}  # TL071A of a datasheet of TL071 is a variant, not a code
    found: dict[str, tuple[int, str, bool]] = {}
    pages = store.db.execute("SELECT page, text FROM pages WHERE document_id = ? ORDER BY page",
                             (doc_id,)).fetchall()
    for key in keys:
        rx = re.compile(rf"(?<![A-Za-z0-9\-]){re.escape(key)}[A-Za-z0-9\-/+#.]{{0,16}}", re.I)
        family = []
        if not any(key.lower() in r["text"].lower() and rx.search(r["text"]) for r in pages):
            family = _family_codes(store, [{"id": doc_id}], key, MAX_CODES)[1]
        for row in pages:
            for line in row["text"].splitlines():
                for code in rx.findall(line) + [c for c, _ in family if c in line]:
                    code = code.rstrip("-/.").upper()
                    suffix = code[len(key):]
                    # A list (TMP112A/B, TPS22916BL/CL/CNL) has short parts after a slash;
                    # a code can have one long part (MCP1827S-3302E/EB, LM358DR/NOPB).
                    if (NOT_ORDERABLE_RE.search(suffix) or DOC_WORD_RE.search(code) or re.fullmatch(r"X+", suffix)
                            or (suffix and key[-1].isdigit() and re.fullmatch(r"\d", suffix))
                            or code.count("/") >= 2 or any(len(s) == 1 for s in code.split("/")[1:])
                            or (code != key.upper() and code in variants)):
                        continue
                    table = line.strip().upper().startswith(code)
                    # A code alone (no suffix) counts only on a line of an ordering table.
                    if not suffix and not (table and order_rx.search(line)):
                        continue
                    old = found.get(code)
                    if old is None or (table and not old[2]):
                        found[code] = (row["page"], re.sub(r"\s{2,}", "  ", line.strip())[:140], table)
    rows = sorted(((c, p, line, t) for c, (p, line, t) in found.items()), key=lambda x: (not x[3], x[1], x[0]))
    return rows[:MAX_CODES]


def _variant(best: str, other: str, sources: list[str], file_name: bool = False) -> bool:
    """True if the candidate other is a variant of the best one. The title or
    page 1 names it, or it is the file name, and it is one of:
    - the same length, with only the last characters different (ADC34J22 and
      ADC34J24, TL071 and TL072)
    - the base with a suffix after a hyphen (LM20144 and LM20144-Q1, LT1086 and
      LT1086-5)
    Not a variant:
    - an orderable code (LM358DR, SN74LV1T34DBVRG4): longer, with no hyphen
    - a suffix of one letter (MC74VHC50-D): onsemi names its documents
      "MC74VHC50/D" and its files "MC74VHC50-D.pdf"
    - a suffix with no digit when only the file name gives it"""
    b, o = _norm(best), _norm(other)
    named = file_name or {"title", "page 1"} & set(sources)
    if not b or o == b or not named or re.search(r"\dx", o):
        return False
    if len(o) == len(b):
        return len(os.path.commonprefix([b, o])) >= max(4, len(b) - 2)
    shorter, longer = sorted((best.upper(), other.upper()), key=len)
    suffix = longer[len(shorter) + 1:]
    if (len(suffix) == 1 and suffix.isalpha()) or (
            not {"title", "page 1"} & set(sources) and not any(c.isdigit() for c in suffix)):
        return False
    return longer.startswith(shorter + "-") and len(longer) - len(shorter) <= 4
