import shutil

import pytest

from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store

OPA = [[(50, 60, 12, "OPA2134 Audio Op Amp")]]
ERR = [[(50, 60, 12, "OPA2134 Errata")]]


def setup_project(store_home, text_pdf, tmp_path):
    """OPA2134 with a datasheet and an errata, OPA134 with the same datasheet."""
    ds, errata = text_pdf("opa2134.pdf", OPA), text_pdf("errata.pdf", ERR)
    with Store() as store:
        d, e = store.add_document(ds), store.add_document(errata)
        opa2134, opa134 = store.add_part("OPA2134", "TI"), store.add_part("OPA134", "TI")
        store.link(opa2134, d.id)
        store.link(opa2134, e.id, "errata")
        store.link(opa134, d.id)
        project = prj.init_project(tmp_path / "board", layout="")
        prj.add_part(project, store, "OPA2134")
        prj.add_part(project, store, "OPA134")
        prj.sync(project, store)
    return project, d, e


def test_rm_part_keeps_a_shared_copy(store_home, text_pdf, tmp_path, capsys):
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    root = str(project.root)
    assert main(["rm", "OPA2134", "-n", "-p", root]) == 0  # a dry run changes nothing
    assert "would delete: datasheets/errata.pdf" in capsys.readouterr().out
    assert (project.folder_path / "errata.pdf").is_file()

    assert main(["rm", "OPA2134", "-p", root]) == 0
    out = capsys.readouterr().out
    assert "removed: part OPA2134" in out and "deleted: datasheets/errata.pdf" in out
    assert "kept: datasheets/opa2134.pdf (a different part uses it)" in out
    assert [p["mpn"] for p in prj.load(project.root).parts] == ["OPA134"]
    assert (project.folder_path / "opa2134.pdf").is_file() and not (project.folder_path / "errata.pdf").exists()
    with Store() as store:  # the central store does not change
        assert len(store.part_documents("OPA2134")) == 2
        report = prj.sync(prj.load(project.root), store)
    assert not report["suggested"] and not report["unassigned"]  # no file comes back as new


def test_rm_document_is_remembered(store_home, text_pdf, tmp_path, capsys):
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    assert main(["rm", "OPA2134", "-d", e.sha256[:12], "-p", str(project.root)]) == 0
    assert "removed: document" in capsys.readouterr().out
    part = prj.load(project.root).parts[0]
    assert [x["file"] for x in part["documents"]] == ["opa2134.pdf"] and part["exclude"] == [e.sha256]
    with Store() as store:  # sync does not add it again
        prj.sync(prj.load(project.root), store)
    assert [x["file"] for x in prj.load(project.root).parts[0]["documents"]] == ["opa2134.pdf"]
    assert not (project.folder_path / "errata.pdf").exists()


