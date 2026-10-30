"""The manufacturer registry: the names of each manufacturer, and the pairs of
manufacturers that are not the same.

A part keeps the manufacturer name of its datasheet. The registry tells if two
names are the same manufacturer:
- the two names have the same key (letters and digits, no legal suffix):
  "Microchip Technology Inc." and "MICROCHIP TECHNOLOGY"
- the two names are aliases of one manufacturer: "TI" and "Texas Instruments"

The registry unifies the spellings of a name where it can, but not at any
cost. It does not follow the name changes and mergers of companies.
MANUFACTURERS gives the first aliases. Only the user records a relation (the
CLI command maker, the MCP tool manufacturer_relation). The registry does not
learn a name from the data by itself: two parts with the same MPN and one
datasheet can be a second source (LM358 of TI and of ST).
store.alias_candidates gives such pairs to the user.

The functions do not commit: the caller does (an update of the registry runs
in one transaction).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .errors import LensError

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
    "GigaDevice": ("GIGADEVICE",),
    "Macronix": ("MACRONIX",),
    "Coto Technology": ("COTO TECHNOLOGY", "COTOTECHNOLOGY", "COTORELAY"),
    "ECS": ("ECS INC", "ECSXTAL"),
    "1GLOBAL": ("1GLOBAL",),
}
# Short names that people and BOMs use. Only for the registry: they are too
# short to find a manufacturer in the text of a PDF (identify.ALIAS_RE).
SHORT_NAMES = {
    "Texas Instruments": ("TI",),
    "Analog Devices": ("ADI",),
    "STMicroelectronics": ("ST",),
}
RELATIONS = ("alias", "distinct")
SEED_KEY = "manufacturer_seed"  # the meta key: the version of MANUFACTURERS in the registry
# A legal suffix at the end of a name: "Inc.", ", Ltd.", "Co., Ltd.".
LEGAL_RE = re.compile(r"(?i)[\s,]+(inc|incorporated|corp|corporation|ltd|limited|gmbh|ag|co|llc|plc|s\.a|b\.v|n\.v)"
                      r"\.?$")


def key(name: str) -> str:
    """The key of a name: no legal suffix, only letters and digits, case folded."""
    text = name.strip()
    while (shorter := LEGAL_RE.sub("", text)) != text:
        text = shorter
    return "".join(c for c in text.casefold() if c.isalnum())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def maker_id(db: sqlite3.Connection, name: str) -> int | None:
    """The manufacturer of a name in the registry, or None."""
    k = key(name)
    row = db.execute("SELECT manufacturer_id FROM manufacturer_aliases WHERE key = ?", (k,)).fetchone() if k else None
    return row[0] if row else None


def group(db: sqlite3.Connection | None, name: str) -> tuple[str, object]:
    """The identity of a name for a comparison: its manufacturer, or its key
    when the registry does not have it (or db is None)."""
    mid = maker_id(db, name) if db is not None else None
    return ("id", mid) if mid is not None else ("key", key(name))


def same(db: sqlite3.Connection | None, a: str, b: str) -> bool:
    """True if the two names are the same manufacturer (see the module
    docstring). An empty name is the same only as an empty name. With no db:
    only the keys."""
    return group(db, a) == group(db, b)


def distinct(db: sqlite3.Connection, a: str, b: str) -> bool:
    """True if the registry records the two names as two manufacturers."""
    ia, ib = maker_id(db, a), maker_id(db, b)
    if ia is None or ib is None or ia == ib:
        return False
    return db.execute("SELECT 1 FROM manufacturer_distinct WHERE a = ? AND b = ?",
                      tuple(sorted((ia, ib)))).fetchone() is not None


def _add_alias(db: sqlite3.Connection, name: str, mid: int, source: str) -> None:
    """Record name for the manufacturer mid. A key that the registry has stays."""
    db.execute("INSERT OR IGNORE INTO manufacturer_aliases (key, manufacturer_id, name, source, created)"
               " VALUES (?, ?, ?, ?, ?)", (key(name), mid, name.strip(), source, _now()))


def _new(db: sqlite3.Connection, name: str, source: str) -> int:
    mid = db.execute("INSERT INTO manufacturers (name) VALUES (?)", (name.strip(),)).lastrowid
    _add_alias(db, name, mid, source)
    return mid


def _ensure(db: sqlite3.Connection, name: str, source: str) -> int:
    if not key(name):
        raise LensError(f"not a manufacturer name: {name!r}")
    mid = maker_id(db, name)
    return mid if mid is not None else _new(db, name, source)


def _join(db: sqlite3.Connection, name: str, known: str, source: str) -> bool:
    """Record name as a different name of the manufacturer of known, when the
    registry does not have one of the two names. True if recorded; False for
    two names that the registry has (see set_alias)."""
    if not key(name) or not key(known) or key(name) == key(known):
        return False
    a, b = maker_id(db, name), maker_id(db, known)
    if a is not None and b is not None:
        return False
    if a is None and b is None:
        b = _new(db, known, source)
    if a is None:
        _add_alias(db, name, b, source)
    else:
        _add_alias(db, known, a, source)
    return True


def set_alias(db: sqlite3.Connection, name: str, other: str, source: str = "user") -> None:
    """name and other are one manufacturer with two names. Two manufacturers of
    the registry become one: the manufacturer of other stays."""
    for n in (name, other):
        if not key(n):
            raise LensError(f"not a manufacturer name: {n!r}")
    if _join(db, name, other, source):
        return
    a, b = maker_id(db, name), maker_id(db, other)
    if a == b:
        return
    if distinct(db, name, other):
        raise LensError(f"the registry records {name} and {other} as two manufacturers")
    # The manufacturer a goes into b: its names and its distinct pairs (the
    # pairs keep the order a < b).
    db.execute("UPDATE manufacturer_aliases SET manufacturer_id = ? WHERE manufacturer_id = ?", (b, a))
    for x, y in db.execute("SELECT a, b FROM manufacturer_distinct WHERE ? IN (a, b)", (a,)).fetchall():
        third = y if x == a else x
        db.execute("INSERT OR IGNORE INTO manufacturer_distinct (a, b) VALUES (?, ?)", tuple(sorted((b, third))))
    db.execute("DELETE FROM manufacturers WHERE id = ?", (a,))


def set_distinct(db: sqlite3.Connection, name: str, other: str, source: str = "user") -> None:
    """name and other are two manufacturers: each can make a part with the same
    MPN (a second source, for example LM358 of TI and onsemi)."""
    a, b = _ensure(db, name, source), _ensure(db, other, source)
    if a == b:
        raise LensError(f"{name} and {other} are one manufacturer in the registry")
    db.execute("INSERT OR IGNORE INTO manufacturer_distinct (a, b) VALUES (?, ?)", tuple(sorted((a, b))))


def relate(db: sqlite3.Connection, relation: str, name: str, other: str, source: str = "user") -> None:
    """Record a relation of RELATIONS between two names."""
    if relation not in RELATIONS:
        raise LensError(f"the relation must be one of {', '.join(RELATIONS)}")
    {"alias": set_alias, "distinct": set_distinct}[relation](db, name, other, source)


def listing(db: sqlite3.Connection) -> list[str]:
    """One line for each manufacturer: its name, the other names and the
    manufacturers that are not the same."""
    names = {r[0]: r[1] for r in db.execute("SELECT id, name FROM manufacturers")}
    lines = []
    for mid, name in db.execute("SELECT id, name FROM manufacturers ORDER BY name"):
        aliases = sorted({r[0] for r in db.execute("SELECT name FROM manufacturer_aliases WHERE manufacturer_id = ?"
                                                   " AND key != ?", (mid, key(name)))}, key=str.casefold)
        others = sorted(names[b if a == mid else a] for a, b in db.execute(
            "SELECT a, b FROM manufacturer_distinct WHERE a = ? OR b = ?", (mid, mid)))
        lines.append(name + (f" = {', '.join(aliases)}" if aliases else "")
                     + (f"; not the same as: {', '.join(others)}" if others else ""))
    return lines


def _seed_version() -> str:
    return hashlib.sha256(json.dumps([MANUFACTURERS, SHORT_NAMES], sort_keys=True).encode()).hexdigest()[:12]


def seed_current(db: sqlite3.Connection) -> bool:
    """True if the registry has the names of this version of MANUFACTURERS."""
    row = db.execute("SELECT value FROM meta WHERE key = ?", (SEED_KEY,)).fetchone()
    return row is not None and row[0] == _seed_version()


def seed(db: sqlite3.Connection) -> None:
    """Put the names of MANUFACTURERS and SHORT_NAMES into the registry. A name
    that the registry has stays with its manufacturer: a change of the user
    stays."""
    for name, long_names in MANUFACTURERS.items():
        aliases = (*long_names, *SHORT_NAMES.get(name, ()))
        mid = next((m for m in (maker_id(db, n) for n in (name, *aliases)) if m is not None), None)
        if mid is None:
            mid = _new(db, name, "builtin")
        for n in (name, *aliases):
            _add_alias(db, n, mid, "builtin")
    db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (SEED_KEY, _seed_version()))
