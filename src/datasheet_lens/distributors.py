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

Each distributor has its own manufacturer names. A distributor name does not
change the manufacturer registry (makers.py): only the user changes it.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote

from . import makers
from .errors import LensError
from .keys import KEYS, get_key

MOUSER_ENV = KEYS["mouser"]
# The distributors that datasheet-lens can ask: name -> (label, where the user gets a key).
SUPPORTED = {"mouser": ("Mouser", "https://www.mouser.com/api-hub/ (the key of the Search API, not of the Cart "
                                   "API; free, with a Mouser account)")}
PLANNED = ("digikey", "farnell", "tme")  # not supported yet
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


def check_names(names: list[str]) -> list[str]:
    """The distributor names in lower case, or a LensError for a name that
    datasheet-lens does not support."""
    out = []
    for name in (n.strip().lower() for n in names):
        if name in PLANNED:
            raise LensError(f"{name} is not supported yet (supported: {', '.join(SUPPORTED)})")
        if name not in SUPPORTED:
            raise LensError(f"unknown distributor {name!r} (supported: {', '.join(SUPPORTED)})")
        if name not in out:
            out.append(name)
    return out


def enabled(project=None) -> tuple[list[str], str, list[str]]:
    """The distributors to ask, where the list comes from (the project file,
    the global configuration or the default), and a note for each name of the
    list that is not supported (for example a list of a newer version): it is
    left out, so the others still work. A user in the USA does not want
    Farnell, for example."""
    from .setup import load_config

    if project is not None and getattr(project, "distributors", None) is not None:
        names, source = project.distributors, f"the project file {project.path}"
    else:
        names = load_config().get("distributors")
        if not isinstance(names, list):
            return list(DEFAULT), "the default", []
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
    return (f"{label}" + (f" is enabled ({source}) but" if source else "") + " has no API key. Tell the user: "
            f"get a key at {where}, then run \"datasheet-lens key {name}\" in a terminal and paste the key (it "
            f"goes into the key store of the system; for CI: the environment variable {KEYS[name]}). Or "
            f"disable {label}: \"datasheet-lens dist\" shows the settings.")


def keys_of(project=None) -> tuple[dict[str, str], list[str]]:
    """The keys of the enabled distributors ({name: key}, one lookup each) and
    the notes for the enabled distributors with no key."""
    names, source, notes = enabled(project)
    keys: dict[str, str] = {}
    for name in names:
        key = get_key(name)
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


def _attribute(part: dict, *names: str) -> str:
    for a in part.get("ProductAttributes") or []:
        if a.get("AttributeName", "").lower() in names:
            return str(a.get("AttributeValue", "")).strip()
    return ""


def _mouser_offer(part: dict) -> Offer:
    return Offer(
        source="Mouser", mpn=str(part.get("ManufacturerPartNumber", "")).strip(),
        manufacturer=str(part.get("Manufacturer", "")).strip(),
        description=str(part.get("Description", "")).strip(),
        lifecycle=str(part.get("LifecycleStatus") or "").strip(),
        discontinued=str(part.get("IsDiscontinued", "")).lower() == "true",
        replacement=str(part.get("SuggestedReplacement") or "").strip(),
        stock=_int(part.get("AvailabilityInStock")), availability=str(part.get("Availability") or "").strip(),
        lead_time=str(part.get("LeadTime") or "").strip(), minimum=_int(part.get("Min")),
        multiple=_int(part.get("Mult")),
        prices=[(_int(p.get("Quantity")) or 0, f"{p.get('Price', '')} {p.get('Currency', '')}".strip())
                for p in part.get("PriceBreaks") or []],
        package=_attribute(part, "package / case", "package"), packaging=_attribute(part, "packaging"),
        datasheet_url=str(part.get("DataSheetUrl") or "").strip(),
        product_url=str(part.get("ProductDetailUrl") or "").strip(),
        distributor_pn=str(part.get("MouserPartNumber", "")).strip())


class Unavailable(LensError):
    """The API cannot answer now (no connection, rate limit, server error): the
    next requests fail too."""


