"""The filter model of the parametric part search, for each source.

A filter is a text "NAME OP VALUE", for example "Vdss >= 20 V",
"FET Type = N-Channel", "Supplier Device Package has 3x3" or
"Rds On <= 10 mOhm at Vgs=2.5V". OP is one of = < <= > >= has. "=" and "has"
accept more values with "|" (one of them). A condition after "at" or "@" tells
the conditions of the value.

A source keeps a parameter as a list of value texts, for example "25 V" or
"4.5mOhm @ 20A, 8V". A numeric filter compares the first quantity of the value
text before "@": "2.5V, 8V" gives 2.5 V, and "4.5mOhm @ 20A, 8V" gives
0.0045 Ohm.

A value text can have more points, each with its conditions after "@":
"4mΩ@4.5V, 6mΩ@2.5V" (JLCPCB). A filter with a condition ("at Vgs<=2.5V")
uses only the points with a condition that agrees. DigiKey keeps one point only,
at the highest drive voltage: there, a filter with a condition is not applied,
and the result tells the user to read the value in the datasheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import LensError

OPS = ("<=", ">=", "<", ">", "=", "has")
PREFIXES = {"p": 1e-12, "n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6,
            "G": 1e9}
# The units, longest first. "Ohm", "Ohms" and "Ω" are one unit.
UNITS = {"ohms": "Ohm", "ohm": "Ohm", "ω": "Ohm", "hz": "Hz", "°c": "°C", "℃": "°C", "v": "V", "a": "A", "f": "F",
         "h": "H", "w": "W", "s": "s", "%": "%"}
_UNIT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
# A number, then a prefix and a unit, or a unit only, or nothing. A prefix needs a unit:
# "5 m" is not 5 milli.
_QUANTITY = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:([pnµμumkKMG])(" + _UNIT + r")\b|(" + _UNIT + r")(?![a-z])|)",
                       re.IGNORECASE)
_OP = re.compile(r"\s*(<=|>=|<|>|=|\bhas\b)\s*", re.IGNORECASE)
_CONDITION = re.compile(r"\s+(?:at|@)\s+|\s*@\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str  # "" for a number with no unit


def quantity(text: str) -> Quantity | None:
    """The first quantity of a text, before "@": "4.5mOhm @ 20A, 8V" gives
    Quantity(0.0045, "Ohm"). None when the text has no number."""
    head = text.split("@", 1)[0]
    match = _QUANTITY.search(head)
    if not match:
        return None
    number, prefix, prefixed_unit, unit = match.groups()
    # A prefix letter with no unit after it is a unit itself ("1 A"): the regex
    # takes the prefix group only when a unit follows.
    scale = 1.0
    if prefixed_unit:
        if prefix in PREFIXES:
            scale = PREFIXES[prefix]
        elif prefix.lower() in PREFIXES:
            scale = PREFIXES[prefix.lower()]
        unit = prefixed_unit
    return Quantity(float(number) * scale, UNITS.get((unit or "").lower(), ""))


def points(text: str) -> list[tuple[Quantity, list[Quantity]]]:
    """The points of a value text: (value, conditions). "4mΩ@4.5V, 6mΩ@2.5V"
    gives two points. "35mΩ@10V,16A" and "4.5mOhm @ 20A, 8V" give one point
    with two conditions: a part with no "@" adds a condition to the point
    before it. A text with no "@" has no points."""
    out: list[tuple[Quantity, list[Quantity]]] = []
    for token in text.split(","):
        if "@" in token:
            head, tail = token.split("@", 1)
            value = quantity(head)
            if value is not None:
                out.append((value, [q for q in (quantity(x) for x in tail.split(";")) if q is not None]))
        elif out:
            condition = quantity(token)
            if condition is not None:
                out[-1][1].append(condition)
    return out


_CONDITION_OP = re.compile(r"^\s*(?:[A-Za-z][\w()]*\s*)?(<=|>=|<|>|=)?\s*(.+?)\s*$")


def _compare(a: float, op: str, b: float) -> bool:
    close = abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-30)
    return {"=": close, "<": a < b and not close, "<=": a < b or close, ">": a > b and not close,
            ">=": a > b or close}[op]


def _whole_quantity(text: str) -> Quantity | None:
    """The quantity when the text is a quantity only ("20 V", "10mOhm"), else None."""
    match = _QUANTITY.fullmatch(text.strip())
    return quantity(text) if match else None


@dataclass
class Filter:
    text: str  # as the user gave it
    name: str
    op: str
    values: list[str]  # more than one: one of them ("=" and "has")
    condition: str = ""
    number: Quantity | None = None  # for a numeric filter

    def _agrees(self, q: Quantity | None) -> bool:
        if q is None or (self.number.unit and q.unit and q.unit != self.number.unit):
            return False
        return _compare(q.value, self.op, self.number.value)

    def matches(self, value_text: str) -> bool:
        """True if a value text of the source agrees with the filter."""
        if self.op == "has":
            return any(v.lower() in value_text.lower() for v in self.values)
        if self.number is not None:
            return self._agrees(quantity(value_text))
        return any(v.lower() == value_text.strip().lower() for v in self.values)

    def condition_quantity(self) -> tuple[str, Quantity]:
        """The operator and the quantity of the condition: "Vgs<=2.5V" gives
        ("<=", 2.5 V), "2.5 V" gives ("=", 2.5 V). A LensError for a condition
        with no unit, or for a filter that is not numeric."""
        if self.number is None:
            raise LensError(f"filter {self.text!r}: a condition needs a numeric filter (< <= > >= = with a "
                            "number), for example \"rds_on <= 10 mOhm at Vgs<=2.5V\"")
        match = _CONDITION_OP.match(self.condition)
        q = _whole_quantity(match[2]) if match else None
        if q is None or not q.unit:
            raise LensError(f"filter {self.text!r}: the condition needs a number with its unit, for example "
                            "\"at Vgs<=2.5V\"")
        return match[1] or "=", q

    def matches_at(self, value_text: str) -> bool | None:
        """For a filter with a condition: True if a point with a condition that
        agrees has a value that agrees, False if such points exist and none
        agrees, None if the text has no point with such a condition. A
        condition agrees only with the same unit and the same sign: a P-channel
        point at -10 V is not a point at "Vgs<=2.5V" (give "Vgs>=-2.5V")."""
        op, cond = self.condition_quantity()
        found = [value for value, conditions in points(value_text)
                 if any(c.unit == cond.unit and (c.value < 0) == (cond.value < 0) and _compare(c.value, op, cond.value)
                        for c in conditions)]
        if not found:
            return None
        return any(self._agrees(v) for v in found)


def parse_filter(text: str) -> Filter:
    """A Filter from "NAME OP VALUE [at CONDITION]". A LensError for a text with
    no operator, no name or no value, or a < <= > >= with a value that is not a
    quantity."""
    match = _OP.search(text)
    if not match:
        raise LensError(f"filter {text!r}: give NAME OP VALUE, with OP one of {' '.join(OPS)} "
                        "(for example \"Vdss >= 20 V\")")
    name, op, rest = text[:match.start()].strip(), match[1].lower(), text[match.end():].strip()
    if not name or not rest:
        raise LensError(f"filter {text!r}: the name or the value is empty")
    parts = _CONDITION.split(rest, maxsplit=1)
    value, condition = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    values = [v.strip() for v in value.split("|") if v.strip()] if op in ("=", "has") else [value]
    if not values:
        raise LensError(f"filter {text!r}: the value is empty")
    number = _whole_quantity(values[0]) if len(values) == 1 and op != "has" else None
    if op in ("<", "<=", ">", ">=") and number is None:
        raise LensError(f"filter {text!r}: {op} needs a number with its unit, for example 20 V or 10 mOhm")
    return Filter(text=text.strip(), name=name, op=op, values=values, condition=condition, number=number)


@dataclass
class Parameter:
    """A parameter of a category at a source, with its values (ID and text)."""
    id: str
    name: str
    values: list[tuple[str, str]] = field(default_factory=list)


def resolve(name: str, parameters: list[Parameter], aliases: dict[str, str]) -> Parameter:
    """The parameter for a filter name: the same name (case is ignored), an
    alias of the source, or the only parameter whose name holds the text. A
    LensError for no parameter or for more parameters, with the names."""
    key = name.strip().lower()
    by_name = {p.name.lower(): p for p in parameters}
    if key in by_name:
        return by_name[key]
    alias = aliases.get(key.replace(" ", "_"))
    if alias and alias.lower() in by_name:
        return by_name[alias.lower()]
    found = [p for p in parameters if key in p.name.lower()]
    if len(found) == 1:
        return found[0]
    if found:
        raise LensError(f"filter name {name!r} fits more parameters: " + "; ".join(p.name for p in found)
                        + ". Give the full name.")
    raise LensError(f"no parameter {name!r} in this category. The parameters: "
                    + "; ".join(p.name for p in parameters))


def matching_values(flt: Filter, parameter: Parameter) -> list[tuple[str, str]]:
    """The values (ID, text) of the parameter that agree with the filter."""
    return [(vid, text) for vid, text in parameter.values if flt.matches(text)]
