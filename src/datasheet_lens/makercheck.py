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

import pypdfium2 as pdfium

from .errors import LensError
from .store import Store


def _read(store: Store, part_id: int, names: dict[str, str], cache: dict[str, tuple[str, str]]) -> tuple:
    """The datasheets of a part and the manufacturers that they name. Gives
    (the SHA-256 of the datasheets, {name: SHA-256} with one name for each
    manufacturer of the registry, the first error of a datasheet or "").
    cache: SHA-256 -> (the manufacturer of the datasheet, an error)."""
    from .identify import propose

    shas = list(dict.fromkeys(r["sha256"] for r in store.documents_of_part(part_id) if r["role"] == "datasheet"))
    for sha in shas:
        if sha not in cache:
            try:
                cache[sha] = (propose(store.document(sha), names).manufacturer, "")
            except (LensError, OSError, pdfium.PdfiumError) as e:
                cache[sha] = ("", str(e))
    groups: dict[str, str] = {}
    for sha in shas:
        name = cache[sha][0]
        if name and not any(store.same_maker(name, other) for other in groups):
            groups[name] = sha
    return shas, groups, next((cache[s][1] for s in shas if cache[s][1]), "")


def datasheet_name(store: Store, part_id: int) -> str:
    """The manufacturer that the datasheets of the part name, when they name
    one manufacturer that the tool knows, else "" (see check)."""
    _, groups, _ = _read(store, part_id, store.text_names(), {})
    return next(iter(groups)) if len(groups) == 1 else ""


def check(store: Store) -> dict:
    """The parts with a datasheet, by the result of the check (see the module
    docstring). Gives {"rename": [...], "different": [...], "unread": [...],
    "ok": the number of the parts with the name of the datasheet}. An item has
    id, mpn, manufacturer (of the part), document (the SHA-256 of a datasheet),
    and for rename "new" (the name of the datasheet), for different "names"
    (the names of the datasheets), for unread "reason" (empty, or an error)."""
    names = store.text_names()
    cache: dict[str, tuple[str, str]] = {}
    out: dict = {"rename": [], "different": [], "unread": [], "ok": 0}
    for part in store.db.execute("SELECT id, mpn, manufacturer FROM parts ORDER BY mpn COLLATE NOCASE, id").fetchall():
        shas, groups, reason = _read(store, part["id"], names, cache)
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
