import shutil

import pytest

from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store, sha256_file

OPA = [[(50, 60, 12, "1 Features"), (50, 90, 10, "Supply voltage 36 V")]]
RTC = [[(50, 60, 12, "1 Overview"), (50, 90, 10, "Real-time clock module")]]


@pytest.fixture
def setup(store_home, text_pdf, tmp_path):
    """A project, and a store with OPA2134 (one datasheet) and RV-3149-C3 (none)."""
    opa, rtc = text_pdf("opa2134.pdf", OPA), text_pdf("rtc.pdf", RTC)
    with Store() as store:
        doc = store.add_document(opa)
        store.link(store.add_part("OPA2134", "TI"), doc.id)
        store.add_part("RV-3149-C3", "Micro Crystal")
    return prj.init_project(tmp_path / "board"), opa, rtc


def test_copies_and_new_files(setup):
    project, opa, rtc = setup
    with Store() as store:
        part = prj.add_part(project, store, "opa2134")
        assert part["manufacturer"] == "TI" and part["documents"][0]["file"] == "opa2134.pdf"
        report = prj.sync(project, store)
        assert report["copied"] == ["opa2134.pdf"]
        copy = project.folder_path / "opa2134.pdf"
        assert sha256_file(copy) == sha256_file(opa)

        # The project file is read back the same.
        assert prj.load(project.root).parts == project.parts

        # A new file of the folder goes into the store; the file name proposes the part.
        shutil.copyfile(rtc, project.folder_path / "RV3149C3_datasheet.pdf")
        report = prj.sync(project, store)
        assert report["imported"] == ["RV3149C3_datasheet.pdf"]
        item = report["suggested"][0]
        assert item["parts"] == ["RV-3149-C3 (Micro Crystal)"]
        lines = prj.format_report(report, True, "add {mpn} {sha}")
        assert any("proposal: RV-3149-C3" in l and f"add RV-3149-C3 {item['sha256'][:12]}" in l for l in lines)

        # The assignment uses the file that is in the folder: no second copy.
        part = prj.add_part(project, store, "RV-3149-C3", document=item["sha256"][:12], role="datasheet")
        assert part["documents"][0]["file"] == "RV3149C3_datasheet.pdf"
        report = prj.sync(project, store)
        assert len(report["ok"]) == 2 and not report["copied"] and not report["suggested"]
        assert len(report["documents"]) == 2

        # A changed copy is reported, and restored only on request.
        copy.write_bytes(b"%PDF-1.7 changed")
        report = prj.sync(project, store, apply=False)
        assert report["changed"] == ["opa2134.pdf"]
        report = prj.sync(project, store, restore=True)
        assert report["restored"] == ["opa2134.pdf"] and sha256_file(copy) == sha256_file(opa)


def test_store_link_assigns_a_new_file(setup):
    """A file that the store links to a part of the project needs no proposal."""
    project, opa, _ = setup
    with Store() as store:
        prj.add_part(project, store, "OPA2134")
        project.parts[0]["documents"].clear()  # for example a project file of an older state
        prj.save(project)
        shutil.copyfile(opa, project.folder_path / "ti-opa.pdf")
        report = prj.sync(prj.load(project.root), store)
    assert "ti-opa.pdf" in report["added"] and not report["copied"] and not report["suggested"]


def test_project_from_a_different_machine(setup, tmp_path, monkeypatch):
    """A project with its folder (for example from git) and an empty store: the
    copies go into the store, with their parts."""
    project, _, _ = setup
    with Store() as store:
        prj.add_part(project, store, "OPA2134")
        prj.sync(project, store)
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path / "store2"))
    with Store() as store:
        report = prj.sync(prj.load(project.root), store)
        assert report["imported"] == ["opa2134.pdf"] and report["unknown_parts"] == ["OPA2134"]
        assert [r["mpn"] for r in store.part_documents("OPA2134")] == ["OPA2134"]


def test_manufacturer_selects_the_part(setup):
    project, opa, _ = setup
    with Store() as store:
        # An MPN of a different manufacturer is a different part.
        part = prj.add_part(project, store, "OPA2134", manufacturer="Burr-Brown")
        assert part["manufacturer"] == "Burr-Brown" and part["documents"] == []
        assert len(store.match_parts("OPA2134")) == 2
        project.parts.append({"mpn": "OPA2134", "manufacturer": "", "documents": []})
        report = prj.sync(project, store, apply=False)
    assert any("more parts in the store have this MPN" in p for p in report["problems"])


def test_problem_files(setup):
    project, opa, rtc = setup
    with Store() as store:
        prj.add_part(project, store, "OPA2134")
        prj.sync(project, store)
        # A PDF that cannot be read does not stop the sync.
        (project.folder_path / "broken.pdf").write_bytes(b"%PDF-1.7 not a PDF")
        # The folder has a different file with the name of a reference whose
        # document is not in the store: the file is a new document.
        project.parts[0]["documents"][0]["sha256"] = "0" * 64
        shutil.copyfile(rtc, project.folder_path / "opa2134.pdf")
        report = prj.sync(project, store)
    assert any(p.startswith("broken.pdf: not imported") for p in report["problems"])
    assert report["missing"] == ["opa2134.pdf"] and report["imported"] == ["opa2134.pdf"]
    assert prj.load(project.root).parts[0]["documents"][0]["sha256"] == "0" * 64


def test_project_file_checks(setup):
    project, _, _ = setup
    project.parts.append({"mpn": 'A"B\\C', "manufacturer": "", "documents": []})
    prj.save(project)
    assert prj.load(project.root).parts[0]["mpn"] == 'A"B\\C'
    text = project.path.read_text(encoding="utf-8")
    bad = text + '\n[[part.document]]\nsha256 = "' + "0" * 64 + '"\nfile = "../../x.pdf"\n'
    project.path.write_text(bad, encoding="utf-8")
    with pytest.raises(LensError, match="no '..'"):
        prj.load(project.root)
    # A file name in the normal form; an entry with no file is a LensError.
    project.path.write_text(text + '\n[[part.document]]\nsha256 = "' + "0" * 64 + '"\nfile = "./a//x.pdf"\n',
                            encoding="utf-8")
    assert prj.load(project.root).parts[0]["documents"][0]["file"] == "a/x.pdf"
    project.path.write_text(text + '\n[[part.document]]\nsha256 = "' + "0" * 64 + '"\n', encoding="utf-8")
    with pytest.raises(LensError, match="relative path"):
        prj.load(project.root)
    with pytest.raises(LensError, match="exists already"):
        prj.init_project(project.root)
    with pytest.raises(LensError, match="no datasheet-lens.toml"):
        prj.load(project.root.parent)


def test_cli_and_mcp(setup, capsys, monkeypatch):
    project, _, _ = setup
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    root = str(project.root)
    assert main(["project", "add-part", "OPA2134", "--path", root]) == 0
    assert main(["project", "status", "--path", root]) == 0
    assert "copy missing: opa2134.pdf" in capsys.readouterr().out
    assert main(["project", "sync", "--path", root]) == 0
    assert "element map of document 1" in capsys.readouterr().out

    import asyncio

    from datasheet_lens.server import mcp

    result = asyncio.run(mcp.call_tool("project_status", {"path": root}))
    blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    text = "\n".join(getattr(b, "text", "") for b in blocks)
    assert "OPA2134 (TI)" in text and "1 copies are current" in text
