"""DigiKey Product Information API v4: the parametric search.

Official API only. The user has a personal credential: the Client ID and the
Client Secret of a production app (see doc/distributors.md). The token comes
from the OAuth2 client credentials flow (2-legged): no browser, no callback.
The credential and the token never go into a message, a log or the database.

Facts of the API (a probe with a production key, 2026-11-06, see
plans/parametric-search.md):
- POST /search/keyword with a CategoryFilter gives FilterOptions.ParametricFilters:
  all parameters of the category with all their values (ID and text).
- A parameter filter is a list of value IDs, with no ranges. The values of one
  parameter combine as OR, the parameters as AND. A filter of 3000 value IDs
  works.
- The query parameter includes=Products,ProductsCount leaves out the filter
  values, which are about 2 MB for each category.

The terms of DigiKey forbid an own database of DigiKey data (§5.1(e)), and ask
to name DigiKey as the source (§3.1.4). Thus the data stays in the memory of the
process for one hour, and each result names DigiKey.
"""

from __future__ import annotations

import collections
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .errors import Choose, LensError, Unavailable
from .parametric import Filter, Parameter, matching_values, parse_filter, resolve

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
BASE_URL = "https://api.digikey.com/products/v4"
TIMEOUT = 60.0
CACHE_SECONDS = 3600.0
MAX_LIMIT = 50  # the API gives 50 products for each request
# Short names for DigiKey parameter names. A filter can also use the full name, or
# a part of the name that only one parameter has.
ALIASES = {
    "vdss": "Drain to Source Voltage (Vdss)", "vds": "Drain to Source Voltage (Vdss)",
    "rds_on": "Rds On (Max) @ Id, Vgs", "rdson": "Rds On (Max) @ Id, Vgs",
    "vgs_th": "Vgs(th) (Max) @ Id", "vgsth": "Vgs(th) (Max) @ Id",
    "id": "Current - Continuous Drain (Id) @ 25°C", "qg": "Gate Charge (Qg) (Max) @ Vgs",
    "drive": "Drive Voltage (Max Rds On, Min Rds On)", "channel": "FET Type",
    "package": "Supplier Device Package", "case": "Package / Case", "mounting": "Mounting Type",
}
SORTS = {"stock": ("QuantityAvailable", "Descending"), "price": ("Price", "Ascending")}


class NotFound(LensError):
    """The API has no such product (HTTP 404)."""


class Unauthorized(LensError):
    """The API does not accept the token or the key (HTTP 401)."""

_token: dict = {}  # client ID, token, expiry time
_categories: dict = {}  # time, flat list
_parameters: dict[str, tuple[float, list[Parameter], int]] = {}  # category ID -> time, parameters, products


def locale() -> tuple[str, str]:
    """(site, currency) of the global configuration: datasheet-lens dist --site CZ --currency CZK."""
    from .config import load_config

    cfg = load_config()
    return str(cfg.get("site") or "US").upper(), str(cfg.get("currency") or "USD").upper()


def _credential(credential=None) -> tuple[str, str]:
    from .distributors import no_key_note
    from .keys import get_credential

    value = credential or get_credential("digikey")
    if not value:
        raise LensError(no_key_note("digikey"))
    client_id, secret = value
    return client_id, secret


def _error(e: urllib.error.HTTPError) -> LensError:
    """The error of an HTTP answer, with the message of DigiKey (no key: the
    request has none in its URL)."""
    try:
        body = json.loads(e.read().decode("utf-8", "replace") or "null")
    except ValueError:
        body = None
    finally:
        e.close()
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("ErrorMessage") or body.get("title") or body.get("message")
                     or "").strip()
    text = f"DigiKey API: HTTP {e.code} {e.reason}" + (f": {detail}" if detail else "")
    if e.code == 401:
        return Unauthorized(text + ". The key is not valid: tell the user to check the Client ID and the Client "
                                "Secret of the production app at https://developer.digikey.com, then save them "
                                "again with \"datasheet-lens key digikey\"")
    if e.code == 404:
        return NotFound(text)
    if e.code == 429 or e.code >= 500:
        return Unavailable(text)
    return LensError(text)