MIN_PREFIX = 3  # a shorter start of an MPN gives 50 parts of no use
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
    if not keys:
        return notes + ["Search links:"] + format_links(mpn)
    lines = []
    if "mouser" in keys:
        offers = mouser_search(mpn, exact, keys["mouser"])
        lines += ([f"Source: Mouser ({len(offers)} parts)"] + [format_offer(o) for o in offers] if offers
                  else [f"Source: Mouser. No part {'is' if exact else 'starts with'} {mpn.strip()}."])
    return lines + notes


def not_orderable(offer: Offer) -> str:
    """The reason why an offer cannot be ordered, or "": obsolete, discontinued,
    end of life."""
    state = offer.lifecycle.lower()
    if offer.discontinued or any(w in state for w in ("obsolete", "discontinued", "end of life", "eol")):
        return f"{offer.lifecycle or 'discontinued'}" + (f", replacement {offer.replacement}" if offer.replacement
                                                          else "")
    return ""


def format_offer(o: Offer) -> str:
    """One line for an offer: MPN, manufacturer, lifecycle, stock, prices,
    package and datasheet URL."""
    prices = ", ".join(f"{q}: {p}" for q, p in o.prices[:3])
    warn = not_orderable(o)
    parts = [f"{o.mpn} ({o.manufacturer})",
             f"state: {o.lifecycle or 'unknown'}" + (f" [NOT ORDERABLE: {warn}]" if warn else ""),
             f"stock: {o.stock if o.stock is not None else o.availability or 'unknown'}"]
    if o.lead_time:
        parts.append(f"lead time {o.lead_time}")
    if prices:
        parts.append(f"price {prices}")
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
    if "mouser" not in keys:
        return notes or ["No enabled distributor: \"datasheet-lens dist\" shows the settings."]
    lines = ["Source: Mouser"]
    for i, part in enumerate(project.parts):
        try:
            offers = [o for o in mouser_search(part["mpn"], exact=True, key=keys["mouser"])
                      if o.mpn.lower() == part["mpn"].lower()]
        except Unavailable as e:  # the next parts fail too: do not wait for each one
            lines.append(f"not checked: {', '.join(p['mpn'] for p in project.parts[i:])}: {e}")
            break
        except LensError as e:
            lines.append(f"{part['mpn']}: not checked: {e}")
            continue
        if not offers:
            lines.append(f"{part['mpn']}: WARNING: Mouser does not have this MPN (a base part, or a wrong MPN?)")
            continue
        offers, own = _of_maker(offers, part.get("manufacturer", ""), store)
        # One MPN can have more offers (cut tape and reel): the part is orderable
        # when one offer is, and its stock is the sum.
        orderable = [o for o in offers if not not_orderable(o)]
        o = (orderable or offers)[0]
        stocks = [x.stock for x in orderable or offers if x.stock is not None]
        stock = sum(stocks) if stocks else None
        state = f"{part['mpn']}: {o.lifecycle or 'state unknown'}, stock {stock if stock is not None else 'unknown'}"
        if not orderable:
            state += f". WARNING: not orderable: {not_orderable(o)}"
        elif not stock:
            state += (". WARNING: no stock" + (f" ({o.availability})" if o.availability else "")
                      + (f", lead time {o.lead_time}" if o.lead_time else ""))
        if part.get("manufacturer") and not own:
            state += (f". WARNING: Mouser has no offer from {part['manufacturer']}: the data is of "
                      + ", ".join(sorted({x.manufacturer for x in offers})))
        lines.append(state)
    return lines + notes


def status_lines(project=None) -> list[str]:
    """The enabled distributors, where the list comes from, and the state of
    their keys (no values), with a note for each missing key."""
    from .keys import key_source

    names, source, skipped = enabled(project)
    lines = [f"enabled distributors: {', '.join(names) or 'none'} (from {source}); supported: "
             f"{', '.join(SUPPORTED)}; not supported yet: {', '.join(PLANNED)}"] + skipped
    for name in names:
        where = key_source(name)
        lines.append(f"{name}: key {where}" if where != "not set" else no_key_note(name, source))
    return lines
