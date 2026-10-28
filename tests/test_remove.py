import shutil

from datasheet_lens import project as prj
from datasheet_lens.cli import main
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
    assert "kept: datasheets/opa2134.pdf (an other part uses it)" in out
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


def test_pinned_file_stays(store_home, text_pdf, tmp_path, capsys):
    rtc = text_pdf("rtc.pdf", [[(50, 60, 12, "RV-3149-C3 RTC")]])
    with Store() as store:
        store.add_part("RV-3149-C3", "Micro Crystal")
        project = prj.init_project(tmp_path / "board", layout="")
        shutil.copyfile(rtc, project.folder_path / "rv.pdf")
        sha = prj.sync(project, store)["unassigned"][0]["sha256"]
        prj.add_part(project, store, "RV-3149-C3", document=sha[:12])
    assert main(["rm", "RV-3149-C3", "-p", str(project.root)]) == 0
    assert "kept: datasheets/rv.pdf (pinned" in capsys.readouterr().out
    assert (project.folder_path / "rv.pdf").is_file()


def test_short_commands(store_home, text_pdf, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    monkeypatch.chdir(tmp_path)
    assert main(["new"]) == 0
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
        for maker in ("TI", "ST"):
            store.link(store.add_part("LM358", maker), doc.id)
        project = prj.init_project(tmp_path / "board", layout="")
        prj.add_part(project, store, "LM358", "TI")
        prj.add_part(project, store, "LM358", "ST")
    assert main(["rm", "LM358", "-M", "TI", "-d", doc.sha256[:12], "-p", str(project.root)]) == 0
    parts = {p["manufacturer"]: p for p in prj.load(project.root).parts}
    assert parts["TI"]["documents"] == [] and len(parts["ST"]["documents"]) == 1