# The server ends some large answers early: the answer with the filter values of a
# category (2.7 MB) stopped in 1 of 3 to 1 of 5 requests (2026-11-06), with gzip and
# with no gzip, with "Connection: close" too. Each request of this module only
# reads data, thus a second request is safe.
ATTEMPTS = 3
_ENDED = (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionResetError)


def _open(request: urllib.request.Request, timeout: float = TIMEOUT):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as e:
            raise _error(e) from None
        except (*_ENDED, urllib.error.URLError) as e:
            # urlopen gives a connection that ends before the headers as a URLError.
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            if not isinstance(reason, _ENDED):
                raise Unavailable(f"DigiKey API: no connection ({reason})") from None
            if attempt == ATTEMPTS:
                raise Unavailable(f"DigiKey API: the server ended the answer early {ATTEMPTS} times "
                                  f"({reason.__class__.__name__})") from None
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            raise Unavailable(f"DigiKey API: no connection ({e})") from None
    try:
        return json.loads(raw or b"null")
    except ValueError:
        raise LensError("DigiKey API: the answer is not JSON") from None


def _get_token(client_id: str, secret: str) -> str:
    now = time.monotonic()
    if _token.get("id") == client_id and _token.get("expires", 0) > now + 30:
        return _token["token"]
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret,
                                   "grant_type": "client_credentials"}).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
    data = _open(request)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise LensError("DigiKey API: the answer has no token")
    _token.update(id=client_id, token=token, expires=now + float(data.get("expires_in") or 600))
    return token


def call(method: str, path: str, body=None, query=None, credential=None, timeout: float = TIMEOUT):
    """One request to the API, with a token. The answer as JSON. When the API
    does not accept the token (it can end before its expiry time), the request
    is sent again one time with a new token."""
    client_id, secret = _credential(credential)
    site, currency = locale()
    data = json.dumps(body).encode() if body is not None else None
    url = BASE_URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_get_token(client_id, secret)}", "X-DIGIKEY-Client-Id": client_id,
                   "X-DIGIKEY-Locale-Site": site, "X-DIGIKEY-Locale-Language": "en",
                   "X-DIGIKEY-Locale-Currency": currency, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            return _open(urllib.request.Request(url, data=data, method=method, headers=headers), timeout)
        except Unauthorized:
            _token.clear()
            if attempt == 2:
                raise


def categories(credential=None) -> list[dict]:
    """All categories as a flat list: id, name, parent, products, leaf, path."""
    now = time.monotonic()
    if _categories.get("time", -CACHE_SECONDS) > now - CACHE_SECONDS:
        return _categories["list"]
    data = call("GET", "/search/categories", credential=credential)
    out: list[dict] = []

    def walk(items, path):
        for c in items or []:
            name = str(c.get("Name") or "").strip()
            children = c.get("Children") or []
            out.append({"id": str(c.get("CategoryId")), "name": name, "parent": str(c.get("ParentId") or ""),
                        "products": int(c.get("ProductCount") or 0), "leaf": not children,
                        "path": " > ".join([*path, name])})
            walk(children, [*path, name])

    walk((data or {}).get("Categories") if isinstance(data, dict) else None, [])
    _categories.update(time=now, list=out)
    return out


