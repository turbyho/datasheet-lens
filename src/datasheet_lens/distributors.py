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


def _mouser_offer(part: dict) -> Offer:
    number = str(part.get("MouserPartNumber", "")).strip()
    return Offer(
        source="Mouser", mpn=str(part.get("ManufacturerPartNumber", "")).strip(),
        manufacturer=str(part.get("Manufacturer", "")).strip(),
        description=str(part.get("Description", "")).strip(),
        lifecycle=str(part.get("LifecycleStatus") or "").strip(),
        discontinued=str(part.get("IsDiscontinued", "")).lower() == "true",
        replacement=str(part.get("SuggestedReplacement") or "").strip(),
        stock=_int(part.get("AvailabilityInStock")), availability=str(part.get("Availability") or "").strip(),
        lead_time=_lead_time(str(part.get("LeadTime") or "")), minimum=_int(part.get("Min")),
        multiple=_int(part.get("Mult")),
        prices=[(_int(p.get("Quantity")) or 0, f"{p.get('Price', '')} {p.get('Currency', '')}".strip())
                for p in part.get("PriceBreaks") or []],
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
    parts.append(f"stock: {o.stock if o.stock is not None else o.availability or 'unknown'}"
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


def availability(project, store=None) -> list[str]:
    """The lifecycle state and the stock of each project part, as lines, with a
    WARNING for a part that is not orderable, that the distributor does not have,
    or that has no stock. An enabled distributor with no key gives a note for
    the user. store: its manufacturer registry selects the offers of the
    manufacturer of the part."""
    keys, notes = keys_of(project)
    jlc = jlcpcb_ready()
    if not keys and not jlc:
        return notes or ["No enabled distributor: \"datasheet-lens dist\" shows the settings."]
    lines: list[str] = []
    for name, credential in keys.items():
        lines += _availability_at(name, credential, project, store)
    if jlc:
        lines += _availability_jlcpcb(project, store)
    return lines + notes


def _availability_jlcpcb(project, store=None) -> list[str]:
    """The lines of availability in the JLCPCB parts library: the JLCPCB stock
    for the assembly and the library class of each project part, from the
    manufacturer of the part when the MPN has parts of more manufacturers."""
    from . import jlcpcb

    lines = ["Source: JLCPCB parts library (stock for the JLCPCB assembly)"]
    for part in project.parts:
        try:
            offers = jlcpcb.offers(part["mpn"], exact=True)
        except (LensError, sqlite3.Error) as e:
            lines.append(f"{part['mpn']}: not checked: {e}")
            continue
        if not offers:
            lines.append(f"{part['mpn']}: WARNING: the JLCPCB parts library does not have this MPN")
            continue
        offers, own = _of_maker(offers, part.get("manufacturer", ""), store)
        o = max(offers, key=lambda x: x.stock or 0)
        state = f"{part['mpn']}: JLCPCB stock {o.stock}, library {o.library} ({o.distributor_pn})"
        if not o.stock:
            state += ". WARNING: NOT IN STOCK at JLCPCB: the assembly needs an order of the part"
        if part.get("manufacturer") and not own:
            state += (f". WARNING: the JLCPCB library has no part from {part['manufacturer']}: the data is of "
                      + ", ".join(sorted({x.manufacturer or "manufacturer unknown" for x in offers})))
        lines.append(state)
    note = jlcpcb.age_note()
    return lines + ([note] if note else [])


def _availability_at(name: str, credential, project, store=None) -> list[str]:
    """The lines of availability for one distributor."""
    label = SUPPORTED[name][0]
    lines = [f"Source: {label}"]
    for i, part in enumerate(project.parts):
        try:
            offers = [o for o in offers_of(name, part["mpn"], True, credential) if fits(o.mpn, part["mpn"], True)]
        except Unavailable as e:  # the next parts fail too: do not wait for each one
            lines.append(f"not checked: {', '.join(p['mpn'] for p in project.parts[i:])}: {e}")
            break
        except LensError as e:
            lines.append(f"{part['mpn']}: not checked: {e}")
            continue
        if not offers:
            lines.append(f"{part['mpn']}: WARNING: {label} does not have this MPN (a base part, or a wrong MPN?)")
            continue
        offers, own = _of_maker(offers, part.get("manufacturer", ""), store)
        # One MPN can have more offers (cut tape and reel): the part is orderable
        # when one offer is, and its stock is the sum.
        orderable = [o for o in offers if not not_orderable(o)]
        o = (orderable or offers)[0]
        stocks = [x.stock for x in orderable or offers if x.stock is not None]
        stock = sum(stocks) if stocks else None
        state = (f"{part['mpn']}: " + (f"{o.lifecycle}, " if o.lifecycle else "")
                 + f"stock {stock if stock is not None else 'unknown'}")
        if not orderable:
            state += f". WARNING: not orderable: {not_orderable(o)}"
        elif not stock and all(restricted(x) for x in offers):
            state += (f". WARNING: {label} does not sell this part now ({o.lifecycle}). This is not the "
                      "lifecycle state of the manufacturer: ask the manufacturer or a different distributor")
        elif not stock:
            state += (". WARNING: no stock" + (f" ({o.availability})" if o.availability else "")
                      + (f", lead time {o.lead_time}" if o.lead_time else ""))
        if part.get("manufacturer") and not own:
            state += (f". WARNING: {label} has no offer from {part['manufacturer']}: the data is of "
                      + ", ".join(sorted({x.manufacturer or "manufacturer unknown" for x in offers})))
        lines.append(state)
    return lines


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
