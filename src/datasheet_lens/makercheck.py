"""The check of the manufacturers of the parts in the store against their datasheets.

The manufacturer of a part is the manufacturer of its datasheet, not the name
of a distributor (decision of the user, 2026-11-07). Older versions gave a part
the name of the distributor (adopt), for example "UNI ROYAL Uniroyal Elec" of
JLCPCB. The check reads the manufacturer of each datasheet of a part
(identify.propose with the names of the registry) and gives:

- rename: the datasheet names the same manufacturer with a different name, or
  the part has no manufacturer. apply changes these parts, after the user
  agrees (decision of the user, 2026-11-08). The change goes to the team server
  with the other changes of the store.
- different: the datasheet names a manufacturer that the registry does not
  relate to the name of the part, or the datasheets of the part name more
  manufacturers. The user decides.
- unread: the datasheet names no manufacturer that the tool knows. The agent
  reads page 1 and asks the user.
"""

from __future__ import annotations

from collections import OrderedDict

import pypdfium2 as pdfium

from .errors import LensError
from .store import Store

# The manufacturer that each datasheet names (identify.propose), by the SHA-256 and
# the title of the document and the names of the registry: a PDF does not change,
# thus each process reads its page 1 one time (sync and project_check run often).
_FOUND: OrderedDict[tuple, tuple[str, str]] = OrderedDict()
_FOUND_SIZE = 2048

CONFIRM = ("When the user read page 1 and confirms the given name (for example the maker of a module whose "
           "datasheet names the maker of its chip more often), give it with manufacturer_confirmed=true (CLI: "
           "--confirmed).")


def found_maker(store: Store, doc, names: dict[str, tuple[str, str]] | None = None) -> tuple[str, str]:
    """The manufacturer that the datasheet doc names (identify.propose with the
    names of the registry, Store.text_names), and an error text of a PDF that
    the tool cannot read ("" for none). Kept for the process (_FOUND)."""
    from .identify import propose

    names = store.text_names() if names is None else names
    key = (doc.sha256, doc.title or "", tuple(sorted(names.items())))
    if key in _FOUND:
        _FOUND.move_to_end(key)
        return _FOUND[key]
    try:
        value = (propose(doc, names).manufacturer, "")
    except (LensError, OSError, pdfium.PdfiumError) as e:
        value = ("", str(e))
    _FOUND[key] = value
    if len(_FOUND) > _FOUND_SIZE:
        _FOUND.popitem(last=False)
    return value


def datasheet_maker(store: Store, doc, given: str, mpn: str, confirmed: bool = False) -> tuple[str, str]:
    """The manufacturer of a part from its datasheet doc (decision of the user,
    2026-11-08): the manufacturer of a part is the manufacturer of its
    datasheet. given: the name of the step (the user, the agent or the store).
    Gives (the name, "") or ("", the question for the user: the caller adds
    and links no part):
    - confirmed: the user read page 1 and confirms given (a module datasheet
      can name the maker of its chip more often than its own maker): given
    - the datasheet names a manufacturer that the tool knows (found_maker):
      its name, when given is empty or a name of the same manufacturer
    - the datasheet names no manufacturer that the tool knows: given; with no
      given, the question for the name
    - the registry does not relate the two names: the question for the relation
    A LensError when the registry records the two names as two manufacturers."""
    given = given.strip()
    if confirmed and given:
        return given, ""
    found = found_maker(store, doc)[0]
    if found and (not given or store.same_maker(found, given)):
        return found, ""
    if not found and given:
        return given, ""
    if not found:
        return "", (f"ASK THE USER: the manufacturer of {mpn}. Its datasheet (document {doc.sha256[:12]}) names no "
                    "manufacturer that the tool knows. Read page 1 of the datasheet (get_page_image), ask the "
                    "user, then do the step again with the manufacturer (the name in the datasheet). The "
                    "tool did not add or link the part.")
    if store.distinct_makers(found, given):
        raise LensError(f"the datasheet (document {doc.sha256[:12]}) of {mpn} names {found}, and the manufacturer "
                        f"registry records {given} as a different manufacturer: the document is not the datasheet "
                        f"of this part, or the manufacturer of the part is {found}. {CONFIRM} The tool did not add "
                        "or link the part.")
    return "", (f"ASK THE USER: the manufacturer of {mpn}. Its datasheet (document {doc.sha256[:12]}) names "
                f"{found}, and the step gives {given}: the manufacturer registry does not know if they are one "
                "manufacturer. Ask the user, then record it with manufacturer_relation (CLI: datasheet-lens maker "
                f"RELATION NAME OTHER): alias \"{given}\" \"{found}\" (one manufacturer: the part gets the name "
                "of the datasheet) or distinct (two manufacturers: the document is not the datasheet of this "
                f"part). Then do the step again. {CONFIRM} The tool did not add or link the part.")


def part_maker(store: Store, doc, mpn: str, manufacturer: str, role: str,
               confirmed: bool = False) -> tuple[str, str]:
    """The manufacturer for a link of the document doc to the part mpn (as
    datasheet_maker): for a datasheet, the given name or the name of the part
    in the store must agree with the datasheet. An other role: the given name."""
    if role != "datasheet":
        return manufacturer.strip(), ""
    return datasheet_maker(store, doc, manufacturer.strip() or store.known_maker(mpn, ""), mpn,
                           confirmed and bool(manufacturer.strip()))