def test_document_state_of_the_session_review(store_home, text_pdf, tmp_path, capsys):
    """The cases of the session review (plans/session-review-plan.md, phase 2):
    the store links a new document to a part, and the project file does not
    name it. project_check tells that sync is necessary (4.8). A copy that an
    earlier sync wrote is a copy of sync after git restored the project file
    (5.3, decision D2). The removal accepts the document that sync would add
    (5.2). pin and unpin change a reference."""
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    root = str(project.root)
    cad = [{"mpn": "OPA2134", "reference": "U1"}, {"mpn": "OPA134", "reference": "U2"}]
    with Store() as store:
        doc = store.add_document(text_pdf("pkg.pdf", [[(50, 60, 12, "OPA2134 package drawing")]]))
        store.link(store.find_part("OPA2134", "TI", create=False), doc.id, "other")
        result = prj.check_parts(prj.load(root), store, cad)
        result["sync"] = prj.sync_steps(prj.dry_run(prj.load(root), store)[0])
        text = "\n".join(prj.format_check(result))
        assert "project folder: 1 new reference, 1 copy missing. Run project_sync; project_status gives the files." \
            in text and "next steps: project_sync copies" in text
        # A sync writes the copy, then git restores the older project file: the copy is
        # in the subfolder of its part (layout "": the folder), thus a copy of sync.
        old = project.path.read_text(encoding="utf-8")
        prj.sync(prj.load(root), store)
        project.path.write_text(old, encoding="utf-8")
        report = prj.sync(prj.load(root), store, apply=False)
        assert report["found"] == [("pkg.pdf", "copy")]
        assert ("new reference: pkg.pdf (a copy of sync: the file is in the subfolder of its part; project_update_part "
                "with pin_document or unpin_document changes it)") in prj.format_report(
            report, False, ("{part}{base} {sha}", " {base}"))
        assert ("new reference: pkg.pdf (a copy of sync: the file is in the subfolder of its part; datasheet-lens set "
                "MPN --pin FILE or --unpin FILE changes it)") in prj.format_report(
            report, False, ("{part}{base} {sha}", " {base}"), cli=True)
    # The project file does not name the document, but sync would add it: the removal
    # remembers it, and deletes the copy of sync.
    assert main(["rm", "OPA2134", "-d", doc.sha256[:12], "-p", root]) == 0
    assert "deleted: datasheets/pkg.pdf" in capsys.readouterr().out
    assert doc.sha256 in prj.load(root).parts[0]["exclude"] and not (project.folder_path / "pkg.pdf").exists()
    with Store() as store:
        result = prj.check_parts(prj.load(root), store, cad)
        result["sync"] = prj.sync_steps(prj.dry_run(prj.load(root), store)[0])
        assert result["sync"] == {"sync": [], "user": []}
        assert prj.format_check(result)[-1].startswith("next step: project_availability gives the lifecycle state")
        # pin and unpin, with the changes in the result. pinned is a state of the file:
        # OPA134 shares the copy opa2134.pdf, thus its reference changes too.
        part = prj.update_part(prj.load(root), store, "OPA2134", pin="opa2134.pdf")
        assert part["changes"] == ["pinned opa2134.pdf: false -> true"]
        assert [p["documents"][0].get("pinned") for p in prj.load(root).parts] == [True, True]
        part = prj.update_part(prj.load(root), store, "OPA2134", unpin=d.sha256[:12], category="op amp")
        assert part["changes"] == ["category: (none) -> op amp", "pinned opa2134.pdf: true -> false"]
        assert [p["documents"][0].get("pinned") for p in prj.load(root).parts] == [None, None]
        with pytest.raises(LensError, match="part OPA2134 has no document nope.pdf"):
            prj.update_part(prj.load(root), store, "OPA2134", pin="nope.pdf")
        with pytest.raises(LensError, match="pin and unpin give the same document opa2134.pdf"):
            prj.update_part(prj.load(root), store, "OPA2134", pin="opa2134.pdf", unpin=d.sha256[:12])
    assert main(["set", "OPA2134", "--pin", str(d.id), "-p", root]) == 0  # a document ID of the store
    assert "  pinned opa2134.pdf: false -> true" in capsys.readouterr().out


def test_rm_document_also_from_the_parts_that_sync_would_give_it(store_home, text_pdf, tmp_path, capsys):
    """rm -d with no MPN removes the document from each part that has it, and
    also from each part that sync would give it: else the next sync adds it
    again."""
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    with Store() as store:
        store.link(store.find_part("OPA134", "TI", create=False), e.id, "errata")
    assert main(["rm", "-d", e.sha256[:12], "-p", str(project.root)]) == 0
    assert "from OPA2134, OPA134" in capsys.readouterr().out
    assert [p.get("exclude") for p in prj.load(project.root).parts] == [[e.sha256], [e.sha256]]
    with Store() as store:
        report = prj.sync(prj.load(project.root), store)
    assert report["added"] == [] and not (project.folder_path / "errata.pdf").exists()