def find_category(text: str, credential=None) -> dict:
    """The category for an ID or a name. A name that fits more categories gives a
    LensError with the categories that fit: the user selects one."""
    key = text.strip().lower()
    if not key:
        raise LensError("give a category: its name or its DigiKey ID, for example \"Single FETs, MOSFETs\" or 278")
    cats = categories(credential)
    for c in cats:
        if c["id"] == key:
            return c
    # DigiKey has more categories with one name ("Accessories" in many families).
    same = [c for c in cats if c["name"].lower() == key]
    if len(same) == 1:
        return same[0]
    if same:
        return _choose(text, same)
    found = [c for c in cats if key in c["name"].lower()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise LensError(f"no DigiKey category {text!r}. Give a part of its name, for example \"MOSFET\" or "
                        "\"LDO\"")
    return _choose(text, found)


def _choose(text: str, found: list[dict], header: str = ""):
    """A Choose with the categories that fit: the name in quotes (to copy), the
    ID, the path and the number of parts, and an example command."""
    names = collections.Counter(c["name"].lower() for c in _categories.get("list") or found)
    found = sorted(found, key=lambda c: (not c["leaf"], -c["products"]))[:30]
    lines = [header or f"The category {text!r} fits more DigiKey categories. Select one: give its name in quotes, "
                       "or its ID:"]
    for c in found:
        unique = names[c["name"].lower()] == 1
        lines.append(f"  \"{c['name']}\" (ID {c['id']}): {c['path']}, {c['products']} parts"
                     + ("" if c["leaf"] else " [has subcategories: select one of them]")
                     + ("" if unique else " [the name is not unique: give the ID]"))
    first = next((c for c in found if c["leaf"]), found[0])
    ref = f"\"{first['name']}\"" if names[first["name"].lower()] == 1 else first["id"]
    lines.append(f"For example: datasheet-lens pick digikey {ref} -P (its parameters), then the same with filters. "
                 "MCP: part_parameters(\"digikey\", ...), then find_parts.")
    raise Choose(lines)


def parameters(category: dict, credential=None) -> tuple[list[Parameter], int]:
    """The parameters of a category with all their values, and the number of
    its products."""
    now = time.monotonic()
    cached = _parameters.get(category["id"])
    if cached and cached[0] > now - CACHE_SECONDS:
        return cached[1], cached[2]
    data = call("POST", "/search/keyword", {"Keywords": "", "Limit": 1, "FilterOptionsRequest": {
        "CategoryFilter": [{"Id": category["id"]}]}}, credential=credential)
    data = data if isinstance(data, dict) else {}
    out = []
    for p in (data.get("FilterOptions") or {}).get("ParametricFilters") or []:
        values = [(str(v.get("ValueId")), str(v.get("ValueName") or "")) for v in p.get("FilterValues") or []]
        out.append(Parameter(id=str(p.get("ParameterId")), name=str(p.get("ParameterName") or ""), values=values))
    count = int(data.get("ProductsCount") or 0)
    if len(_parameters) > 50:
        _parameters.clear()
    _parameters[category["id"]] = (now, out, count)
    return out, count


@dataclass
class FilterResult:
    filter: Filter
    status: str  # server, datasheet
    parameter: Parameter | None = None
    values: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SearchResult:
    category: dict
    filters: list[FilterResult]
    count: int
    products: list[dict]
    offset: int
    sort: str


def search(category: str, filters: list[str], keywords: str = "", in_stock: bool = True, sort: str = "stock",
           limit: int = 20, offset: int = 0, credential=None) -> SearchResult:
    """The parts of a category that agree with the filters."""
    if sort not in SORTS:
        raise LensError(f"sort: one of {', '.join(SORTS)}")
    if not 1 <= limit <= MAX_LIMIT:
        raise LensError(f"limit: 1 to {MAX_LIMIT}")
    if offset < 0:
        raise LensError("offset: 0 or more")
    parsed = [parse_filter(f) for f in filters]  # a wrong filter: an error before a request
    cat = find_category(category, credential)
    if not cat["leaf"]:
        _choose(category, [c for c in categories(credential) if c["parent"] == cat["id"]] or [cat],
                f"The category \"{cat['name']}\" has subcategories. Select one: give its name in quotes, or its ID:")
    params, _ = parameters(cat, credential)
    results, groups = [], {}
    for f in parsed:
        parameter = resolve(f.name, params, ALIASES)
        if f.condition:
            results.append(FilterResult(f, "datasheet", parameter))
            continue
        values = matching_values(f, parameter)
        results.append(FilterResult(f, "server", parameter, values))
        # Two filters on one parameter (Vdss >= 20 V, Vdss <= 40 V): the values of both.
        if parameter.id in groups:
            keep = {v for v, _ in values}
            groups[parameter.id] = [v for v in groups[parameter.id] if v in keep]
        else:
            groups[parameter.id] = [v for v, _ in values]
    if any(not ids for ids in groups.values()):
        return SearchResult(cat, results, 0, [], offset, sort)
    request = {"Keywords": keywords.strip(), "Limit": limit, "Offset": offset,
               "SortOptions": dict(zip(("Field", "SortOrder"), SORTS[sort], strict=True)),
               "FilterOptionsRequest": {"ParameterFilterRequest": {
                   "CategoryFilter": {"Id": cat["id"]},
                   "ParameterFilters": [{"ParameterId": int(pid), "FilterValues": [{"Id": v} for v in ids]}
                                        for pid, ids in groups.items()]}}}
    if in_stock:
        request["FilterOptionsRequest"]["SearchOptions"] = ["InStock"]
    data = call("POST", "/search/keyword", request, query={"includes": "Products,ProductsCount"},
                credential=credential)
    data = data if isinstance(data, dict) else {}
    return SearchResult(cat, results, int(data.get("ProductsCount") or 0), list(data.get("Products") or []),
                        offset, sort)


def _parameter(product: dict, name: str) -> str:
    for p in product.get("Parameters") or []:
        if p.get("ParameterText") == name:
            return str(p.get("ValueText") or "")
    return ""


def _chosen_variation(product: dict) -> dict:
    """The packaging that gives the DigiKey number and the prices: cut tape,
    else the first packaging."""
    variations = product.get("ProductVariations") or []
    for v in variations:
        if "cut tape" in str((v.get("PackageType") or {}).get("Name") or "").lower():
            return v
    return variations[0] if variations else {}


def _digikey_pn(product: dict) -> str:
    return str(_chosen_variation(product).get("DigiKeyProductNumber") or "")


def product_line(product: dict, names: list[str], currency: str) -> str:
    """One line for a product: MPN, manufacturer, DigiKey number, state, stock,
    price, the parameters of the filters, datasheet and product URL."""
    parts = [f"{product.get('ManufacturerProductNumber', '')} ({(product.get('Manufacturer') or {}).get('Name', '')})"]
    pn = _digikey_pn(product)
    if pn:
        parts.append(f"DigiKey {pn}")
    status = str((product.get("ProductStatus") or {}).get("Status") or "")
    if status:
        from .distributors import not_orderable, restricted

        offer = offer_of(product, currency)  # the same marks as search_distributors
        warn = not_orderable(offer)
        parts.append(f"state: {status}" + (f" [NOT ORDERABLE: {warn}]" if warn else "")
                     + (" [DigiKey does not sell it now; not a lifecycle state of the manufacturer]"
                        if restricted(offer) else ""))
    count = stock(product)
    parts.append(f"stock: {count if count is not None else 'unknown'}")
    if product.get("UnitPrice"):
        parts.append(f"price 1: {product['UnitPrice']} {currency}")
    for name in names:
        value = _parameter(product, name)
        if value:
            parts.append(f"{name}: {value}")
    for key, label in (("DatasheetUrl", "datasheet"), ("ProductUrl", "product")):
        if product.get(key):
            parts.append(f"{label} {url(product[key])}")
    return "; ".join(parts)


def url(text: str) -> str:
    """A URL with its scheme: DigiKey gives some datasheet URLs as "//mm.digikey.com/..."."""
    text = str(text).strip()
    return "https:" + text if text.startswith("//") else text


def search_lines(result: SearchResult) -> list[str]:
    """The result as lines: the source, each filter with its status, each part,
    and the next step."""
    _, currency = locale()
    cat = result.category
    lines = [f"Source: DigiKey. Category {cat['id']} {cat['path']}: {result.count} parts agree"
             + (f", parts {result.offset + 1} to {result.offset + len(result.products)}, sorted by {result.sort}"
                if result.products else "") + "."]
    names: list[str] = []
    for r in result.filters:
        name = r.parameter.name if r.parameter else r.filter.name
        if r.status == "datasheet":
            lines.append(f"filter \"{r.filter.text}\": NOT APPLIED. DigiKey keeps one value of \"{name}\" at its own "
                         f"conditions, not at {r.filter.condition}. Read the value in the datasheet of each part.")
        elif not r.values:
            examples = " | ".join(t for _, t in _spread(r.parameter.values, 8)) if r.parameter else ""
            lines.append(f"filter \"{r.filter.text}\": no value of \"{name}\" agrees. Examples of the values "
                         f"(separated by |): {examples}")
        else:
            lines.append(f"filter \"{r.filter.text}\": applied by DigiKey to \"{name}\" ({len(r.values)} "
                         f"value{'' if len(r.values) == 1 else 's'})")
        if name not in names and r.parameter:
            names.append(name)
    if "Supplier Device Package" not in names:
        names.append("Supplier Device Package")
    lines += [product_line(p, names, currency) for p in result.products]
    if result.products:
        lines.append("The values come from DigiKey. Read each value that the design needs in the datasheet of the "
                     "part (add_document, then get_page_image), and save it with save_fact.")
    return lines


def _spread(values: list, n: int) -> list:
    """Up to n items, spread over the list."""
    if len(values) <= n:
        return list(values)
    step = (len(values) - 1) / (n - 1)
    return [values[round(i * step)] for i in range(n)]


def parameter_lines(category: str, credential=None) -> list[str]:
    """The parameters of a category, with the number of values and examples."""
    cat = find_category(category, credential)
    if not cat["leaf"]:
        _choose(category, [c for c in categories(credential) if c["parent"] == cat["id"]] or [cat],
                f"The category \"{cat['name']}\" has subcategories. Select one: give its name in quotes, or its ID:")
    params, count = parameters(cat, credential)
    short: dict[str, str] = {}
    for alias, name in ALIASES.items():
        short.setdefault(name, alias)
    lines = [f"Source: DigiKey. Category {cat['id']} {cat['path']} ({count} parts). The parameters for the "
             "filters, with examples of their values (separated by |):"]
    for p in params:
        alias = f" (short name {short[p.name]})" if p.name in short else ""
        examples = " | ".join(t for _, t in _spread(p.values, 8))
        lines.append(f"{p.name}{alias}: {len(p.values)} values: {examples}")
    return lines


# The offers of an MPN (search_distributors, project_availability).
_offers: dict[tuple, tuple[float, list]] = {}
# The ProductStatus of DigiKey that tells that DigiKey does not sell the part. It is
# not a lifecycle state of the manufacturer (as "Restricted Availability" of Mouser).
NOT_SOLD = "discontinued at digi-key"


def _count(value) -> int | None:
    """A count of the API (900, "900", 0), or None when the API gives none."""
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def stock(product: dict) -> int | None:
    """The stock of a product. The API leaves out QuantityAvailable when it is 0
    (a live call, 2026-11-06): then the sum of the stock of each packaging."""
    total = _count(product.get("QuantityAvailable"))
    if total is not None:
        return total
    counts = [_count(v.get("QuantityAvailableforPackageType")) for v in product.get("ProductVariations") or []]
    counts = [c for c in counts if c is not None]
    return sum(counts) if counts else None


def offer_of(product: dict, currency: str):
    """An Offer of distributors.py for a DigiKey product: the cut tape packaging,
    else the first packaging, gives the DigiKey number and the prices."""
    from .distributors import Offer

    chosen = _chosen_variation(product)
    status = str((product.get("ProductStatus") or {}).get("Status") or "").strip()
    return Offer(
        source="DigiKey", mpn=str(product.get("ManufacturerProductNumber") or "").strip(),
        manufacturer=str((product.get("Manufacturer") or {}).get("Name") or "").strip(),
        description=str((product.get("Description") or {}).get("ProductDescription") or "").strip(),
        lifecycle=status, discontinued=status.lower() == "obsolete",
        stock=stock(product), minimum=_count(chosen.get("MinimumOrderQuantity")),
        prices=[(int(float(p.get("BreakQuantity") or 0)), f"{p.get('UnitPrice')} {currency}")
                for p in chosen.get("StandardPricing") or []],
        package=_parameter(product, "Supplier Device Package"),
        packaging=str((chosen.get("PackageType") or {}).get("Name") or "").strip(),
        datasheet_url=url(product.get("DatasheetUrl") or ""), product_url=url(product.get("ProductUrl") or ""),
        distributor_pn=str(chosen.get("DigiKeyProductNumber") or "").strip())


def product(number: str, credential=None) -> dict | None:
    """The DigiKey product of a DigiKey number (productdetails), or None when
    DigiKey has none. For an MPN, use offers: productdetails gives HTTP 404 also
    for an MPN of more products."""
    try:
        data = call("GET", f"/search/{urllib.parse.quote(number.strip(), safe='')}/productdetails",
                    credential=credential)
    except NotFound:
        return None
    found = data.get("Product") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else None


def offers(mpn: str, exact: bool = True, credential=None, timeout: float = TIMEOUT) -> list:
    """The DigiKey offers of an MPN: exact, or each part whose MPN starts with it
    (up to 50). One offer for each product: an MPN of more manufacturers (a
    second source, a copy) gives one offer for each manufacturer. An MPN that
    DigiKey does not have gives no offer.

    The keyword search gives the products, also for an exact MPN: productdetails
    gives HTTP 404 for an MPN of more products ("Duplicate Products found", a
    live call, 2026-11-07), thus it is only for a DigiKey number."""
    from .distributors import MIN_PREFIX

    mpn = mpn.strip()
    if not mpn:
        raise LensError("the MPN is empty")
    if not exact and len(mpn) < MIN_PREFIX:
        raise LensError(f"give {MIN_PREFIX} or more characters of the MPN")
    now = time.monotonic()
    site, currency = locale()
    key = (mpn.upper(), exact, site, currency)  # other settings: other prices and stock
    cached = _offers.get(key)
    if cached and cached[0] > now - CACHE_SECONDS:
        return list(cached[1])
    data = call("POST", "/search/keyword", {"Keywords": mpn, "Limit": MAX_LIMIT},
                query={"includes": "Products,ExactMatches,ProductsCount"}, credential=credential, timeout=timeout)
    data = data if isinstance(data, dict) else {}
    products, seen = [], set()
    for p in (data.get("ExactMatches") or []) + (data.get("Products") or []):
        number = str(p.get("ManufacturerProductNumber") or "").strip().upper()
        if not (number == mpn.upper() if exact else number.startswith(mpn.upper())):
            continue
        # One entry for each product: the two lists can give the same product. Its
        # page URL ends with the product ID of DigiKey. Two products can have the
        # same MPN and manufacturer (a product of a marketplace seller), each with
        # its own DigiKey numbers and stock.
        maker = p.get("Manufacturer") or {}
        identity = str(p.get("ProductUrl") or "").strip() or (number, str(maker.get("Id") or maker.get("Name") or ""))
        if identity not in seen:
            seen.add(identity)
            products.append(p)
    result = [offer_of(p, currency) for p in products]
    if len(_offers) > 500:
        _offers.clear()
    _offers[key] = (now, result)
    return list(result)


# The media types of /media that part_links gives: the user opens the link.
MEDIA = {"EDA Models": "symbol, footprint, 3D model", "Datasheets": "datasheet"}
# part_links gives its static links also when DigiKey does not answer: do not wait long.
MEDIA_TIMEOUT = 15.0


def media_lines(mpn: str, credential=None) -> list[str]:
    """The direct links of DigiKey for an MPN: the CAD models (SnapMagic, Ultra
    Librarian) and the datasheets. datasheet-lens does not download them. An
    MPN of more manufacturers gives the links of each one, with its name: the
    request for the media uses the DigiKey number of each product (with the MPN,
    DigiKey gives HTTP 404 for an MPN of more products)."""
    # One request for each manufacturer: the products of one manufacturer (a
    # product of a marketplace seller) have the same media. A product with a
    # DigiKey number first.
    chosen: dict[str, object] = {}
    for offer in sorted(offers(mpn, True, credential, timeout=MEDIA_TIMEOUT), key=lambda o: not o.distributor_pn):
        chosen.setdefault(offer.manufacturer, offer)
    lines = []
    for offer in chosen.values():
        # A product with no packaging has no DigiKey number: the MPN names it when
        # it is the only product.
        number = offer.distributor_pn or (mpn.strip() if len(chosen) == 1 else "")
        if not number:
            continue
        head = "Direct links (Source: DigiKey" + (f", {offer.manufacturer or 'manufacturer unknown'})"
                                                  if len(chosen) > 1 else ")")
        try:
            data = call("GET", f"/search/{urllib.parse.quote(number, safe='')}/media", credential=credential,
                        timeout=MEDIA_TIMEOUT)
        except NotFound:
            continue
        except Unavailable as e:  # the next requests fail too: do not wait for each one
            lines.append(f"{head}: not checked: {e}")
            break
        except LensError as e:
            lines.append(f"{head}: not checked: {e}")
            continue
        links = []
        for item in (data or {}).get("MediaLinks") or [] if isinstance(data, dict) else []:
            kind = MEDIA.get(str(item.get("MediaType") or ""))
            if kind and item.get("Url"):
                links.append(f"  {kind}: {str(item.get('Title') or '').strip()}: {url(item['Url'])}")
        if links:
            lines += [f"{head}:"] + links
    return lines
