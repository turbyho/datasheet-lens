"""The JLCPCB parts library: an optional local database for the parametric search.

The data is the database of the jlcparts project
(https://github.com/yaqwsx/jlcparts): the parts library of the JLCPCB assembly
service, from the official JLCPCB Components API. The project publishes it
three times each day as a split zip (`zip -s 50m`) of one SQLite file. The
tool downloads it only on a command of the user (datasheet-lens jlc update),
because the file is large (276 MB to download, 2.2 GB on the disk) and the data
has no license statement.

Facts of the data (a probe, 2026-11-06, see plans/parametric-search.md):
- `stock` is the stock of JLCPCB for the assembly, not the stock of LCSC.
- `library_type` is "base" (Basic) or "expand" (Extended), `preferred` = 1 marks
  a Preferred part. The class sets the assembly fee, not the stock.
- The attributes are the texts of the LCSC listings: some are typical values,
  and some parts copy the values of other parts.
"""

from __future__ import annotations

import collections
import http.client
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

from .errors import Choose, LensError, Unavailable

DATA_URL = "https://yaqwsx.github.io/jlcparts/data"
ARCHIVE = "cache.zip"
DB_NAME = "jlcpcb.sqlite3"
STATE_NAME = "state.json"
TIMEOUT = 120.0
CHUNK = 1 << 20
MAX_AGE_DAYS = 1.0  # an older database gives a note in each result
LIMITS = ("The JLCPCB database comes from the jlcparts project. Its data has no license statement. "
          "The values come from the LCSC listings: some are typical values, not maximum values, and some "
          "parts copy the values of other parts. Read each value that the design needs in the datasheet. "
          "When the user selects a part: adopt_part(source='jlcpcb', number=<its C number>) adds its datasheet, "
          "the part and its purchase source.")


def folder() -> Path:
    """The folder of the JLCPCB database: jlcpcb/ in the data folder of the store."""
    from .config import data_home

    return data_home() / "jlcpcb"


def db_path() -> Path:
    return folder() / DB_NAME