def test_copies_shared_and_pinned_files(store_home, text_pdf, tmp_path):
    """The rule of the copies with a layout of subfolders (D2): of two files with
    the content, the one in the subfolder of the part is the copy. A file of a
    different part is shared. A pinned reference of one part keeps the file at
    its place for each part. The steps that sync cannot do need the user, and
    do not stop the next step."""
    ds = text_pdf("opa.pdf", OPA)
    with Store() as store:
        doc = store.add_document(ds)
        for mpn in ("OPA2134", "OPA134"):
            store.link(store.add_part(mpn, "TI"), doc.id)
        project = prj.init_project(tmp_path / "board", layout="{manufacturer}")
        for folder in ("Other", "Texas Instruments"):  # layout_dir gives the built-in name of TI
            (project.folder_path / folder).mkdir(parents=True)
            shutil.copyfile(ds, project.folder_path / folder / "opa.pdf")
        prj.add_part(project, store, "OPA2134")
        report = prj.sync(prj.load(project.root), store, apply=False)
        assert report["found"] == []
        assert prj.load(project.root).parts[0]["documents"][0]["file"] == "Texas Instruments/opa.pdf"
        prj.add_part(project, store, "OPA134")
        assert prj.load(project.root).parts[1]["documents"][0]["file"] == "Texas Instruments/opa.pdf"
        # A reference to the file of the other part is shared.
        ti = "Texas Instruments"
        assert prj._file_state(f"{ti}/opa.pdf", ti, {}) == "copy"
        assert prj._file_state(f"{ti}/opa.pdf", ti, {f"{ti}/opa.pdf": doc.sha256}) == "shared"
        assert prj._file_state("Other/opa.pdf", ti, {}) == "pinned"
        # Other/opa.pdf is a file with the content and no reference: sync does not
        # assign it (the content is known), it is not a step of sync.
        steps = prj.sync_steps(prj.dry_run(prj.load(project.root), store)[0])
        assert steps == {"sync": [], "user": []}
        # pinned keeps the file: a pin of OPA134 stops the move of the shared file.
        project = prj.load(project.root)
        prj.update_part(project, store, "OPA134", pin="Texas Instruments/opa.pdf")
        project = prj.load(project.root)
        project.layout = "{manufacturer}/{mpn}"
        report = prj.sync(project, store, apply=False)
        assert report["moved"] == [] and "Texas Instruments/opa.pdf" in report["ok"]


def test_pinned_file_stays(store_home, text_pdf, tmp_path, capsys):
    """A file of the user outside the subfolder of the layout of its part is
    pinned: a removal keeps it. A file in that subfolder (with layout "": the
    folder itself) is a copy of sync: a removal deletes it, and the store keeps
    the document (decision D2 of plans/session-review-plan.md)."""
    rtc = text_pdf("rtc.pdf", [[(50, 60, 12, "RV-3149-C3 RTC")]])
    for name, pinned in (("mine/rv.pdf", True), ("rv.pdf", False)):
        with Store() as store:
            store.add_part("RV-3149-C3", "Micro Crystal")
            project = prj.init_project(tmp_path / name.replace("/", "-"), layout="")
            (project.folder_path / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rtc, project.folder_path / name)
            report = prj.sync(project, store)  # the second time, the store links the document to the part
            sha = (report["unassigned"] or report["suggested"])[0]["sha256"]
            part = prj.add_part(project, store, "RV-3149-C3", document=sha[:12])
        assert part["documents"][0].get("pinned", False) is pinned
        assert main(["rm", "RV-3149-C3", "-p", str(project.root)]) == 0
        out = capsys.readouterr().out
        assert (f"kept: datasheets/{name} (pinned" if pinned else f"deleted: datasheets/{name}") in out
        assert (project.folder_path / name).is_file() is pinned