def _read(store: Store, part_id: int, names: dict[str, tuple[str, str]]) -> tuple:
    """The datasheets of a part and the manufacturers that they name. Gives
    (the SHA-256 of the datasheets, {name: SHA-256} with one name for each
    manufacturer of the registry, the first error of a datasheet or "")."""
    shas = list(dict.fromkeys(r["sha256"] for r in store.documents_of_part(part_id) if r["role"] == "datasheet"))
    found = {sha: found_maker(store, store.document(sha), names) for sha in shas}
    groups: dict[str, str] = {}
    for sha in shas:
        name = found[sha][0]
        if name and not any(store.same_maker(name, other) for other in groups):
            groups[name] = sha
    return shas, groups, next((found[s][1] for s in shas if found[s][1]), "")


def datasheet_name(store: Store, part_id: int) -> str:
    """The manufacturer that the datasheets of the part name, when they name
    one manufacturer that the tool knows, else "" (see check)."""
    _, groups, _ = _read(store, part_id, store.text_names())
    return next(iter(groups)) if len(groups) == 1 else ""


def check(store: Store) -> dict:
    """The parts with a datasheet, by the result of the check (see the module
    docstring). Gives {"rename": [...], "different": [...], "unread": [...],
    "ok": the number of the parts with the name of the datasheet}. An item has
    id, mpn, manufacturer (of the part), document (the SHA-256 of a datasheet),
    and for rename "new" (the name of the datasheet), for different "names"
    (the names of the datasheets), for unread "reason" (empty, or an error)."""
    names = store.text_names()
    out: dict = {"rename": [], "different": [], "unread": [], "ok": 0}
    for part in store.db.execute("SELECT id, mpn, manufacturer FROM parts ORDER BY mpn COLLATE NOCASE, id").fetchall():
        shas, groups, reason = _read(store, part["id"], names)
        if not shas:
            continue
        item = {"id": part["id"], "mpn": part["mpn"], "manufacturer": part["manufacturer"], "document": shas[0]}
        if not groups:
            out["unread"].append({**item, "reason": reason})
            continue
        if len(groups) > 1:
            out["different"].append({**item, "names": list(groups), "document": next(iter(groups.values()))})
            continue
        [(name, sha)] = groups.items()
        item["document"] = sha
        if part["manufacturer"] == name:
            out["ok"] += 1
        elif not part["manufacturer"] or store.same_maker(part["manufacturer"], name):
            out["rename"].append({**item, "new": name})
        else:
            out["different"].append({**item, "names": [name]})
    return out


def apply(store: Store, mpns: list[str] | None = None) -> tuple[list[str], dict]:
    """Do the renames of the check: all, or only the parts with these MPNs
    (case is ignored). A part with no manufacturer does not get the name when
    the store has its MPN of this manufacturer already (Store.set_base tells
    the same): the user decides. Gives the lines of the changes and the result
    of the check before the changes."""
    result = check(store)
    wanted = {m.strip().lower() for m in mpns or [] if m.strip()}
    known = {item["mpn"].lower() for item in result["rename"]}
    changes = [f"not changed: {m}: the check proposes no new name for it" for m in sorted(wanted - known)]
    for item in result["rename"]:
        if wanted and item["mpn"].lower() not in wanted:
            continue
        twin = [r for r in store.match_parts(item["mpn"]) if r["id"] != item["id"] and r["manufacturer"]
                and store.same_maker(r["manufacturer"], item["new"])]
        if twin:
            changes.append(f"not changed: {item['mpn']}: the store has {twin[0]['mpn']} from {twin[0]['manufacturer']} "
                         "already, a second part with this MPN: ask the user which part is correct")
            continue
        store.db.execute("UPDATE parts SET manufacturer = ? WHERE id = ? AND manufacturer = ?",
                         (item["new"], item["id"], item["manufacturer"]))
        changes.append(f"changed: {item['mpn']}: {item['manufacturer'] or 'no manufacturer'} -> {item['new']}")
    store.db.commit()
    return changes, result


def lines(result: dict) -> list[str]:
    """The text of a check for the user and the agent."""
    out = [f"{result['ok']} parts have the manufacturer name of their datasheet"]
    for item in result["rename"]:
        out.append(f"rename: {item['mpn']}: {item['manufacturer'] or 'no manufacturer'} -> {item['new']} (the name "
                   f"of the datasheet {item['document'][:12]})")
    for item in result["different"]:
        out.append(f"different: {item['mpn']} ({item['manufacturer'] or 'no manufacturer'}): the datasheet "
                   f"{item['document'][:12]} gives {' and '.join(item['names'])}")
    for item in result["unread"]:
        out.append(f"unread: {item['mpn']} ({item['manufacturer'] or 'no manufacturer'}): the datasheet "
                   f"{item['document'][:12]} names no manufacturer that the tool knows"
                   + (f" ({item['reason']})" if item["reason"] else ""))
    if result["rename"]:
        out.append("Show the renames to the user. When the user agrees, apply them: all, or the MPNs that the user "
                   "selects (MCP: manufacturer_check(apply=True, mpns=[...]); CLI: datasheet-lens maker check -a "
                   "[MPN ...]).")
    if result["different"] or result["unread"]:
        out.append("For each different or unread part: read page 1 of the datasheet (get_page_image) and ask the "
                   "user for the manufacturer. When the name of the datasheet and the name of the part are one "
                   "manufacturer, record it with manufacturer_relation alias: the next check then proposes the "
                   "rename. A datasheet of a different manufacturer is a wrong datasheet or a second source: the "
                   "user decides.")
    return out
