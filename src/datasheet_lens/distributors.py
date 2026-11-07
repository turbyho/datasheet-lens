"""Part data from distributors through their official APIs: the concrete
orderable MPNs, their lifecycle state, stock, prices and datasheet URL.

Only official APIs, no scraping. Each user has a personal API key (a team key
on a server conflicts with the terms of some distributors): see keys.py. The
key never goes into a message, a log or the database.

Mouser (Search API, https://api.mouser.com/api/docs/V1, read on 2026-10-28):
POST /api/v1/search/partnumber with the key in the query. Exact gives the part
with the MPN; None gives up to 50 parts whose MPN starts with it (the concrete
MPNs of a base part). The terms of Mouser ask to name Mouser as the source of
its data, and not to mix it with other data with no distinction: each result
gives its source.

Mouser gives no package. Its ProductAttributes hold only the packaging options of
the Mouser part number and the standard pack quantity, with their names in the
language of the Mouser account ("Balení" for a Czech account). Thus a Mouser
offer has no package and no packaging (live calls, 2026-11-06). LifecycleStatus
is English for each language.

Each distributor has its own manufacturer names. A distributor name does not
change the manufacturer registry (makers.py): only the user changes it.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from . import makers
from .errors import Choose, LensError, Unavailable
from .keys import GROUPS, KEYS, get_key
from .keys import credential as _credential_of

MOUSER_ENV = KEYS["mouser"]
# The distributors that datasheet-lens can ask: name -> (label, where the user gets a key).
SUPPORTED = {"mouser": ("Mouser", "https://www.mouser.com/api-hub/ (the key of the Search API, not of the Cart "
                                   "API; free, with a Mouser account)"),
             "digikey": ("DigiKey", "https://developer.digikey.com (an organization, then a production app with "
                                    "\"Product Information V4\"; free, with a DigiKey account; the steps are in "
                                    "doc/distributors.md)")}
PLANNED = ("farnell", "tme")  # not supported yet
# What datasheet-lens can get from the API of each distributor, and what not. The
# agent reads it in distributor_status, to select the distributor for a task.
SERVICES = {
    "mouser": (["MPN search (search_distributors, project_availability): stock, prices, lifecycle state, "
                "suggested replacement of an obsolete part, datasheet URL"],
               ["parametric search (Mouser gives no electrical parameters)", "package"]),
    "digikey": (["parametric search (part_parameters, find_parts with source digikey): parameters, stock, price, lifecycle state, "
                 "package, datasheet URL",
                 "MPN search (search_distributors, project_availability): stock, prices, lifecycle state, "
                 "package, datasheet URL",
                 "direct links to the CAD models (SnapMagic, Ultra Librarian) and the datasheets (part_links)"],
                []),
    "jlcpcb": (["parametric search (part_parameters, find_parts with source jlcpcb): parameters with more "
                "points (RDS(on) at more Vgs), JLCPCB stock for the assembly, library class (Basic, Preferred, "
                "Extended), price in USD, package, datasheet URL",
                "MPN search (search_distributors, project_availability): JLCPCB stock and library class"],
               ["lifecycle state (the database has no obsolete parts)",
                "values of a known quality (LCSC listings: typical and maximum values mix, copies of values)"]),
}


def jlcpcb_ready() -> bool:
    """True when the user downloaded the JLCPCB database (datasheet-lens jlc update)."""
    from . import jlcpcb

    return jlcpcb.db_path().is_file()


def _jlcpcb_status() -> str:
    from . import jlcpcb

    s = jlcpcb.state()
    if not s:
        return ("jlcpcb: the local database is not on this machine. The user downloads it with \"datasheet-lens "
                "jlc update\", or an agent starts jlcpcb_library(action=\"update\") when the user agrees (276 MB "
                "download, 2.2 GB on the disk)")
    return f"jlcpcb: the local database is {s['age_days']:.1f} days old ({s['path']})"
# With no list of the user: the distributors with a saved key. With no key at all:
# Mouser, so that the user gets the note with the steps.
DEFAULT = ["mouser"]
MOUSER_URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"
TIMEOUT = 30.0
CACHE_SECONDS = 3600.0  # the same question in one hour: no second request (the API has a rate limit)
_cache: dict[tuple, tuple[float, list]] = {}


@dataclass
class Offer:
    """One orderable part at one distributor."""
    source: str
    mpn: str
    manufacturer: str = ""
    description: str = ""
    lifecycle: str = ""  # as the distributor gives it, for example "New Product", "Obsolete"
    discontinued: bool = False
    replacement: str = ""
    stock: int | None = None
    # More availability, in English: the quantities on order with their dates,
    # the stock of the factory (Mouser). Not the text of the distributor: Mouser
    # gives it in the language of the account ("11287 Na skladě", "Žádný").
    availability: str = ""
    lead_time: str = ""
    minimum: int | None = None
    multiple: int | None = None
    prices: list[tuple[int, str]] = field(default_factory=list)  # (quantity, price with currency)
    package: str = ""
    packaging: str = ""
    datasheet_url: str = ""
    product_url: str = ""
    distributor_pn: str = ""
    library: str = ""  # JLCPCB: Basic, Preferred or Extended (the assembly fee)


def check_names(names: list[str]) -> list[str]:
    """The distributor names in lower case, or a LensError for a name that
    datasheet-lens does not support."""
    out = []
    for name in (n.strip().lower() for n in names):
        if name == "jlcpcb":
            raise LensError("jlcpcb is not a distributor of this list: it is a local database. Run "
                            "\"datasheet-lens jlc update\" to download it, and \"datasheet-lens jlc remove\" to "
                            "delete it")
        if name in PLANNED:
            raise LensError(f"{name} is not supported yet (supported: {', '.join(SUPPORTED)})")
        if name not in SUPPORTED:
            raise LensError(f"unknown distributor {name!r} (supported: {', '.join(SUPPORTED)})")
        if name not in out:
            out.append(name)
    return out


def lookup_once():
    """A function that gives (credential, source) of a distributor, with one
    lookup of each key for each call of a tool: a locked key store waits
    RING_TIMEOUT seconds for each lookup."""
    found: dict[str, tuple] = {}

    def get(name: str) -> tuple:
        if name not in found:
            found[name] = _credential_of(name)
        return found[name]

    return get


def enabled(project=None, lookup=None) -> tuple[list[str], str, list[str]]:
    """The distributors to ask, where the list comes from (the project file,
    the global configuration or the default), and a note for each name of the
    list that is not supported (for example a list of a newer version): it is
    left out, so the others still work. A user in the USA does not want
    Farnell, for example. lookup: see lookup_once."""
    from .config import load_config

    if project is not None and getattr(project, "distributors", None) is not None:
        names, source = project.distributors, f"the project file {project.path}"
    else:
        names = load_config().get("distributors")
        if not isinstance(names, list):
            lookup = lookup or lookup_once()
            with_key = [n for n in SUPPORTED if lookup(n)[0]]
            return ((with_key, "the default: the distributors with a saved key", []) if with_key
                    else (list(DEFAULT), "the default", []))
        source = "the global configuration"
    out, skipped = [], []
    for name in (str(n).strip().lower() for n in names):
        if name in SUPPORTED:
            if name not in out:
                out.append(name)
        else:
            skipped.append(f"{name} (in {source}) is not supported" + (" yet" if name in PLANNED else "")
                           + ": datasheet-lens does not ask it")
    return out, source, skipped


def no_key_note(name: str, source: str = "") -> str:
    """The note for the user when an enabled distributor has no API key."""
    label, where = SUPPORTED[name]
    if name in GROUPS:
        labels = " and the ".join(lbl for _, lbl in GROUPS[name])
        envs = " and ".join(KEYS[key] for key, _ in GROUPS[name])
        paste = f"paste the {labels} (the command asks for each one; they go into the key store of the system; " \
                f"for CI: the environment variables {envs})"
    else:
        paste = (f"paste the key (it goes into the key store of the system; for CI: the environment variable "
                 f"{KEYS[name]})")
    return (f"{label}" + (f" is enabled ({source}) but" if source else "") + " has no API key. Tell the user: "
            f"get a key at {where}, then run \"datasheet-lens key {name}\" in a terminal and {paste}. Or "
            f"disable {label}: \"datasheet-lens dist\" shows the settings.")


def keys_of(project=None, lookup=None) -> tuple[dict[str, str | tuple[str, ...]], list[str]]:
    """The credentials of the enabled distributors ({name: key, or a tuple of
    keys for DigiKey}, one lookup of each key) and the notes for the enabled
    distributors with no key."""
    lookup = lookup or lookup_once()
    names, source, notes = enabled(project, lookup)
    keys: dict[str, str | tuple[str, ...]] = {}
    for name in names:
        key = lookup(name)[0]
        if key:
            keys[name] = key
        else:
            notes.append(no_key_note(name, source))
    return keys, notes


def _int(text) -> int | None:
    try:
        return int(str(text).replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None


# Mouser gives the lead time in the language of the account ("182 Dny", "6 Weeks").
LEAD_UNITS = {"days": ("day", "days", "den", "dny", "dnů", "tag", "tage", "jour", "jours", "día", "días"),
              "weeks": ("week", "weeks", "týden", "týdny", "týdnů", "woche", "wochen", "semaine", "semaines",
                        "semana", "semanas")}


def _lead_time(text: str) -> str:
    """The lead time in English ("182 days"), or the text as it is for an
    unknown unit. 0 or no number: "" (Mouser gives 0 when it has no lead time)."""
    match = re.fullmatch(r"(\d+)\s*(\w*)", text.strip())
    if not match:
        return text.strip()
    if int(match[1]) == 0:
        return ""
    unit = next((u for u, words in LEAD_UNITS.items() if match[2].lower() in words), "")
    return f"{int(match[1])} {unit}" if unit else text.strip()


def restricted(offer: Offer) -> bool:
    """True if the distributor does not sell the part now (Mouser: "Restricted
    Availability", DigiKey: "Discontinued at Digi-Key"). This is not a lifecycle
    state of the manufacturer."""
    state = offer.lifecycle.lower()
    return "restricted" in state or state == "discontinued at digi-key"


# The Mouser number of a part that Mouser lists with no number of its own (a
# live call, 2026-11-07: "N/A" for three manufacturers of SMAJ12A). It is not a
# number to order: the offer gets no number.
MOUSER_NO_NUMBER = "N/A"


def _mouser_availability(part: dict) -> str:
    """The availability of a Mouser part from its data fields, in English: the
    quantities on order (AvailabilityOnOrder, with the date) and the stock of
    the factory (FactoryStock). The text Availability is in the language of
    the account: usually a summary of the stock, which AvailabilityInStock
    gives. Only when no data field gives the state (no stock number, nothing
    on order), the text is the only data: then it comes as Mouser gives it,
    with its source ("Mouser: Non-Stocked")."""
    items = []
    for entry in part.get("AvailabilityOnOrder") or []:
        quantity = _int(entry.get("Quantity")) if isinstance(entry, dict) else None
        if quantity:
            date = str(entry.get("Date") or "")[:10]
            items.append(f"{quantity} on order" + (f" for {date}" if date else ""))
    factory = _int(part.get("FactoryStock"))
    if factory:
        items.append(f"{factory} at the factory")
    text = str(part.get("Availability") or "").strip()
    if not items and text and _int(part.get("AvailabilityInStock")) is None:
        items.append(f"Mouser: {text}")
    return ", ".join(items)


def price_text(number, currency: str) -> str:
    """A price as each source gives it in the results: the number with the
    currency code ("2012.6 CZK", "0.223 CZK"), with no exponent and no zeros at
    the end. "" when the number or the currency is missing or not valid: a
    price with no number or no currency is not a price."""
    currency = str(currency or "").strip()
    if number is None or isinstance(number, bool) or not currency:
        return ""
    try:
        value = Decimal(str(number).strip())
    except InvalidOperation:
        return ""
    return f"{format(value.normalize(), 'f')} {currency}" if value.is_finite() else ""


def _mouser_price(price_break: dict) -> str:
    """The price of a Mouser price break: the number RawPrice with the currency
    code (price_text). The text Price is in the format of the account ("2 012,60
    Kč", with its own currency sign). With no RawPrice or no currency code, the
    text comes as Mouser gives it, with its source and the currency code."""
    currency = str(price_break.get("Currency") or "").strip()
    text = price_text(price_break.get("RawPrice"), currency)
    if text:
        return text
    shown = str(price_break.get("Price") or "").strip()
    if not shown:
        return ""
    return f"Mouser: {shown}" + (f" ({currency})" if currency and currency not in shown else "")


def _mouser_offer(part: dict) -> Offer:
    number = str(part.get("MouserPartNumber", "")).strip()
    return Offer(
        source="Mouser", mpn=str(part.get("ManufacturerPartNumber", "")).strip(),
        manufacturer=str(part.get("Manufacturer", "")).strip(),
        description=str(part.get("Description", "")).strip(),
        lifecycle=str(part.get("LifecycleStatus") or "").strip(),
        discontinued=str(part.get("IsDiscontinued", "")).lower() == "true",
        replacement=str(part.get("SuggestedReplacement") or "").strip(),
        stock=_int(part.get("AvailabilityInStock")), availability=_mouser_availability(part),
        lead_time=_lead_time(str(part.get("LeadTime") or "")), minimum=_int(part.get("Min")),
        multiple=_int(part.get("Mult")),
        prices=[(quantity, price) for quantity, price in ((_int(p.get("Quantity")) or 0, _mouser_price(p))
                                                         for p in part.get("PriceBreaks") or [] if isinstance(p, dict))
                if price],
        datasheet_url=str(part.get("DataSheetUrl") or "").strip(),
        product_url=str(part.get("ProductDetailUrl") or "").strip(),
        distributor_pn="" if number.upper() == MOUSER_NO_NUMBER else number)


MIN_PREFIX = 3  # a shorter start of an MPN gives 50 parts of no use


def fits(text: str, search: str, exact: bool) -> bool:
    """True if a part number is the search text (exact) or starts with it. Case
    is ignored. The rule of each MPN search of the distributors."""
    text, search = text.strip().upper(), search.strip().upper()
    return text == search if exact else text.startswith(search)
MAX_CACHE = 500


def mouser_search(mpn: str, exact: bool = True, key: str = "") -> list[Offer]:
    """The parts of Mouser for an MPN: exact, or each part whose MPN starts with
    it (up to 50). key: the API key (empty: look it up). A LensError with no key,
    or when the API gives an error; Unavailable when it cannot answer now."""
    mpn = mpn.strip()
    if not mpn:
        raise LensError("the MPN is empty")
    if "|" in mpn:  # Mouser reads "|" as a separator of more MPNs
        raise LensError(f"not one MPN: {mpn}")
    if not exact and len(mpn) < MIN_PREFIX:
        raise LensError(f"give {MIN_PREFIX} or more characters of the MPN")
    key = key or get_key("mouser")
    if not key:
        raise LensError(no_key_note("mouser"))
    now = time.monotonic()
    cached = _cache.get(("Mouser", mpn.upper(), exact))
    if cached and now - cached[0] < CACHE_SECONDS:
        return list(cached[1])
    body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": mpn,
                                               "partSearchOptions": "Exact" if exact else "None"}}).encode()
    request = urllib.request.Request(MOUSER_URL.format(key=quote(key, safe="")), data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # not str(e) of the request: the URL holds the key
        e.close()
        error = Unavailable if e.code == 429 or e.code >= 500 else LensError
        raise error(f"Mouser API: HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise Unavailable(f"Mouser API: no connection ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise LensError("Mouser API: the answer is not JSON") from None
    if not isinstance(data, dict):
        raise LensError("Mouser API: the answer has no search results")
    errors = [e.get("Message") or e.get("Code") or "error" for e in data.get("Errors") or [] if isinstance(e, dict)]
    if errors:
        text = "; ".join(errors).rstrip(".")
        if "unique identifier" in text.lower():  # the answer of Mouser for a wrong key or a key not approved yet
            text += (". The API key is not valid: it is the key of the Cart API (not of the Search API), Mouser "
                     "has not approved it yet, or it is not correct. Tell the user: check the Search API key at "
                     "https://www.mouser.com/api-hub/, then save it again with \"datasheet-lens key mouser\"")
        raise LensError(f"Mouser API: {text}")
    parts = (data.get("SearchResults") or {}).get("Parts") or []
    offers = [_mouser_offer(p) for p in parts if isinstance(p, dict) and p.get("ManufacturerPartNumber")]
    # Mouser also gives other parts: for "Exact", MPNs that only start with the
    # text (SMAJ12A-TP for SMAJ12A), and for a start, parts with a suggested
    # replacement that agrees (TPS79433DGNT for TPS7A2033, a live call,
    # 2026-11-07). The search text is an MPN or a Mouser number.
    offers = [o for o in offers if fits(o.mpn, mpn, exact) or fits(o.distributor_pn, mpn, exact)]
    if len(_cache) >= MAX_CACHE:  # remove the old answers
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_SECONDS] or list(_cache):
            del _cache[k]
    _cache[("Mouser", mpn.upper(), exact)] = (now, offers)
    return list(offers)


def search_lines(mpn: str, exact: bool = False, project=None) -> list[str]:
    """The result of a search at the enabled distributors as lines, each with
    its source. An enabled distributor with no key gives a note for the user.
    With no key at all: the search links of part_links."""
    from .links import format_links

    if not mpn.strip():
        raise LensError("the MPN is empty")
    keys, notes = keys_of(project)
    jlc = jlcpcb_ready()
    if not keys and not jlc:
        return notes + ["Search links:"] + format_links(mpn)
    lines = []
    for name, credential in keys.items():
        label = SUPPORTED[name][0]
        try:
            offers = offers_of(name, mpn, exact, credential)
        except LensError as e:  # the other distributors still answer
            lines.append(f"Source: {label}. Not checked: {e}")
            continue
        lines += ([f"Source: {label} ({len(offers)} parts)"] + [format_offer(o) for o in offers] if offers
                  else [f"Source: {label}. No part {'is' if exact else 'starts with'} {mpn.strip()}."])
    if jlc:
        from . import jlcpcb

        try:
            offers = jlcpcb.offers(mpn, exact)
        except (LensError, sqlite3.Error) as e:  # the distributors still answer
            lines.append(f"Source: JLCPCB parts library. Not checked: {e}")
        else:
            lines += ([f"Source: JLCPCB parts library ({len(offers)} parts)"] + [format_offer(o) for o in offers]
                      if offers else [f"Source: JLCPCB parts library. No part {'is' if exact else 'starts with'} "
                                      f"{mpn.strip()}."])
        note = jlcpcb.age_note()
        lines += [note] if note else []
    if not keys:  # no distributor: the search links too
        lines += notes + ["Search links:"] + format_links(mpn)
        return lines
    return lines + notes


def offers_of(name: str, mpn: str, exact: bool, credential) -> list[Offer]:
    """The offers of one distributor for an MPN."""
    if name == "mouser":
        return mouser_search(mpn, exact, credential)
    from . import digikey

    return digikey.offers(mpn, exact, credential)


def not_orderable(offer: Offer) -> str:
    """The reason why an offer cannot be ordered, or "": no part number of the
    distributor (Mouser gives "N/A"), obsolete, discontinued, end of life. A
    part that only the distributor does not sell now (restricted) is not in this
    group."""
    state = offer.lifecycle.lower()
    if offer.discontinued or (any(w in state for w in ("obsolete", "discontinued", "end of life", "eol"))
                              and not restricted(offer)):
        return f"{offer.lifecycle or 'discontinued'}" + (f", replacement {offer.replacement}" if offer.replacement
                                                          else "")
    if not offer.distributor_pn:
        return f"{offer.source} gives no part number to order"
    return ""


def format_offer(o: Offer) -> str:
    """One line for an offer: MPN, manufacturer, the number of the distributor
    (adopt_part takes it), lifecycle (Mouser gives it only for some parts),
    stock, prices, the package when the distributor gives it, and the datasheet
    URL."""
    prices = ", ".join(f"{q}: {p}" for q, p in o.prices[:3])
    warn = not_orderable(o)
    parts = [f"{o.mpn} ({o.manufacturer or 'manufacturer unknown'})"]
    if o.distributor_pn:
        parts.append(f"{o.source} {o.distributor_pn}")
    marks = [f"state: {o.lifecycle}" if o.lifecycle else "", f"[NOT ORDERABLE: {warn}]" if warn else "",
             "[the distributor does not sell it now; not a lifecycle state of the manufacturer]"
             if restricted(o) else ""]
    if any(marks):
        parts.append(" ".join(m for m in marks if m))
    parts.append(f"stock: {o.stock if o.stock is not None else 'unknown'}"
                 + (f" ({o.availability})" if o.availability else "")
                 + (" (NOT IN STOCK at JLCPCB: the assembly needs an order of the part)"
                    if o.library and o.stock == 0 else ""))
    if o.lead_time:
        parts.append(f"lead time {o.lead_time}")
    if prices:
        parts.append(f"price {prices}")
    if o.library:
        parts.append(f"library: {o.library}")
    if o.package or o.packaging:
        parts.append(" / ".join(x for x in (o.package, o.packaging) if x))
    if o.datasheet_url:
        parts.append(f"datasheet {o.datasheet_url}")
    return "; ".join(parts)


def _of_maker(offers: list[Offer], manufacturer: str, store=None) -> tuple[list[Offer], bool]:
    """The offers of the manufacturer of the part and True. Only an MPN with
    offers of two or more manufacturers (a second source) needs the comparison
    of the names (see makers.same): a distributor writes the names in its own
    form (Mouser: "Analog Devices / Maxim Integrated"). With no offer of the
    manufacturer: all offers and False."""
    if len({o.manufacturer for o in offers}) <= 1:
        return offers, True
    db = store.db if store is not None else None
    own = [o for o in offers if manufacturer and makers.same(db, o.manufacturer, manufacturer)]
    return (own, True) if own else (offers, False)


# The lifecycle states of the manufacturer that are a risk for a design, the most
# severe first, with the words that the distributors use for them (Mouser
# LifecycleStatus, DigiKey ProductStatus). The documentation of the two APIs gives
# no list of the values: a state with none of these words is shown as the source
# gives it, with no risk.
RISKS = (("obsolete", r"obsolete|end of life|eol|discontinued"),
         ("last time buy", r"last time buy|ltb"),
         ("not for new designs", r"not (?:recommended )?for new designs?|nrnd"))
RISK_NAMES = [name for name, _ in RISKS]


def risk_of(offer: Offer) -> str:
    """The lifecycle risk of the manufacturer for an offer (a name of RISKS), or
    "". A state of the distributor only (restricted) is not a state of the
    manufacturer."""
    if restricted(offer):
        return ""
    if offer.discontinued:
        return "obsolete"
    state = offer.lifecycle.lower()
    return next((name for name, words in RISKS if re.search(rf"\b(?:{words})\b", state)), "")


def _worst(risks) -> str:
    """The most severe lifecycle risk (RISKS) of a list, or ""."""
    found = [r for r in risks if r]
    return min(found, key=RISK_NAMES.index) if found else ""


def lifecycle_of(offers: list[Offer]) -> tuple[str, str, str]:
    """The lifecycle state of an MPN at one source, from its offers (the
    packagings): (risk, state, replacement). The orderable offers count first:
    an obsolete packaging of an MPN that has an orderable packaging is not a
    state of the MPN. The most severe risk of these offers wins, thus the
    result does not depend on the order of the offers. The state and the
    replacement are of the offer with this risk."""
    base = [o for o in offers if not not_orderable(o)] or offers
    if not base:
        return "", "", ""
    o = min(base, key=lambda x: RISK_NAMES.index(risk_of(x)) if risk_of(x) else len(RISK_NAMES))
    return risk_of(o), o.lifecycle or ("discontinued" if o.discontinued else ""), o.replacement


def maker_lifecycle(name: str, credential, mpn: str, manufacturer: str,
                    store=None) -> tuple[tuple[str, str, str] | None, str]:
    """The lifecycle state of the manufacturer for an MPN at one distributor:
    (lifecycle_of, "") or (None, the reason). Only the offers of the
    manufacturer of the part count (_of_maker), as in availability."""
    label = SUPPORTED[name][0]
    try:
        offers = [o for o in offers_of(name, mpn, True, credential) if fits(o.mpn, mpn, True)]
    except (LensError, Unavailable) as e:
        return None, f"{label}: {e}"
    if not offers:
        return None, f"{label} does not have this MPN"
    offers, own = _of_maker(offers, manufacturer, store)
    if not own:
        return None, (f"{label} has no offer from {manufacturer}" if manufacturer.strip()
                      else f"{label} has offers of more manufacturers, and the part has no manufacturer")
    return lifecycle_of(offers), ""


@dataclass
class _SourceState:
    """The availability of one project part at one source (see availability)."""
    label: str  # Mouser, DigiKey, JLCPCB
    numbers: list[str] = field(default_factory=list)  # the purchase source numbers of the source
    text: str = ""  # the details
    warning: bool = False  # the details have a warning
    error: str = ""  # not checked: the reason
    found: bool = False  # the source has offers of the part (its MPN, its manufacturer)
    missing: list[str] = field(default_factory=list)  # the purchase source numbers that the source does not have
    wrong: list[str] = field(default_factory=list)  # the purchase source numbers of a different MPN
    others: bool = False  # the source has the MPN only from other manufacturers
    orderable: bool = False  # an offer of the part can be ordered now
    stock: int = 0  # the stock of the orderable offers
    risk: str = ""  # the lifecycle risk of the manufacturer (RISKS)
    lifecycle: str = ""  # the lifecycle state as the source gives it
    replacement: str = ""  # the replacement that the source suggests
    lines: list[str] = field(default_factory=list)  # more lines: the replacements in the library


def availability(project, store=None, not_fitted=()) -> list[str]:
    """The availability of each project part at all enabled sources, as lines:
    one verdict for each part from all sources, then the details of each source
    (see _verdict). A purchase source at a source that the tool cannot ask (no
    key, not enabled, no JLCPCB library) is not checked, and the verdict tells
    it. After the parts: the lines of the sources (the parts with no purchase
    source, a source that does not answer, the age of the JLCPCB library), and
    a note for each enabled distributor with no key. store: its manufacturer
    registry selects the offers of the manufacturer of the part. not_fitted:
    the MPNs of the parts that no symbol fits (DNP, check_parts
    not_fitted_parts): the assembly does not place them, thus their verdict is
    information only (decision of the user, 2026-11-07)."""
    keys, notes = keys_of(project)
    jlc = jlcpcb_ready()
    if not keys and not jlc:
        return notes or ["No enabled distributor: \"datasheet-lens dist\" shows the settings."]
    states: list[list[_SourceState]] = [[] for _ in project.parts]
    after: list[str] = []
    for name, credential in keys.items():
        for row, state in zip(states, _states_at(name, credential, project, store, after)):
            row.append(state)
    if jlc:  # after the distributors: their lifecycle risk selects the replacements
        risks = [_worst(s.risk for s in row) for row in states]
        for row, state in zip(states, _states_jlcpcb(project, store, risks, after)):
            row.append(state)
    asked = set(keys) | ({"jlcpcb"} if jlc else set())
    dnp = {m.strip().lower() for m in not_fitted}
    lines: list[str] = []
    for part, row in zip(project.parts, states):
        for source in dict.fromkeys(s["source"] for s in part.get("sources", [])):
            if source not in asked:
                label = "JLCPCB" if source == "jlcpcb" else SUPPORTED[source][0]
                row.append(_SourceState(label, source_numbers(part, source), error=(
                    "the JLCPCB parts library is not on this machine (datasheet-lens jlc update)"
                    if source == "jlcpcb" else f"{label} is not enabled or has no API key")))
        lines += _verdict(part, row, part["mpn"].lower() in dnp)
    return lines + after + notes


def _verdict(part: dict, states: list[_SourceState], dnp: bool = False) -> list[str]:
    """The lines of one part: the verdict from all sources, then the details.

    The verdict starts with a lifecycle risk of the manufacturer at any source,
    with the state of each source (thus a difference between the sources
    shows), and asks for a replacement also when a source has stock. Then: the
    purchase source can deliver, or the reasons why not and the other sources
    with stock. A part with no purchase source: the sources with stock. A part
    with no warning gets one line with the details of each source; a part with
    a warning gets one line for each source. dnp: no symbol fits the part, thus
    the verdict is information, with no WARNING."""
    numbers = [n for s in states for n in s.numbers]
    head = part["mpn"] + (f" ({', '.join(numbers)}, the purchase source{'s' if len(numbers) > 1 else ''})"
                          if numbers else "")

    def stocked(s: _SourceState) -> bool:
        return s.orderable and s.stock > 0

    def why(s: _SourceState) -> str:
        """Why a purchase source cannot deliver."""
        if s.error:
            return "not checked"
        reasons = (([f"does not have {', '.join(s.missing)}"] if s.missing else [])
                   + ([f"{', '.join(s.wrong)} is an other MPN" if len(s.wrong) == 1
                       else f"{', '.join(s.wrong)} are other MPNs"] if s.wrong else []))
        if s.found:
            reasons.append("no stock" if s.orderable else "not orderable")
        return "; ".join(reasons) or "no offer"

    purchase = [s for s in states if s.numbers]
    unchecked = [s.label for s in states if s.error]
    warn = True
    if purchase and any(stocked(s) for s in purchase):
        order, warn = f"orderable at the purchase source ({', '.join(s.label for s in purchase if stocked(s))})", False
    elif purchase:
        others = [s.label for s in states if not s.numbers and stocked(s)]
        order = (f"the purchase source cannot deliver ({'; '.join(f'{s.label}: {why(s)}' for s in purchase)}). "
                 + (f"In stock at {', '.join(others)}" if others else "No other source has stock"))
    elif any(stocked(s) for s in states):
        order, warn = f"orderable ({', '.join(s.label for s in states if stocked(s))})", False
    elif any(s.orderable for s in states):
        order = "no stock at any source"
    elif any(s.found for s in states):
        order = "NOT ORDERABLE at any source"
    elif any(s.others for s in states):
        order = f"no offer of the manufacturer of the part ({', '.join(s.label for s in states if s.others)})"
    elif len(unchecked) < len(states):
        order = "no source has this MPN (a base part, or a wrong MPN?)"
    else:
        order, warn = "not checked", False
    if unchecked and len(unchecked) < len(states):
        order += f". Not checked: {', '.join(unchecked)}"
    risk = _worst(s.risk for s in states)
    if risk:
        sources = "; ".join(f"{s.label}: {s.lifecycle}" + (f", replacement {s.replacement}" if s.replacement else "")
                            for s in states if s.lifecycle)
        order = (f"WARNING: {risk.upper()} at the manufacturer ({sources}). {order[0].upper()}{order[1:]}"
                 # A part that the assembly does not place needs no replacement now.
                 + ("" if dnp else ". Select a replacement with the user (find_parts with the values of the "
                                   "datasheet)"))
    elif warn:
        order = f"WARNING: {order}"
    if dnp:
        order = order.replace("WARNING: ", "")
        order = ("not fitted (DNP), information only: the assembly does not place the part. "
                 f"{order[0].upper()}{order[1:]}")
    details = [(s.label, f"not checked: {s.error}" if s.error else s.text) for s in states]
    more = [f"  {line}" for s in states for line in s.lines]
    if not (risk or warn or unchecked or any(s.warning for s in states)):
        return [f"{head}: {order}. " + "; ".join(f"{label}: {text}" for label, text in details)] + more
    return [f"{head}: {order}"] + [f"  {label}: {text}" for label, text in details] + more


def source_numbers(part: dict, source: str) -> list[str]:
    """The part numbers of the purchase sources of a source for a project part
    ([[part.source]]): more than one when the project orders more packagings
    (cut tape and reel). project_remove_source removes one."""
    return [s["number"] for s in part.get("sources", []) if s["source"] == source]


def _no_source_line(project, source: str, label: str) -> list[str]:
    """One line with the project parts that have no purchase source of the
    source, when other parts have one: they are checked by their MPN."""
    if not any(source_numbers(p, source) for p in project.parts):
        return []
    missing = [p["mpn"] for p in project.parts if not source_numbers(p, source)]
    return [f"No purchase source of {label} in the project file (checked by the MPN): {', '.join(missing)}. "
            f"adopt_part saves one."] if missing else []


def _part_offers(part: dict, source: str, by_number, by_mpn, store=None) -> tuple[list, bool, list, list]:
    """The offers of a project part at one source, for its availability: the
    offers of its purchase source numbers when the project file has them (the
    packagings that the project orders; the selection of the user, thus no
    comparison of the manufacturer), else the offers of its MPN from the
    manufacturer of the part (_of_maker). Gives (offers, own, numbers, missing):
    own is False when the source has no offer of the manufacturer of the part;
    missing: the numbers that the source does not have."""
    numbers = source_numbers(part, source)
    if numbers:
        offers, missing = [], []
        for number in numbers:
            found = by_number(number)
            offers += found
            if not found:
                missing.append(number)
        return offers, True, numbers, missing
    offers = by_mpn(part["mpn"])
    if not offers:
        return [], True, [], []
    offers, own = _of_maker(offers, part.get("manufacturer", ""), store)
    return offers, own, [], []


def _missing(label: str, numbers: list[str]) -> str:
    """The details of a source that has no offer of a part. A purchase source
    number that the source does not have is a warning. An MPN that one source
    does not have is not: a different source can sell it, and the verdict tells
    when no source has it."""
    if numbers:
        return (f"WARNING: {label} does not have {', '.join(numbers)}, "
                f"{'the purchase sources' if len(numbers) > 1 else 'the purchase source'} in the project file")
    return "does not have this MPN"


def _number_warnings(part: dict, numbers: list[str], offers: list, missing: list[str], label: str) -> str:
    """The warnings of the purchase source numbers of a part: a number that is a
    different MPN, and a number that the source does not have (when other
    numbers are there). "" with no purchase source."""
    if not numbers:
        return ""
    text = "".join(f". WARNING: {o.distributor_pn} is {o.mpn} ({o.manufacturer or 'manufacturer unknown'}), "
                   f"not {part['mpn']}" for o in offers if not fits(o.mpn, part["mpn"], True))
    return text + (f". {_missing(label, missing)}" if missing and offers else "")


def _maker_warning(label: str, part: dict, offers: list) -> str:
    """The warning of a source that has the MPN only from other manufacturers
    (_of_maker), or from more manufacturers when the part has no manufacturer:
    these offers are not offers of the part."""
    names = ", ".join(sorted({x.manufacturer or "manufacturer unknown" for x in offers}))
    if part.get("manufacturer"):
        return f". WARNING: {label} has no part from {part['manufacturer']}: the data is of {names}"
    return (f". WARNING: {label} has the MPN from more manufacturers ({names}), and the project file gives no "
            "manufacturer of the part")


def _numbers_state(s: _SourceState, part: dict, numbers: list[str], offers: list, missing: list[str],
                   label: str) -> str:
    """Set the numbers that the source does not have or that are a different
    MPN, and give their warnings (_number_warnings)."""
    s.missing = list(missing)
    s.wrong = list(dict.fromkeys(o.distributor_pn for o in offers if not fits(o.mpn, part["mpn"], True)))
    text = _number_warnings(part, numbers, offers, missing, label)
    s.warning = s.warning or bool(text)
    return text


def _states_jlcpcb(project, store, risks: list[str], after: list[str]) -> list[_SourceState]:
    """The state of each project part in the JLCPCB parts library: the JLCPCB
    stock for the assembly and the library class (see _part_offers), and the
    replacements (jlcpcb.replacements) of a part with no JLCPCB stock, with a
    loading fee (Extended), or with a lifecycle risk of the manufacturer (risks:
    one for each part, from the distributors; the library has no lifecycle
    state). One connection to the database."""
    from . import jlcpcb

    name = "the JLCPCB parts library"
    registry = store.db if store is not None else None
    states = [_SourceState("JLCPCB", source_numbers(p, "jlcpcb")) for p in project.parts]
    try:
        db = jlcpcb.connect()
    except (LensError, sqlite3.Error) as e:
        for s in states:
            s.error = "the library cannot be read"
        after.append(f"JLCPCB parts library: not checked: {e}")
        return states
    try:
        for part, risk, s in zip(project.parts, risks, states):
            try:
                offers, own, numbers, missing = _part_offers(
                    part, "jlcpcb", lambda n: [o for o in [jlcpcb.offer_of_number(n, db)] if o is not None],
                    lambda mpn: jlcpcb.offers(mpn, exact=True), store)
            except (LensError, sqlite3.Error) as e:
                s.error = str(e)
                continue
            if not offers:
                s.missing, s.text, s.warning = list(missing), _missing(name, numbers), bool(numbers)
                continue
            # Each C number is a part of its own: the one with the most stock first, then the others.
            offers = sorted(offers, key=lambda x: -(x.stock or 0))
            o = offers[0]
            others = offers[1:] if numbers else []
            text = (f"stock {o.stock}, library {o.library} ({o.distributor_pn})"
                    + "".join(f"; {x.distributor_pn}: stock {x.stock}, library {x.library}" for x in others)
                    + _numbers_state(s, part, numbers, offers, missing, name))
            # Each C number of the project file is a part of its own: its stock warning
            # and its replacements. With no purchase source: the offer with the most stock.
            # A number of a different MPN, and an offer of a different manufacturer, are
            # not offers of the part.
            checked = [x for x in (offers if numbers else [o]) if fits(x.mpn, part["mpn"], True)] if own else []
            s.found = s.orderable = bool(checked)
            s.others = not own
            s.stock = sum(x.stock or 0 for x in checked)
            for x in checked:
                if not x.stock:
                    s.warning = True
                    text += (". WARNING: " + (f"{x.distributor_pn} " if len(checked) > 1 else "")
                             + "NOT IN STOCK at JLCPCB: the assembly needs an order of the part")
            if not own:
                s.warning = True
                text += _maker_warning("the JLCPCB library", part, offers)
            s.text = text
            # Replacements for a part with no JLCPCB stock, with a loading fee (Extended),
            # or with a lifecycle risk of its manufacturer. With a risk, the same MPN of
            # the manufacturer of the part is no replacement (the registry compares the
            # names of the library and of the project file).
            for x in (x for x in checked if risk or not x.stock or x.library == "Extended"):
                makers_of_part = [m for m in (x.manufacturer, part.get("manufacturer", "")) if m]
                try:
                    found = jlcpcb.replacements(
                        x.distributor_pn, db=db, obsolete=bool(risk),
                        same_maker=lambda maker, names=makers_of_part: any(
                            makers.same(registry, maker, m) for m in names))
                except (LensError, sqlite3.Error) as e:
                    s.lines.append(f"replacements of {x.distributor_pn}: not checked: {e}")
                    continue
                s.lines += ["replacement" + (f" of {x.distributor_pn}" if len(checked) > 1 else "")
                            + f": {r.distributor_pn} {r.mpn} ({r.manufacturer or 'manufacturer unknown'}), "
                            f"library {r.library}, JLCPCB stock {r.stock}, {r.package} ({why}): read its datasheet "
                            "before you use it" for r, why in found]
    finally:
        db.close()
    note = jlcpcb.age_note()
    after += _no_source_line(project, "jlcpcb", "JLCPCB") + ([note] if note else [])
    return states


def _offers_by_number(name: str, number: str, credential) -> list[Offer]:
    """The offer of a part number of a distributor (a purchase source): the
    packaging of this number, with its prices and its stock."""
    if name == "mouser":
        return [o for o in mouser_search(number, True, credential) if fits(o.distributor_pn, number, True)]
    from . import digikey

    product = digikey.product(number, credential)
    if product is None:
        return []
    try:
        return [digikey.offer_of(product, digikey.locale()[1], number)]
    except LensError:  # the number is an MPN, not a packaging of the product
        return []


def _states_at(name: str, credential, project, store, after: list[str]) -> list[_SourceState]:
    """The state of each project part at one distributor (see _part_offers). A
    distributor that does not answer is not asked for the next parts: one line
    in after names them."""
    label = SUPPORTED[name][0]
    states = [_SourceState(label, source_numbers(p, name)) for p in project.parts]
    for i, (part, s) in enumerate(zip(project.parts, states)):
        try:
            offers, own, numbers, missing = _part_offers(
                part, name, lambda n: _offers_by_number(name, n, credential),
                lambda mpn: [o for o in offers_of(name, mpn, True, credential) if fits(o.mpn, mpn, True)], store)
        except Unavailable as e:  # the next parts fail too: do not wait for each one
            for rest in states[i:]:
                rest.error = f"{label} does not answer"
            after.append(f"not checked: {', '.join(p['mpn'] for p in project.parts[i:])}: {e}")
            break
        except LensError as e:
            s.error = str(e)
            continue
        if not offers:
            s.missing, s.text, s.warning = list(missing), _missing(label, numbers), bool(numbers)
            continue
        # The offers of the part: not a purchase source number of a different MPN, and
        # not an offer of a different manufacturer (the details still show their data).
        mine = [o for o in offers if fits(o.mpn, part["mpn"], True)] if own else []
        # One MPN can have more offers (cut tape and reel): the part is orderable
        # when one offer is, and its stock is the sum.
        sellable = [o for o in mine or offers if not not_orderable(o)]
        o = (sellable or mine or offers)[0]
        stocks = [x.stock for x in sellable or mine or offers if x.stock is not None]
        stock = sum(stocks) if stocks else None
        now = [x for x in sellable if not restricted(x)] if mine else []  # the distributor sells it now
        s.found, s.orderable, s.others = bool(mine), bool(now), not own
        s.stock = sum(x.stock or 0 for x in now)
        if mine:
            s.risk, s.lifecycle, s.replacement = lifecycle_of(mine)
        text = ((f"{o.lifecycle}, " if o.lifecycle else "") + f"stock {stock if stock is not None else 'unknown'}"
                + _numbers_state(s, part, numbers, offers, missing, label))
        warned = True
        if not sellable:
            text += f". WARNING: not orderable: {not_orderable(o)}"
        elif not stock and all(restricted(x) for x in offers):
            text += (f". WARNING: {label} does not sell this part now ({o.lifecycle}). This is not the "
                     "lifecycle state of the manufacturer: ask the manufacturer or a different distributor")
        elif not stock:
            # The quantities on order of each offer (cut tape and reel).
            more = ", ".join(dict.fromkeys(x.availability for x in sellable if x.availability))
            text += (". WARNING: no stock" + (f" ({more})" if more else "")
                     + (f", lead time {o.lead_time}" if o.lead_time else ""))
        else:
            warned = False
        if not own:
            warned = True
            text += _maker_warning(label, part, offers)
        s.warning = s.warning or warned
        s.text = text
    after += _no_source_line(project, name, label)
    return states


def status_lines(project=None) -> list[str]:
    """The enabled distributors, where the list comes from, and the state of
    their keys (no values), with a note for each missing key, and the services
    of each supported distributor."""
    lookup = lookup_once()
    names, source, skipped = enabled(project, lookup)
    lines = [f"enabled distributors: {', '.join(names) or 'none'} (from {source}); supported: "
             f"{', '.join(SUPPORTED)}; not supported yet: {', '.join(PLANNED)}"] + skipped
    for name in names:
        value, where = lookup(name)
        lines.append(f"{name}: key {where}" if value else
                     (f"{name}: key {where}; " if where != "not set" else "") + no_key_note(name, source))
    lines.append(_jlcpcb_status())
    lines.append("The services of each source (jlcpcb is not in the distributor list: \"datasheet-lens jlc "
                 "update\" enables it):")
    for name, (has, lacks) in SERVICES.items():
        enabled_now = jlcpcb_ready() if name == "jlcpcb" else name in names
        lines.append(f"{name}: " + "; ".join(has) + (f". Not available: {'; '.join(lacks)}" if lacks else "")
                     + ("" if enabled_now else ". Not enabled"))
    return lines


# The distributors with a parametric search (see parametric.py).
PARAMETRIC = ("digikey",)


def _parametric_credential(project=None) -> tuple[str, object]:
    """The first enabled distributor with a parametric search and a key, and
    its credential. A LensError with the steps when there is none: enable the
    distributor, get a key, or both."""
    lookup = lookup_once()
    names, source, _ = enabled(project, lookup)
    for name in PARAMETRIC:
        if name in names and lookup(name)[0]:
            return name, lookup(name)[0]
    steps = []
    for name in PARAMETRIC:
        label = SUPPORTED[name][0]
        enable = f"\"datasheet-lens dist {' '.join([*names, name])}\" (or with --local for this project)"
        if name in names:
            steps.append(no_key_note(name, source))
        elif lookup(name)[0]:
            steps.append(f"{label} has a key but is not enabled ({source}). Tell the user to enable it: {enable}.")
        else:
            steps.append(no_key_note(name) + f" Then enable it: {enable}.")
    raise LensError("The parametric search needs a distributor with a parameter search: "
                    + ", ".join(SUPPORTED[n][0] for n in PARAMETRIC) + ". " + " ".join(steps))


PARAMETRIC_SOURCES = ("digikey", "jlcpcb")


def _source(source: str, project=None) -> tuple[str, object]:
    """The source of the parametric search and its credential. The caller
    always names the source: the categories, the parameters and the meaning of
    the stock differ between the sources."""
    source = source.strip().lower()
    if source not in PARAMETRIC_SOURCES:
        raise LensError(f"give the source of the search: {' or '.join(PARAMETRIC_SOURCES)} (digikey: the stock "
                        "and the lifecycle at DigiKey; jlcpcb: the JLCPCB stock for the assembly and the library "
                        "class)")
    if source == "jlcpcb":
        if not jlcpcb_ready():
            from . import jlcpcb

            jlcpcb.connect()  # the LensError with the step for the user
        return "jlcpcb", None
    _, cred = _parametric_credential(project)
    return "digikey", cred


def pick_lines(source: str, category: str, filters: list[str], keywords: str = "", in_stock: bool = True,
               sort: str = "stock", limit: int = 20, offset: int = 0, project=None) -> list[str]:
    """The parametric search at a source (digikey or jlcpcb), as lines with the
    source. A category text that fits more categories gives the list to select
    from, as a normal result."""
    name, cred = _source(source, project)
    try:
        if name == "jlcpcb":
            from . import jlcpcb

            return jlcpcb.search_lines(jlcpcb.search(category, filters, keywords, in_stock, sort, limit, offset))
        from . import digikey

        return digikey.search_lines(digikey.search(category, filters, keywords, in_stock, sort, limit, offset,
                                                   cred))
    except Choose as c:
        return c.lines


def parameter_lines(source: str, category: str, project=None) -> list[str]:
    """The parameters of a category at a source of the parametric search. A
    category text that fits more categories gives the list to select from."""
    name, cred = _source(source, project)
    try:
        if name == "jlcpcb":
            from . import jlcpcb

            return jlcpcb.parameter_lines(category)
        from . import digikey

        return digikey.parameter_lines(category, cred)
    except Choose as c:
        return c.lines


def link_lines(mpn: str, project=None) -> list[str]:
    """The search links of part_links, and the direct links of DigiKey (CAD
    models, datasheets) when DigiKey is enabled and has a key."""
    from .links import format_links

    if not mpn.strip():
        raise LensError("the MPN is empty")
    lines = format_links(mpn)
    keys, _ = keys_of(project)
    if "digikey" in keys:
        from . import digikey

        try:
            lines += digikey.media_lines(mpn, keys["digikey"])
        except LensError as e:
            lines.append(f"Direct links of DigiKey: not checked: {e}")
    return lines


def check_locale(site: str | None, currency: str | None) -> tuple[str | None, str | None]:
    """The country and the currency in upper case, or a LensError. None or "":
    no change."""
    import re

    if site and not re.fullmatch(r"[A-Za-z]{2}", site.strip()):
        raise LensError("site: a country code of 2 letters, for example CZ or US")
    if currency and not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise LensError("currency: a currency code of 3 letters, for example CZK, EUR or USD")
    return (site.strip().upper() if site else None), (currency.strip().upper() if currency else None)


def set_locale(site: str = "", currency: str = "") -> list[str]:
    """Save the country and the currency of the prices and the stock in the
    global configuration (DigiKey uses them). Gives the lines of the result."""
    from .config import load_config
    from .setup import save_config

    site, currency = check_locale(site, currency)
    if not site and not currency:
        raise LensError("give the site (a country code, for example CZ) or the currency (for example CZK), or both")
    cfg = load_config(strict=True)  # a file that cannot be read is not written over
    for key, value in (("site", site), ("currency", currency)):
        if value:
            cfg[key] = value
    save_config(cfg)
    from . import digikey

    s, c = digikey.locale()
    return [f"global: site {s}, currency {c}. DigiKey gives its prices and stock for this country and currency. "
            "Mouser uses the country of the Mouser account. JLCPCB gives USD."]
