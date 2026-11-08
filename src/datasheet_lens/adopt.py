"""Take a part of a source into the store and the project in one step (adopt).

The source is DigiKey, the JLCPCB parts library or Mouser. The caller gives
either the part number of the source (a DigiKey number, a JLCPCB C number, a
Mouser number) or the MPN, never a text that the tool must interpret. The tool
gets the part from the source, adds its datasheet, adds the part, and saves the
purchase source: in the store (part_sources, shared with the team server) and
in the project file ([[part.source]]). It saves no stock and no price: the
terms of DigiKey forbid an own database of DigiKey data.

The datasheet: a datasheet of the part (or of its base part) in the store comes
first: the tool does not download a second copy. Else the tool downloads one:
a URL on the site of the manufacturer first, a copy at a distributor next. It
asks the other sources for URLs only when it needs them (the APIs have daily
limits). The result tells which URL it used, and the URLs that failed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from urllib.parse import urlparse

from .errors import LensError, Unavailable
from .store import SOURCES

# The hosts of the distributors: a datasheet URL on a different host is on the
# site of the manufacturer.
DISTRIBUTOR_HOSTS = ("digikey.", "mouser.", "lcsc.", "jlcpcb.", "octopart.")


def _part(source: str, number: str, mpn: str) -> dict:
    """The part at the source: mpn, manufacturer, package, number (of the
    source), url (product page), library (JLCPCB), datasheet (URL), offer (the
    Offer of the source, for its lifecycle state)."""
    if bool(number.strip()) == bool(mpn.strip()):
        raise LensError("give the part number of the source (number) or the MPN (mpn), not both and not none")
    if source == "jlcpcb":
        return _jlcpcb(number.strip(), mpn.strip())
    if source == "digikey":
        return _digikey(number.strip(), mpn.strip())
    if source == "mouser":
        return _mouser(number.strip(), mpn.strip())
    raise LensError(f"source: one of {', '.join(SOURCES)}")


def _jlcpcb(number: str, mpn: str) -> dict:
    from . import jlcpcb

    if number:
        try:
            offer = jlcpcb.offer_of_number(number)
        except LensError:
            raise LensError(f"{number!r} is not a JLCPCB number: it is C and digits, for example C47616. Give an "
                            "MPN as mpn") from None
        if offer is None:
            raise LensError(f"the JLCPCB parts library has no part C{jlcpcb.number_id(number)}")
        if not offer.mpn.strip():
            raise LensError(f"the JLCPCB part {offer.distributor_pn} has no MPN in the library: take the part from a "
                            "different source")
        return {"mpn": offer.mpn, "manufacturer": offer.manufacturer, "package": offer.package,
                "number": offer.distributor_pn, "url": offer.product_url, "library": offer.library,
                "datasheet": offer.datasheet_url, "offer": offer}
    return _one(jlcpcb.offers(mpn, exact=True), mpn, "the JLCPCB parts library")


def _digikey(number: str, mpn: str) -> dict:
    from . import digikey
    from .keys import get_credential

    credential = get_credential("digikey")
    if not credential:
        from .distributors import no_key_note

        raise LensError(no_key_note("digikey"))
    if not number:
        # One offer for each manufacturer of the MPN.
        return _one(digikey.offers(mpn, True, credential), mpn, "DigiKey")
    product = digikey.product(number, credential)
    # The number names one packaging of the product (cut tape, reel): it stays.
    numbers = {str(v.get("DigiKeyProductNumber") or "").lower(): str(v.get("DigiKeyProductNumber") or "")
               for v in (product or {}).get("ProductVariations") or []}
    if number.lower() not in numbers:
        if product is not None or digikey.offers(number, True, credential):
            raise LensError(f"{number!r} is not a DigiKey number of a product: give an MPN as mpn")
        raise LensError(f"DigiKey has no part {number}")
    _, currency = digikey.locale()
    offer = digikey.offer_of(product, currency)
    return {"mpn": offer.mpn, "manufacturer": offer.manufacturer, "package": offer.package,
            "number": numbers[number.lower()], "url": offer.product_url, "library": "",
            "datasheet": offer.datasheet_url, "offer": offer}


def _mouser(number: str, mpn: str) -> dict:
    from .distributors import mouser_search

    offers = mouser_search(number or mpn, exact=True)
    if number:
        found = [o for o in offers if o.distributor_pn.lower() == number.lower()]
        if not found:
            raise LensError(f"{number!r} is not a Mouser number: give an MPN as mpn")
        return _one(found, number, "Mouser")
    return _one([o for o in offers if o.mpn.lower() == mpn.lower()], mpn, "Mouser")


def _one(offers: list, text: str, where: str) -> dict:
    """The part of the one offer, or a LensError: no offer, or more offers (with
    their numbers to select from). An offer with no number of the source (a
    DigiKey product with no packaging) cannot be a purchase source."""
    if not offers:
        raise LensError(f"{where} has no part {text}")
    offers = [o for o in offers if o.distributor_pn]
    if not offers:
        raise LensError(f"{where} gives no part number for {text}: take the part from a different source")
    if len(offers) > 1:
        raise LensError(f"{where} has more parts for {text}. Give the number of one: "
                        + "; ".join(f"{o.distributor_pn} ({o.mpn}, {o.manufacturer or 'manufacturer unknown'})"
                                    for o in offers))
    o = offers[0]
    return {"mpn": o.mpn, "manufacturer": o.manufacturer, "package": o.package, "number": o.distributor_pn,
            "url": o.product_url, "library": o.library, "datasheet": o.datasheet_url, "offer": o}


def _lifecycle_lines(c: dict, source: str, project=None) -> list[str]:
    """The lifecycle state of the manufacturer for the part: the offer of the
    source, and the offer of each other enabled distributor with a key, of the
    manufacturer of the part (distributors._of_maker, as project_availability).
    A risk (obsolete, last time buy, not for new designs) gives a warning: the
    part is a risk for the design also when the source has stock (the JLCPCB
    library has no lifecycle state). With no state from any distributor: a
    line that tells it, with the reasons. Then the notes about the missing
    keys."""
    from .distributors import SUPPORTED, _worst, keys_of, lifecycle_of, maker_lifecycle
    from .store import Store

    states = []  # (label, state, risk)
    offer = c.get("offer")
    if offer is not None and source != "jlcpcb":
        risk, text, _ = lifecycle_of([offer])
        states.append((offer.source, text or "no state", risk))
    reasons = []
    keys, notes = keys_of(project)
    with Store() as store:
        for name, credential in keys.items():
            if name == source:
                continue
            found, reason = maker_lifecycle(name, credential, c["mpn"], c["manufacturer"], store)
            if found is None:
                reasons.append(reason)
            else:
                states.append((SUPPORTED[name][0], found[1] or "no state", found[0]))
    shown = "; ".join(f"{label}: {text}" for label, text, _ in states)
    risk = _worst(r for _, _, r in states)
    if risk:
        line = (f"WARNING: {risk.upper()} at the manufacturer ({shown}). Tell the user, and select a replacement "
                "with the user (find_parts with the values of the datasheet); project_availability gives the "
                "replacements in the JLCPCB library")
    elif states:
        line = f"lifecycle: {shown}"
    else:
        line = ("lifecycle: not checked" + (" (the JLCPCB library has no lifecycle state)" if source == "jlcpcb"
                                            else "") + (f": {'; '.join(reasons)}" if reasons else
                                                        ": no enabled distributor with a key"))
    return [line] + notes


def _is_distributor(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(h in host for h in DISTRIBUTOR_HOSTS)


def _other_urls(c: dict, source: str, project=None) -> tuple[list[str], list[str]]:
    """The datasheet URLs of the other sources for the MPN, only of the parts
    of the same manufacturer: an MPN can have parts of more manufacturers (a
    second source, a copy), each with its own datasheet. The manufacturer
    registry compares the names. It does not learn a name by itself: a
    distributor name that it does not know is a different manufacturer here.
    Thus the second list gives the names of the URLs that the tool did not use
    ("Mouser: Vishay Semiconductors"), for the user to decide. A part with no
    manufacturer gets no URL of a different source. An error of a source only
    leaves out its URL."""
    from .distributors import jlcpcb_ready, keys_of, offers_of
    from .store import Store

    found = []
    keys, _ = keys_of(project)
    for name, cred in keys.items():
        if name == source:
            continue
        try:
            found += offers_of(name, c["mpn"], True, cred)
        except (LensError, Unavailable):
            continue
    if source != "jlcpcb" and jlcpcb_ready():
        from . import jlcpcb

        with contextlib.suppress(LensError, sqlite3.Error):
            found += jlcpcb.offers(c["mpn"], True)
    found = [o for o in found if o.datasheet_url and o.manufacturer.strip()]
    if not found or not c["manufacturer"].strip():
        return [], []
    with Store() as store:
        same = [store.same_maker(o.manufacturer, c["manufacturer"]) for o in found]
    urls = [o.datasheet_url for o, s in zip(found, same) if s]
    others = list(dict.fromkeys(f"{o.source}: {o.manufacturer}" for o, s in zip(found, same) if not s))
    return urls, others


def datasheet_urls(c: dict, source: str, project=None, not_used: list[str] | None = None) -> Iterator[str]:
    """The datasheet URLs in their order: a URL on the site of the manufacturer
    first. The other sources are asked only when the URL of the source is not
    on the site of the manufacturer, or when it fails. not_used gets the
    sources and the names of the manufacturers whose URLs the tool did not use
    (see _other_urls)."""
    seen: set[str] = set()
    first = (c.get("datasheet") or "").strip()
    if first and not _is_distributor(first):
        seen.add(first)
        yield first
    urls, names = _other_urls(c, source, project)
    if not_used is not None:
        not_used += names
    others = [u.strip() for u in urls if u.strip()]
    ordered = [u for u in others if not _is_distributor(u)] + ([first] if first else []) \
        + [u for u in others if _is_distributor(u)]
    for url in ordered:
        if url not in seen:
            seen.add(url)
            yield url


def _known_datasheet(store, mpn: str, base: str, manufacturer: str):
    """A datasheet of the part or of its base part in the store, from the same
    manufacturer (a second source of one MPN has its own datasheet), or None."""
    for name in dict.fromkeys(n for n in (mpn, base) if n):
        for part in store.match_parts(name):
            if manufacturer and part["manufacturer"] and not store.same_maker(part["manufacturer"], manufacturer):
                continue
            # The documents of the part and of its base part.
            rows = [r for r in store.documents_of_part(part["id"]) if r["role"] == "datasheet"]
            if rows:
                return store.document(rows[0]["sha256"])
    return None


def adopt(source: str, number: str = "", mpn: str = "", base: str = "", category: str = "", project=None,
          lock=None, manufacturer: str = "") -> dict:
    """Take a part of a source: the datasheet, the part, the purchase source.
    lock: held only while the store and the project file change (the MCP server
    gives its lock); the API calls and the downloads run before it.
    manufacturer: the manufacturer of the datasheet that the user gives (see
    _maker). Gives a dict: lines (the result for the user), document (the ID of
    a new document, or None), part (the project part or None), project (the
    project as it is now, or None), ask (True: the tool must have the
    manufacturer from the user, and it added no part)."""
    from .store import Store

    source = source.strip().lower()
    if source not in SOURCES:
        raise LensError(f"source: one of {', '.join(SOURCES)}")
    c = _part(source, number, mpn)
    lines = [f"{c['mpn']} ({c['manufacturer'] or 'manufacturer unknown'}) from {source}: {c['number']}"
             + (f", library {c['library']}" if c["library"] else "")
             + (f", package {c['package']}" if c["package"] else "")]
    lines += _lifecycle_lines(c, source, project)
    lock = lock or contextlib.nullcontext()
    given = manufacturer.strip()
    with Store() as store:
        known = _known_datasheet(store, c["mpn"], base.strip(), given or c["manufacturer"])
    if known is not None:
        lines.append(f"datasheet: in the store already (document {known.sha256[:12]}, {known.filename})")
        return _finish(c, source, base, category, project, lock, lines, known, False, given)
    failed: list[str] = []
    tried: list[str] = []
    not_used: list[str] = []
    for url in datasheet_urls(c, source, project, not_used):
        tried.append(url)
        doc = _download(url, lock, failed)
        if doc is not None:
            lines.append(f"datasheet: {url} (document {doc.sha256[:12]}, {doc.pages} pages)"
                         + (f"; failed before: {'; '.join(failed)}" if failed else ""))
            lines += _not_used_line(c, not_used)
            return _finish(c, source, base, category, project, lock, lines, doc, True, given)
    lines.append("datasheet: NOT ADDED. " + ("The URLs that failed: " + "; ".join(failed) + "." if failed
                                             else "No source gives a datasheet URL."))
    if project is not None:  # the folder for the file of the user; an error only leaves it out
        with contextlib.suppress(OSError):
            project.inbox_path.mkdir(parents=True, exist_ok=True)
    lines.append(_manual_line(c, project, tried))
    lines += _not_used_line(c, not_used)
    return _finish(c, source, base, category, project, lock, lines, None, False, given)


def _finish(c: dict, source: str, base: str, category: str, project, lock, lines: list[str], doc, new: bool,
            given: str) -> dict:
    """The manufacturer of the part (_maker), then the changes (_commit). When
    the tool must ask the user for the manufacturer, it changes nothing: the
    result gives the question and the new document (its element map can build)."""
    maker, ask = _maker(c, source, doc, given, lines)
    if ask:
        return {"lines": lines, "document": doc.id if doc is not None and new else None, "part": None,
                "project": project, "ask": True}
    return _commit(c, source, base, category, project, lock, lines, doc, new, maker)


def _maker(c: dict, source: str, doc, given: str, lines: list[str]) -> tuple[str, bool]:
    """The manufacturer of the part: the manufacturer of its datasheet, never
    the name of the distributor (decision of the user, 2026-11-07). Gives (the
    name, ask) and adds a line to lines:
    - given (the answer of the user): this name, when it agrees with the
      datasheet (makercheck.datasheet_maker: the name of the datasheet for a
      name of the same manufacturer, else the question for the relation of
      the two names). When the registry does not relate the name to the name
      of the distributor, a line tells the user to record the relation: the
      offers of the distributors use it.
    - no datasheet: no manufacturer. project_sync gives the part the
      manufacturer of the datasheet when the user puts the file into the inbox.
    - the datasheet names a manufacturer that the registry knows
      (identify.propose), and the distributor gives no name or a name of the
      same manufacturer: the name of the datasheet.
    - else ask: the agent reads page 1 and asks the user."""
    from .identify import propose
    from .makercheck import datasheet_maker
    from .store import Store

    offered = c["manufacturer"].strip()
    if given:
        name = given
        if doc is not None:  # the given name must agree with the datasheet
            with Store() as store:
                name, ask = datasheet_maker(store, doc, given, c["mpn"])
            if ask:
                lines.append(ask)
                return "", True
        lines.append(f"manufacturer: {name} (given)" if name == given
                     else f"manufacturer: {name} (from the datasheet; given: {given})")
        with Store() as store:
            unrelated = offered and not store.same_maker(name, offered) and not store.distinct_makers(name, offered)
        if unrelated:
            lines.append(f"manufacturer names: {source} gives {offered}, the datasheet gives {name}, and the "
                         "manufacturer registry does not know if they are one manufacturer. Ask the user, then "
                         "record it with manufacturer_relation (CLI: datasheet-lens maker RELATION NAME OTHER): "
                         f"alias \"{name}\" \"{offered}\" (one manufacturer) or distinct (two manufacturers). The "
                         f"offers of the distributors for {c['mpn']} use this relation.")
        return name, False
    if doc is None:
        lines.append(f"manufacturer: none until the datasheet is in the store ({source} gives "
                     f"{offered or 'no name'}, but the manufacturer of a part comes from its datasheet). project_sync "
                     "gives the part the manufacturer of the datasheet.")
        return "", False
    with Store() as store:
        found = propose(doc, store.text_names()).manufacturer
        if found and (not offered or store.same_maker(found, offered)):
            lines.append(f"manufacturer: {found} (from the datasheet)")
            return found, False
    lines.append(f"ASK THE USER: the manufacturer of {c['mpn']}. The datasheet (document {doc.sha256[:12]}) gives "
                 f"{found or 'no manufacturer name that the tool knows'}, and {source} gives "
                 f"{offered or 'no name'}. The manufacturer of a part comes from its datasheet. Read page 1 of the "
                 "datasheet (get_page_image), ask the user, then call adopt_part again with manufacturer=<the name "
                 "in the datasheet> (CLI: --manufacturer). The tool added no part and no purchase source.")
    return "", True


def _manual_line(c: dict, project, urls: list[str]) -> str:
    """The line for the user when no URL gives the datasheet (a login, a
    captcha, a page of links): the URLs to open in a web browser, and the
    folder to save the file into. In a project, the inbox: the next sync gives
    the file to the part (project._inbox)."""
    found = f"from {' or '.join(urls)}" if urls else f"(part_links of {c['mpn']} gives the search links)"
    if project is not None:
        target = (f"save the PDF file into the inbox of the project: {project.inbox_path}. Then run project_sync "
                  f"(CLI: datasheet-lens sync): it gives the file to {c['mpn']}")
    else:
        target = "add the PDF file with add_document (CLI: datasheet-lens add FILE)"
    return f"Tell the user: download the datasheet of {c['mpn']} {found} in a web browser, and {target}."


def _not_used_line(c: dict, not_used: list[str]) -> list[str]:
    """The line about the datasheet URLs of other manufacturers, or none: the
    user decides if a name is a different name of the manufacturer."""
    if not not_used:
        return []
    return [f"datasheet URLs not used: the parts of other manufacturers with the MPN {c['mpn']}: "
            f"{'; '.join(not_used)}. When a name is a different name of {c['manufacturer']}, tell the user: "
            "record it with manufacturer_relation (CLI: datasheet-lens maker alias NAME OTHER). Then adopt uses "
            "its URLs."]


def _download(url: str, lock, failed: list[str]):
    """The document of a URL: the download (no lock), then the store (with the
    lock), then its index (no lock). None, with a line in failed, when the URL gives no
    file or a file that is not a readable PDF: the next URL can give one."""
    import pypdfium2 as pdfium

    from .config import store_home
    from .fetch import downloaded
    from .indexer import build_index
    from .store import Store

    try:
        with downloaded(url, store_home()) as path:
            with lock, Store() as store:
                doc = store.add_document(path, source_url=url, move=True)
            # Not under the lock: build_index makes one build at a time (indexer).
            with Store() as store:
                build_index(store, doc.id)
            return doc
    except (LensError, Unavailable, OSError, pdfium.PdfiumError) as e:
        # The errors of fetch start with the URL: the line gives it one time.
        reason = str(e)
        reason = reason[len(url):].lstrip(": ") if reason.startswith(url) else reason
        failed.append(f"{url}: {reason}")
        return None


def _commit(c: dict, source: str, base: str, category: str, project, lock, lines: list[str], doc,
            new: bool, maker: str) -> dict:
    """The changes of the store and of the project, under the locks: the lock of
    the project folder first, then lock (the MCP server), then the store (the
    order of lock.py). The project file is read again under the lock of the
    folder: a different tool or process can change it during the downloads."""
    from .lock import folder_lock
    from .project import add_part_with_id, load, store_part
    from .store import Store

    with folder_lock(project.root) if project is not None else contextlib.nullcontext(), lock, Store() as store:
        owner = base.strip() if base.strip().lower() != c["mpn"].lower() else ""
        document = doc.sha256 if doc is not None else ""
        part = None
        if project is not None:
            project = load(project.root)
            source_entry = {"source": source, "number": c["number"], "library": c["library"]}
            part, part_id = add_part_with_id(project, store, c["mpn"], maker, document, "datasheet",
                                             category, base=owner, source=source_entry)
            lines.append(f"project: {part['mpn']}" + (f" (base part {part['base']})" if part.get("base") else "")
                         + f", source {source} {c['number']}")
        else:
            part_id, _ = store_part(store, c["mpn"], maker, document, "datasheet", category, owner)
            lines.append("project: none (not in a project folder): the part is in the store only")
        store.set_part_source(part_id, source, c["number"], url=c["url"], library=c["library"])
    lines.append("Next: read each value that the design needs in the datasheet (get_page_image) and save it with "
                 "save_fact.")
    # document: a new document only (the caller builds its element map). project:
    # the project as it is now (read again under the lock).
    return {"lines": lines, "document": doc.id if doc is not None and new else None, "part": part,
            "project": project, "ask": False}