def test_short_commands(store_home, text_pdf, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    monkeypatch.chdir(tmp_path)
    assert main(["project", "init"]) == 0
    assert main(["add", str(text_pdf("tlv755p.pdf", [[(50, 60, 14, "TLV755P LDO")]])), "-y", "-m",
                 "TLV75533PDBVR", "-M", "TI", "-c", "LDO"]) == 0
    assert main(["ls"]) == 0
    assert "TLV75533PDBVR (TI), base part TLV755P" in capsys.readouterr().out
    assert main(["sync"]) == 0
    assert main(["rm", "TLV75533PDBVR"]) == 0
    assert prj.load(tmp_path).parts == []


def test_review_cases(store_home, text_pdf, tmp_path, capsys):
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    root = str(project.root)
    # The printed path (with the folder) and the document ID of the store work.
    assert main(["rm", "-d", "datasheets/errata.pdf", "-n", "-p", root]) == 0
    assert main(["rm", "-d", str(e.id), "-p", root]) == 0
    capsys.readouterr()
    # The document comes back on an explicit request.
    with Store() as store:
        part = prj.add_part(prj.load(project.root), store, "OPA2134", document=e.sha256[:12], role="errata")
    assert "exclude" not in part and "errata.pdf" in [x["file"] for x in part["documents"]]

    # A file with a different content (a new revision of the user) is not deleted.
    project = prj.load(project.root)
    (project.folder_path / "errata.pdf").write_bytes(b"%PDF-1.7 a new revision of the user")
    assert main(["rm", "OPA2134", "-p", root]) == 0
    assert "kept: datasheets/errata.pdf (its content is not the document" in capsys.readouterr().out
    assert (project.folder_path / "errata.pdf").read_bytes().startswith(b"%PDF-1.7 a new")


def test_rm_document_with_the_manufacturer(store_home, text_pdf, tmp_path, capsys):
    with Store() as store:
        doc = store.add_document(text_pdf("lm358.pdf", [[(50, 60, 12, "LM358")]]))
        store.maker_relation("distinct", "TI", "ST")
        for maker in ("TI", "ST"):
            store.link(store.add_part("LM358", maker), doc.id)
        project = prj.init_project(tmp_path / "board", layout="")
        prj.add_part(project, store, "LM358", "TI")
        prj.add_part(project, store, "LM358", "ST")
    assert main(["rm", "LM358", "-M", "TI", "-d", doc.sha256[:12], "-p", str(project.root)]) == 0
    parts = {p["manufacturer"]: p for p in prj.load(project.root).parts}
    assert parts["TI"]["documents"] == [] and len(parts["ST"]["documents"]) == 1


def test_set_changes_a_part_in_place(store_home, text_pdf, tmp_path, capsys):
    """set (project_update_part) changes the MPN, the base part and the category.
    The part keeps its place in the file and its copies."""
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    root = str(project.root)
    copy = project.folder_path / "opa2134.pdf"
    mtime = copy.stat().st_mtime_ns
    assert main(["set", "OPA2134", "-m", "OPA2134PA", "-b", "OPA2134", "-c", "op amp", "-p", root]) == 0
    assert "OPA2134PA (TI), base part OPA2134" in capsys.readouterr().out
    parts = prj.load(project.root).parts
    assert [(p["mpn"], p.get("base"), p["category"]) for p in parts] == [
        ("OPA2134PA", "OPA2134", "op amp"), ("OPA134", None, "")]
    assert {x["file"] for x in parts[0]["documents"]} == {"opa2134.pdf", "errata.pdf"}
    assert copy.stat().st_mtime_ns == mtime  # not deleted and copied again
    with Store() as store:
        assert store.base_of(store.find_part("OPA2134PA"))["mpn"] == "OPA2134"
        # A second part with the same MPN is an error.
        project = prj.load(project.root)
        with pytest.raises(LensError, match="has a part OPA134 already"):
            prj.update_part(project, store, "OPA2134PA", new_mpn="OPA134")
        # An empty category removes it, also from the store.
        prj.update_part(project, store, "OPA2134PA", category="")
        prj.sync(project, store)
    assert prj.load(project.root).parts[0]["category"] == ""


def test_set_keeps_the_store_consistent(store_home, text_pdf, tmp_path):
    """A new MPN with no base part gets the documents of the old MPN. A
    different spelling of the manufacturer does not make a second store part.
    A part with base = mpn in the file can change its category."""
    project, d, e = setup_project(store_home, text_pdf, tmp_path)
    with Store() as store:
        prj.update_part(project, store, "OPA134", new_mpn="OPA134PA")
        assert {r["sha256"] for r in store.part_documents("OPA134PA")} == {d.sha256}
        with pytest.raises(LensError, match="the store has OPA134PA from TI"):
            prj.update_part(project, store, "OPA134PA", new_manufacturer="TXN")
        prj.update_part(project, store, "OPA134PA", new_manufacturer="Texas Instruments")  # a name of TI
        assert len(store.match_parts("OPA134PA")) == 1
        project.parts[1]["base"] = "OPA134PA"
        prj.update_part(project, store, "OPA134PA", category="op amp")
    assert prj.load(project.root).parts[1]["category"] == "op amp"