def _get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise LensError(f"JLCPCB database: HTTP {e.code} {e.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise Unavailable(f"JLCPCB database: no connection ({getattr(e, 'reason', e)})") from None


def _stream(url: str) -> Iterator[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            while chunk := response.read(CHUNK):
                yield chunk
    except urllib.error.HTTPError as e:
        e.close()
        raise LensError(f"JLCPCB database: HTTP {e.code} {e.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise Unavailable(f"JLCPCB database: no connection ({getattr(e, 'reason', e)})") from None


def _entry(last: bytes) -> dict:
    """The only file of the split zip, from the central directory in the last
    part: its disk, offset, method, sizes and CRC-32."""
    end = last.rfind(b"PK\x05\x06")
    if end < 0 or len(last) < end + 22:
        raise LensError("JLCPCB database: the archive has no end record")
    disk, cd_disk, _, entries, cd_size, cd_offset, _ = struct.unpack("<HHHHIIH", last[end + 4:end + 22])
    if cd_disk != disk:
        raise LensError("JLCPCB database: the central directory of the archive starts in an earlier part: the tool "
                        "does not read this form")
    if entries != 1:
        raise LensError(f"JLCPCB database: the archive has {entries} files, not 1")
    cd = last[cd_offset:cd_offset + cd_size]
    if cd[:4] != b"PK\x01\x02" or len(cd) < 46:
        raise LensError("JLCPCB database: the central directory of the archive is not correct")
    (flags, method, crc, csize, usize, nlen, disk_start, offset) = (
        struct.unpack("<H", cd[8:10])[0], struct.unpack("<H", cd[10:12])[0], *struct.unpack("<III", cd[16:28]),
        struct.unpack("<H", cd[28:30])[0], struct.unpack("<H", cd[34:36])[0], struct.unpack("<I", cd[42:46])[0])
    name = cd[46:46 + nlen].decode("utf-8", "replace")
    if flags & 1 or method not in (0, 8) or 0xFFFFFFFF in (csize, usize, offset) or not name.endswith(".sqlite3"):
        raise LensError(f"JLCPCB database: the archive has a form that the tool does not read ({name}, method "
                        f"{method}, flags {flags})")
    return {"parts": disk, "disk": disk_start, "offset": offset, "method": method, "crc": crc, "csize": csize,
            "usize": usize, "name": name}


def _part_urls(parts: int) -> list[str]:
    """The URLs of the parts of the split zip: cache.z01 ... cache.zNN, then cache.zip."""
    return [f"{DATA_URL}/cache.z{n:02d}" for n in range(1, parts + 1)] + [f"{DATA_URL}/{ARCHIVE}"]


def _data(entry: dict, last: bytes, opener: Callable[[str], Iterator[bytes]]) -> Iterator[bytes]:
    """The compressed data of the file: the parts from its first disk, after its
    local header, up to the compressed size."""
    urls = _part_urls(entry["parts"])

    def parts():
        for i, url in enumerate(urls[entry["disk"]:], start=entry["disk"]):
            yield from ([last] if i == len(urls) - 1 else opener(url))

    skip, header, remaining = entry["offset"], b"", entry["csize"]
    data_start = None
    for chunk in parts():
        if skip:
            cut = min(skip, len(chunk))
            chunk, skip = chunk[cut:], skip - cut
            if not chunk:
                continue
        if data_start is None:
            header += chunk
            if len(header) < 30:
                continue
            if header[:4] != b"PK\x03\x04":
                raise LensError("JLCPCB database: the local header of the archive is not correct")
            nlen, xlen = struct.unpack("<HH", header[26:30])
            data_start = 30 + nlen + xlen
            if len(header) < data_start:
                data_start = None
                continue
            chunk, header = header[data_start:], b""
        part = chunk[:remaining]
        remaining -= len(part)
        if part:
            yield part
        if remaining == 0:
            return
    raise LensError("JLCPCB database: the archive ends before the end of its data")


def update(progress: Callable[[str], None] = print, opener: Callable[[str], Iterator[bytes]] = _stream) -> dict:
    """Download the database, unpack it, check its size and CRC-32, add the
    indexes of the tool, and put it in place of the old one. The old database
    stays when a step fails. Gives the state."""
    out = folder()
    out.mkdir(parents=True, exist_ok=True)
    _remove_stale_tmp(out)
    started = time.time()
    progress(f"get {DATA_URL}/{ARCHIVE}")
    last = _get(f"{DATA_URL}/{ARCHIVE}")
    entry = _entry(last)
    # A file of this process only: an update in a second process (the CLI and the
    # MCP server) does not write into it. The last complete update wins.
    tmp = out / f"{DB_NAME}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"
    state_tmp = out / f"{STATE_NAME}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"
    crc, size, done = 0, 0, 0
    inflate = zlib.decompressobj(-15) if entry["method"] == 8 else None
    try:
        with tmp.open("wb") as f:
            for chunk in _data(entry, last, opener):
                done += len(chunk)
                data = inflate.decompress(chunk) if inflate else chunk
                crc, size = zlib.crc32(data, crc), size + len(data)
                f.write(data)
                if done % (50 * CHUNK) < len(chunk):
                    progress(f"{done * 100 // entry['csize']} % of {entry['csize'] // (1 << 20)} MB")
            if inflate:
                data = inflate.flush()
                crc, size = zlib.crc32(data, crc), size + len(data)
                f.write(data)
        if (crc, size) != (entry["crc"], entry["usize"]):
            raise LensError("JLCPCB database: the file is not correct after the download (CRC-32 or size): "
                            "run the update again")
        progress("add the indexes")
        _add_indexes(tmp)
        state = {"downloaded": time.time(), "source": f"{DATA_URL}/{ARCHIVE}", "size": size,
                 "seconds": round(time.time() - started, 1)}
        state_tmp.write_text(json.dumps(state), encoding="utf-8")  # before the replace: no half result
        os.replace(tmp, db_path())
        os.replace(state_tmp, out / STATE_NAME)
    finally:
        tmp.unlink(missing_ok=True)
        state_tmp.unlink(missing_ok=True)
    return state


TMP_SUFFIX = ".tmp"
STALE_SECONDS = 3600.0  # an update takes minutes: an older temporary file is from a stopped update


def _stale_tmp(out: Path) -> list[Path]:
    """The temporary files of updates that stopped (a process that ended during
    the download): older than STALE_SECONDS."""
    now = time.time()
    return [p for p in out.glob(f"*{TMP_SUFFIX}") if now - p.stat().st_mtime > STALE_SECONDS]


def _remove_stale_tmp(out: Path) -> None:
    for path in _stale_tmp(out):
        path.unlink(missing_ok=True)


def _add_indexes(path: Path) -> None:
    """The indexes of the searches of the tool: the MPN and the category."""
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE INDEX IF NOT EXISTS dl_mfr ON jlc_components(mfr COLLATE NOCASE)")
        db.execute("CREATE INDEX IF NOT EXISTS dl_subcategory ON jlc_components(subcategory, category)")
        db.commit()
    finally:
        db.close()


def remove() -> bool:
    """Delete the database and its state. True if there was one."""
    found = False
    for path in [folder() / DB_NAME, folder() / STATE_NAME, *folder().glob(f"*{TMP_SUFFIX}")]:
        if path.exists():
            path.unlink()
            found = True
    return found


def state() -> dict | None:
    """The state of the local database, or None when there is none."""
    if not db_path().is_file():
        return None
    try:
        data = json.loads((folder() / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"downloaded": db_path().stat().st_mtime}
    data["age_days"] = (time.time() - float(data.get("downloaded") or 0)) / 86400
    data["path"] = str(db_path())
    return data


def age_note() -> str:
    """A note when the database is older than MAX_AGE_DAYS, else ""."""
    s = state()
    if s and s["age_days"] > MAX_AGE_DAYS:
        return (f"Note: the JLCPCB database is {s['age_days']:.1f} days old. The stock changes each day: the "
                "user runs \"datasheet-lens jlc update\", or an agent starts jlcpcb_library(action=\"update\") "
                "when the user agrees.")
    return ""


def connect() -> sqlite3.Connection:
    """The database, read-only. A LensError with the step when there is none."""
    path = db_path()
    if not path.is_file():
        raise LensError("The JLCPCB database is not on this machine (276 MB download, 2.2 GB on the disk). Ask the "
                        "user: start the update with jlcpcb_library(action=\"update\") when the user agrees, or the "
                        "user runs \"datasheet-lens jlc update\" in a terminal. " + LIMITS)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


# The search. -----------------------------------------------------------------

PART_URL = "https://jlcpcb.com/partdetail/C{lcsc}"
LCSC_URL = "https://www.lcsc.com/product-detail/C{lcsc}.html"
# Parameters from the columns of the table, not from the attributes.
COLUMNS = {"Package": "package", "Manufacturer": "manufacturer", "Library": "library"}
ALIASES = {"package": "Package", "manufacturer": "Manufacturer", "library": "Library",
           "vdss": "Drain to Source Voltage", "vds": "Drain to Source Voltage", "rds_on": "RDS(on)",
           "rdson": "RDS(on)", "vgs_th": "Gate Threshold Voltage (Vgs(th))", "vgsth": "Gate Threshold Voltage (Vgs(th))",
           "id": "Current - Continuous Drain(Id)", "qg": "Gate Charge(Qg)", "channel": "Type"}
SORTS = ("stock", "price")
MAX_LIMIT = 100

_categories: dict = {}  # (path, mtime) -> list


def library(row) -> str:
    """The library class of JLCPCB: Basic, Preferred or Extended."""
    if row["library_type"] == "base":
        return "Basic"
    return "Preferred" if row["preferred"] else "Extended"


def _key() -> tuple:
    path = db_path()
    return str(path), path.stat().st_mtime if path.is_file() else 0


def categories(db) -> list[dict]:
    """All (category, subcategory) pairs with the number of parts and of parts with stock."""
    key = _key()
    if _categories.get("key") != key:
        rows = db.execute("SELECT category, subcategory, COUNT(*), SUM(stock > 0) FROM jlc_components "
                          "WHERE present = 1 GROUP BY category, subcategory").fetchall()
        _categories.update(key=key, list=[{"category": c, "subcategory": s, "parts": n, "in_stock": k or 0}
                                          for c, s, n, k in rows])
    return _categories["list"]


def _label(c: dict) -> str:
    return f"{c['category']} > {c['subcategory']} ({c['in_stock']} in stock of {c['parts']})"


def _choices(key: str, found: list[dict]) -> list[str]:
    """The lines of a Choose: the subcategory names in quotes (to copy), with
    their categories and stock, and an example command."""
    groups: dict[str, list[dict]] = {}
    for c in found:
        groups.setdefault(c["subcategory"], []).append(c)
    order = sorted(groups, key=lambda s: -sum(c["in_stock"] for c in groups[s]))[:40]
    lines = [f"The category {key!r} fits more JLCPCB categories. Select one: give its subcategory name in quotes. "
             "A name selects each category with that name; give more names with |:"]
    for name in order:
        cats = groups[name]
        lines.append(f"  \"{name}\": {sum(c['in_stock'] for c in cats)} in stock of {sum(c['parts'] for c in cats)} "
                     f"(in {'; '.join(c['category'] for c in cats)})")
    lines.append(f"For example: datasheet-lens pick jlcpcb \"{order[0]}\" -P (its parameters), then the same with "
                 "filters. MCP: part_parameters(\"jlcpcb\", ...), then find_parts.")
    return lines


def find_categories(text: str, db) -> tuple[list[dict], list[dict]]:
    """The categories for a text, and the other categories whose name holds the
    text. The text is "CATEGORY > SUBCATEGORY", a subcategory name (each
    category with that subcategory), or a part of a name that fits one
    subcategory name. More of them with "|": "MOSFET|MOSFETs". A LensError with
    the list when a part of a name fits more subcategory names."""
    keys = [k.strip().lower() for k in text.split("|") if k.strip()]
    if not keys:
        raise LensError("give a category, for example \"MOSFETs\" or \"Transistors/Thyristors > MOSFETs\"")
    cats = categories(db)
    chosen: list[dict] = []
    for key in keys:
        same = [c for c in cats if f"{c['category']} > {c['subcategory']}".lower() == key]
        same = same or [c for c in cats if c["subcategory"].lower() == key]
        if not same:
            found = [c for c in cats if key in c["subcategory"].lower() or key in c["category"].lower()]
            if not found:
                raise LensError(f"no JLCPCB category {key!r}. Give a part of its name, for example \"MOSFET\" or "
                                "\"LDO\"")
            if len({c["subcategory"].lower() for c in found}) > 1:
                raise Choose(_choices(key, found))
            same = found
        chosen += [c for c in same if c not in chosen]
    others = [c for c in cats if c not in chosen and any(k in c["subcategory"].lower() for k in keys)]
    return chosen, others


def _rows(db, cats: list[dict], in_stock: bool, keywords: str = "") -> list:
    where = " OR ".join("(category = ? AND subcategory = ?)" for _ in cats)
    args = [x for c in cats for x in (c["category"], c["subcategory"])]
    sql = (f"SELECT lcsc, category, subcategory, mfr, package, manufacturer, library_type, preferred, description, "
           f"datasheet, stock, price, attributes FROM jlc_components WHERE present = 1 AND ({where})"
           + (" AND stock > 0" if in_stock else ""))
    rows = db.execute(sql, args).fetchall()
    words = [w.lower() for w in keywords.split() if w]
    if words:
        rows = [r for r in rows if all(w in f"{r['mfr']} {r['description']}".lower() for w in words)]
    return rows


def _attributes(row) -> dict[str, tuple[str, str]]:
    """The attributes of a row: {name in lower case: (name, value)}. The LCSC
    listings write one name in more forms ("Type" and "type")."""
    try:
        data = json.loads(row["attributes"] or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip().lower(): (str(k).strip(), str(v)) for k, v in data.items()}


def _value(row, attrs: dict, name: str) -> str:
    column = COLUMNS.get(name)
    if column == "library":
        return library(row)
    if column:
        return str(row[column] or "")
    return attrs.get(name.lower(), ("", ""))[1]


def _parameters(rows, parsed: dict | None = None) -> list:
    """The parameters of the rows with their distinct values (ID = text). The
    name of a parameter is its most frequent form. parsed: the attributes of
    each row by its LCSC number, when the caller has them."""
    from .parametric import Parameter

    values: dict[str, dict[str, None]] = {name.lower(): {} for name in COLUMNS}
    forms: dict[str, collections.Counter] = {name.lower(): collections.Counter({name: 1}) for name in COLUMNS}
    for row in rows:
        attrs = parsed[row["lcsc"]] if parsed is not None else _attributes(row)
        for name in COLUMNS:
            values[name.lower()].setdefault(_value(row, attrs, name), None)
        for key, (name, value) in attrs.items():
            values.setdefault(key, {}).setdefault(value, None)
            forms.setdefault(key, collections.Counter())[name] += 1
    return [Parameter(id=key, name=forms[key].most_common(1)[0][0], values=[(v, v) for v in vals if v])
            for key, vals in values.items()]


def _first_price(row) -> tuple[str, float | None]:
    """The first price break: ("1-9", 0.995), from "1-9:0.995,10-29:0.82"."""
    first = str(row["price"] or "").split(",", 1)[0]
    if ":" not in first:
        return "", None
    qty, _, price = first.partition(":")
    try:
        return qty, float(price)
    except ValueError:
        return qty, None


def search(category: str, filters: list[str], keywords: str = "", in_stock: bool = True, sort: str = "stock",
           limit: int = 20, offset: int = 0) -> dict:
    """The parts of the categories that agree with the filters."""
    from .parametric import parse_filter, resolve

    if sort not in SORTS:
        raise LensError(f"sort: one of {', '.join(SORTS)}")
    if not 1 <= limit <= MAX_LIMIT:
        raise LensError(f"limit: 1 to {MAX_LIMIT}")
    if offset < 0:
        raise LensError("offset: 0 or more")
    parsed = [parse_filter(f) for f in filters]
    for f in parsed:
        if f.condition:
            f.condition_quantity()  # a wrong condition: an error before the search
    db = connect()
    try:
        cats, others = find_categories(category, db)
        rows = _rows(db, cats, in_stock, keywords)
    finally:
        db.close()
    attrs = {row["lcsc"]: _attributes(row) for row in rows}  # one parse of each row
    params = _parameters(rows, attrs)
    report = []
    for f in parsed:
        parameter = resolve(f.name, params, ALIASES)
        kept, no_value, no_point = [], 0, 0
        for row in rows:
            text = _value(row, attrs[row["lcsc"]], parameter.name)
            if not text:
                no_value += 1
                continue
            if f.condition:
                agrees = f.matches_at(text)
                if agrees is None:
                    no_point += 1
                    continue
            else:
                agrees = f.matches(text)
            if agrees:
                kept.append(row)
        report.append({"filter": f, "parameter": parameter, "before": len(rows), "after": len(kept),
                       "no_value": no_value, "no_point": no_point})
        rows = kept
    if sort == "stock":
        rows.sort(key=lambda r: -(r["stock"] or 0))
    else:
        prices = {r["lcsc"]: _first_price(r)[1] for r in rows}
        rows.sort(key=lambda r: (prices[r["lcsc"]] is None, prices[r["lcsc"]] or 0))
    return {"categories": cats, "others": others, "filters": report, "count": len(rows),
            "rows": rows[offset:offset + limit], "offset": offset, "sort": sort, "in_stock": in_stock}


def _stock_text(row) -> str:
    stock = row["stock"] or 0
    return f"JLCPCB stock: {stock}" if stock > 0 else "JLCPCB stock: 0 (NOT IN STOCK: order needed)"


def part_line(row, names: list[str]) -> str:
    """One line for a part: LCSC number, MPN, manufacturer, JLCPCB stock,
    library class, price, the filtered parameters, package and the links."""
    attrs = _attributes(row)
    qty, price = _first_price(row)
    parts = [f"C{row['lcsc']} {row['mfr']} ({row['manufacturer'] or 'manufacturer unknown'})", _stock_text(row),
             f"library: {library(row)}"]
    if price is not None:
        parts.append(f"price {qty}: {price} USD")
    for name in names:
        value = _value(row, attrs, name) if name != "Library" else ""  # the library class is above
        if value:
            parts.append(f"{name}: {value}")
    if row["datasheet"]:
        parts.append(f"datasheet {row['datasheet']}")
    parts.append(f"JLCPCB {PART_URL.format(lcsc=row['lcsc'])}")
    parts.append(f"LCSC {LCSC_URL.format(lcsc=row['lcsc'])}")
    return "; ".join(parts)


def search_lines(result: dict) -> list[str]:
    """The result as lines: the source and its date, each filter with the parts
    that it left out, each part, and the limits of the data."""
    s = state() or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(s.get("downloaded") or 0)))
    cats = "; ".join(f"{c['category']} > {c['subcategory']}" for c in result["categories"])
    scope = "parts with JLCPCB stock" if result["in_stock"] else "parts (with or without JLCPCB stock)"
    lines = [f"Source: JLCPCB parts library (jlcparts database of {when}). Category: {cats}. {result['count']} "
             f"{scope} agree" + (f", parts {result['offset'] + 1} to {result['offset'] + len(result['rows'])}, "
                                 f"sorted by {result['sort']}" if result["rows"] else "") + "."]
    if result["others"]:
        lines.append("Other categories with a name that holds the text (not searched; give them with |): "
                     + "; ".join(_label(c) for c in result["others"][:10]))
    names: list[str] = []
    for r in result["filters"]:
        f, name = r["filter"], r["parameter"].name
        text = f"filter \"{f.text}\": applied to \"{name}\": {r['after']} of {r['before']} parts agree"
        details = []
        if r["no_value"]:
            details.append(f"{r['no_value']} parts have no value")
        if r["no_point"]:
            details.append(f"{r['no_point']} parts give no value at {f.condition} (read it in their datasheet)")
        lines.append(text + (f" ({'; '.join(details)})" if details else ""))
        if name not in names:
            names.append(name)
    if "Package" not in names:
        names.append("Package")
    lines += [part_line(row, names) for row in result["rows"]]
    lines.append("The library class sets the assembly fee of JLCPCB: Basic and Preferred parts have no loading "
                 "fee, Extended parts have one. " + LIMITS)
    note = age_note()
    return lines + ([note] if note else [])


def parameter_lines(category: str) -> list[str]:
    """The parameters of a category, with the number of values and examples."""
    from .digikey import _spread
    from .parametric import quantity

    db = connect()
    try:
        cats, _ = find_categories(category, db)
        rows = _rows(db, cats, in_stock=True)
    finally:
        db.close()
    short: dict[str, str] = {}
    for alias, name in ALIASES.items():
        short.setdefault(name, alias)
    lines = ["Source: JLCPCB parts library. Category: "
             + "; ".join(f"{c['category']} > {c['subcategory']}" for c in cats)
             + f" ({len(rows)} parts with JLCPCB stock). The parameters for the filters, with examples of their "
               "values (separated by |):"]
    for p in _parameters(rows):
        values = sorted((t for _, t in p.values),
                        key=lambda t: (quantity(t) is None, quantity(t).value if quantity(t) else 0, t))
        alias = f" (short name {short[p.name]})" if p.name in short else ""
        lines.append(f"{p.name}{alias}: {len(values)} values: {' | '.join(_spread(values, 8))}")
    note = age_note()
    return lines + ([note] if note else [])


def offers(mpn: str, exact: bool = True) -> list:
    """The parts of the JLCPCB library for an MPN: exact, or each MPN that
    starts with it (up to 50). As Offer objects of distributors.py."""
    from .distributors import MIN_PREFIX

    mpn = mpn.strip()
    if not mpn:
        raise LensError("the MPN is empty")
    if not exact and len(mpn) < MIN_PREFIX:
        raise LensError(f"give {MIN_PREFIX} or more characters of the MPN")
    db = connect()
    try:
        if exact:
            rows = db.execute("SELECT * FROM jlc_components WHERE present = 1 AND mfr = ? COLLATE NOCASE",
                              (mpn,)).fetchall()
        else:
            escaped = mpn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute("SELECT * FROM jlc_components WHERE present = 1 AND mfr LIKE ? ESCAPE '\\' "
                              "ORDER BY stock DESC LIMIT 50", (escaped + "%",)).fetchall()
    finally:
        db.close()
    return [_offer(row) for row in rows]


def _offer(row):
    """The Offer of distributors.py for a row of the library."""
    from .distributors import Offer, price_text

    return Offer(
        source="JLCPCB", mpn=row["mfr"], manufacturer=row["manufacturer"] or "",
        description=row["description"] or "", stock=row["stock"] or 0,
        prices=[(int(q), price) for q, price in
                ((q.split("-")[0], price_text(p, "USD")) for q, _, p in
                 (x.partition(":") for x in str(row["price"] or "").split(",") if ":" in x))
                if q.isdigit() and price],
        package=row["package"] or "", library=library(row), datasheet_url=row["datasheet"] or "",
        product_url=PART_URL.format(lcsc=row["lcsc"]), distributor_pn=f"C{row['lcsc']}")


# A JLCPCB number (LCSC number): "C" and digits, for example C47616.
NUMBER_RE = re.compile(r"C(\d+)", re.IGNORECASE)


def number_id(number: str) -> int:
    """The LCSC ID of a JLCPCB number (C47616 and c047616 give 47616). A
    LensError for a text that is not a JLCPCB number."""
    match = NUMBER_RE.fullmatch(number.strip())
    if not match:
        raise LensError(f"{number!r} is not a JLCPCB number: it is C and digits, for example C47616")
    return int(match[1])


def row_of_number(db, number: str):
    """The row of the library for a JLCPCB number, or None (see number_id)."""
    return db.execute("SELECT * FROM jlc_components WHERE lcsc = ? AND present = 1",
                      (number_id(number),)).fetchone()


def offer_of_number(number: str, db=None):
    """The Offer of the part with a JLCPCB number (C47616), or None when the
    library does not have it. db: an open connection (connect) for more
    numbers; else the function opens one. A LensError for a text that is not a
    JLCPCB number."""
    own = db is None
    db = connect() if own else db
    try:
        row = row_of_number(db, number)
    finally:
        if own:
            db.close()
    return _offer(row) if row is not None else None


# Replacements (phase 7 of plans/parametric-search.md). -------------------------

# The rules of a replacement of a passive part, for each subcategory of the library:
# (attribute in lower case, rule). "=": the same quantity; "text=": the same text
# (the dielectric); ">=" and "<=": the quantity of the replacement is not worse;
# "worst<=": the largest absolute number of the replacement is not larger (a
# tolerance "±5%", "-20%~+80%"). An attribute that the part has and the
# replacement does not have stops the replacement: the tool cannot verify it.
# Decision of the user (2026-11-07): resistors, capacitors and inductors only.
_INDUCTOR = (("inductance", "="), ("tolerance", "worst<="), ("current rating", ">="),
             ("current - saturation(isat)", ">="), ("dc resistance(dcr)", "<="))
PASSIVE_RULES = {
    "Chip Resistor - Surface Mount": (("resistance", "="), ("tolerance", "worst<="), ("power(watts)", ">="),
                                      ("voltage-supply(max)", ">="), ("temperature coefficient", "worst<=")),
    "Multilayer Ceramic Capacitors MLCC - SMD/SMT": (("capacitance", "="), ("voltage rating", ">="),
                                                     ("temperature coefficient", "text="), ("tolerance", "worst<=")),
    "Power Inductors": _INDUCTOR,
    "Inductors (SMD)": _INDUCTOR,
}
LIBRARY_RANK = {"Basic": 0, "Preferred": 1, "Extended": 2}  # no loading fee first
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _worst(text: str) -> float | None:
    """The largest absolute number of a text ("-20%~+80%" gives 80), or None."""
    numbers = [float(n) for n in _NUMBER_RE.findall(text)]
    return max(numbers) if numbers else None


def _agrees(rule: str, mine: str, theirs: str) -> bool:
    """True if the value of a replacement (theirs) is not worse than the value
    of the part (mine) by the rule (see PASSIVE_RULES)."""
    from .parametric import quantity

    if rule == "text=":
        return " ".join(mine.split()).casefold() == " ".join(theirs.split()).casefold()
    if rule == "worst<=":
        # The same unit only: a tolerance in % and a tolerance in pF do not compare.
        unit_a, unit_b = quantity(mine), quantity(theirs)
        if unit_a is None or unit_b is None or unit_a.unit != unit_b.unit:
            return False
        a, b = _worst(mine), _worst(theirs)
        return a is not None and b is not None and b <= a * (1 + 1e-9)
    a, b = quantity(mine), quantity(theirs)
    if a is None or b is None or a.unit != b.unit:
        return False
    if rule == "=":
        return math.isclose(a.value, b.value, rel_tol=1e-6, abs_tol=1e-15)
    return b.value >= a.value * (1 - 1e-9) if rule == ">=" else b.value <= a.value * (1 + 1e-9)


def _same_parameters(rules, mine: dict, other) -> bool:
    """True if a passive part (other) has the parameters of the part (mine: its
    attributes) by the rules of its subcategory."""
    theirs = _attributes(other)
    for name, rule in rules:
        if name not in mine:
            continue
        if name not in theirs or not _agrees(rule, mine[name][1], theirs[name][1]):
            return False
    return True


def replacements(number: str, limit: int = 3, db=None, obsolete: bool = False,
                 same_maker: Callable[[str], bool] | None = None) -> list[tuple]:
    """The replacements in the JLCPCB library for the part of a C number, with
    JLCPCB stock: (Offer, reason). The same MPN with a different C number (an
    other manufacturer or an other listing), and for a resistor, a capacitor or
    an inductor the parts with the same package and parameters (PASSIVE_RULES).
    Basic and Preferred first (no loading fee), then the stock. For a part with
    stock, only Basic and Preferred parts: an other Extended part does not help.
    The values come from the LCSC listings: read the datasheet of a replacement
    before you use it. No replacement of semiconductors and ICs (decision of the
    user): the agent uses find_parts with the values of the datasheet.

    obsolete: the manufacturer of the part gives a lifecycle risk (obsolete, last
    time buy, not for new designs). Then the same MPN of the manufacturer of the
    part is no replacement: the same name as the row of the number (no regard to
    case), or a name for which same_maker is true (the manufacturer registry of
    the store knows the other names). The same MPN of an other manufacturer has
    its own lifecycle state (not checked). An Extended part helps too."""
    number_id(number)  # a LensError for a text that is not a JLCPCB number
    own = db is None
    db = connect() if own else db
    try:
        row = row_of_number(db, number)
        if row is None:
            return []
        maker = (row["manufacturer"] or "").casefold()

        def of_part(name: str) -> bool:
            return name.casefold() == maker or bool(same_maker and same_maker(name))

        rows = db.execute("SELECT * FROM jlc_components WHERE present = 1 AND stock > 0 AND mfr = ? COLLATE NOCASE "
                          "AND lcsc != ?", (row["mfr"], row["lcsc"]))
        found = ([(r, f"the same MPN, manufacturer {r['manufacturer'] or 'unknown'}: its lifecycle state is not "
                      "checked") for r in rows if not of_part(r["manufacturer"] or "")] if obsolete
                 else [(r, "the same MPN") for r in rows])
        rules = PASSIVE_RULES.get(row["subcategory"])
        mine = _attributes(row)
        value = rules[0][0] if rules else ""  # the value of the part (resistance, capacitance, inductance)
        if rules and row["package"] and value in mine:
            # The SQL filter of the value first (the listings write its name as "Resistance" or
            # "resistance"), then the rules in Python: only the rows of the same value are read.
            db.create_function("same_value", 1, lambda text, wanted=mine[value][1]: (
                text is not None and _agrees("=", wanted, text)), deterministic=True)
            name = mine[value][0]
            found += [(r, "the same package and parameters") for r in db.execute(
                "SELECT * FROM jlc_components WHERE present = 1 AND stock > 0 AND subcategory = ? AND package = ?"
                " AND lcsc != ? AND NOT (mfr = ? COLLATE NOCASE) AND same_value(COALESCE("
                "json_extract(attributes, ?), json_extract(attributes, ?)))",
                (row["subcategory"], row["package"], row["lcsc"], row["mfr"], f'$."{name}"', f'$."{name.lower()}"'))
                if _same_parameters(rules, mine, r)]
    finally:
        if own:
            db.close()
    if row["stock"] and not obsolete:  # the part has stock: only a part with no loading fee helps
        found = [(r, why) for r, why in found if library(r) != "Extended"]
    found.sort(key=lambda item: (LIBRARY_RANK[library(item[0])], -(item[0]["stock"] or 0)))
    return [(_offer(r), why) for r, why in found[:limit]]


# The update in the background (the MCP tool jlcpcb_library). -------------------

_job: dict = {"state": "idle"}  # idle, running, done, failed
_job_lock = threading.Lock()


def start_update(opener: Callable[[str], Iterator[bytes]] | None = None) -> dict:
    """Start the update in a background thread, and give the state of the job
    at once. A job that runs already is not started again."""
    with _job_lock:
        if _job.get("state") == "running":
            return dict(_job)
        _job.clear()
        _job.update(state="running", progress="start", started=time.time())

    def progress(line: str) -> None:
        with _job_lock:
            _job["progress"] = line

    def run() -> None:
        try:
            result = update(progress=progress, opener=opener or _stream)
        except Exception as e:  # the job gives each error to the next status call
            with _job_lock:
                _job.update(state="failed", error=f"{e.__class__.__name__}: {e}", finished=time.time())
        else:
            with _job_lock:
                _job.update(state="done", progress="done", finished=time.time(), seconds=result["seconds"])

    threading.Thread(target=run, name="jlcpcb-update", daemon=True).start()
    return dict(_job)


def job() -> dict:
    """The state of the update job of this process."""
    with _job_lock:
        return dict(_job)


def status_lines() -> list[str]:
    """The state of the update job and of the database, as lines."""
    j = job()
    lines = []
    if j["state"] == "running":
        lines.append(f"update: running for {time.time() - j['started']:.0f} s: {j.get('progress', '')}. Ask the "
                     "status again in some seconds")
    elif j["state"] == "done":
        lines.append(f"update: done in {j.get('seconds', 0)} s")
    elif j["state"] == "failed":
        lines.append(f"update: FAILED: {j.get('error', '')}. The old database, if any, stays")
    s = state()
    lines.append(f"database: {s['path']}, {s['age_days']:.1f} days old" if s else
                 "database: not on this machine")
    if j["state"] != "running" and folder().is_dir():
        others = list(folder().glob(f"*{TMP_SUFFIX}"))
        if others:
            stale = set(_stale_tmp(folder()))
            lines.append(f"{len(others)} temporary files of an update: "
                         + ("an update that stopped (the next update deletes them)" if all(p in stale for p in others)
                            else "an update of a different process (the CLI or a second MCP server) runs now"))
    return lines + ([note] if (note := age_note()) else [])
