import copy
import shutil

import pytest

from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.indexer import index_markdown
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
    return prj.init_project(tmp_path / "board", layout=""), opa, rtc


def test_header_of_a_new_file(setup):
    """A new project file tells a team member where to get the tool."""
    text = setup[0].path.read_text(encoding="utf-8")
    assert text.startswith("# datasheet-lens project file")
    assert "# Install: pipx install git+https://github.com/turbyho/datasheet-lens.git" in text
    assert not any(line != line.rstrip() for line in text.splitlines())


def test_copies_and_new_files(setup):
    project, opa, rtc = setup
    with Store() as store:
        part = prj.add_part(project, store, "opa2134")
        assert part["manufacturer"] == "TI" and part["documents"][0]["file"] == "opa2134.pdf"
        # The document came into the store with no index: the index says so,
        # and sync builds the index.
        doc_id = store.document(sha256_file(opa)).id
        with pytest.raises(LensError, match="has no index"):
            index_markdown(store, doc_id)
        report = prj.sync(project, store)
        assert report["copied"] == ["opa2134.pdf"]
        assert "## Page map\n\n- p.1:" in index_markdown(store, doc_id)
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


def test_save_keeps_the_text_of_the_user(setup):
    """Comments, format and unknown keys stay; only new data is added."""
    project, _, _ = setup
    project.path.write_text(
        "# Board X: datasheets\n"
        'folder = "datasheets"   # the default\n'
        "\n"
        "[[part]]\n"
        'mpn = "OPA2134"   # input amplifier\n'
        'note = "check the PA and UA versions"\n'
        "\n"
        "# the RTC follows\n"
        "[[part]]\n"
        'mpn = "RV-3149-C3"\n'
        'manufacturer = "Micro Crystal"\n', encoding="utf-8")
    with Store() as store:
        prj.sync(prj.load(project.root), store)
    text = project.path.read_text(encoding="utf-8")
    for kept in ("# Board X: datasheets", "# the default", 'mpn = "OPA2134"   # input amplifier',
                 'note = "check the PA and UA versions"', "# the RTC follows"):
        assert kept in text
    loaded = prj.load(project.root)
    assert loaded.parts[0]["manufacturer"] == "TI"  # from the store
    # No layout key (a file of an older version): the copies stay in the folder.
    assert loaded.parts[0]["documents"][0]["file"] == "opa2134.pdf"
    assert [p["mpn"] for p in loaded.parts] == ["OPA2134", "RV-3149-C3"]
    # A second sync with no change does not change the file.
    with Store() as store:
        prj.sync(loaded, store)
    assert project.path.read_text(encoding="utf-8") == text


def test_save_of_unusual_files(setup):
    project, opa, _ = setup
    sha = sha256_file(opa)
    with Store() as store:
        # No final newline, an MPN with spaces, a document with no role, a file
        # name that is not in the normal form: the text stays.
        project.path.write_text(f'[[part]]\nmpn = " OPA2134 "   # c\nnote = "keep me"\n\n[[part.document]]\n'
                                f'sha256 = "{sha}"\nfile = "./opa2134.pdf"   # copy\n# end', encoding="utf-8")
        loaded = prj.load(project.root)
        prj.add_part(loaded, store, "RV-3149-C3")
        text = project.path.read_text(encoding="utf-8")
        for kept in ('mpn = " OPA2134 "   # c', 'note = "keep me"', 'file = "./opa2134.pdf"   # copy', "# end"):
            assert kept in text
        assert "role" not in text and "\n\n[[part]]\nmpn = \"RV-3149-C3\"" in text
        assert [p["mpn"] for p in prj.load(project.root).parts] == ["OPA2134", "RV-3149-C3"]

        # An inline array can be read, but datasheet-lens does not write to it.
        project.path.write_text('part = [{mpn = "OPA2134", note = "n"}]\n', encoding="utf-8")
        assert prj.load(project.root).parts[0]["mpn"] == "OPA2134"
        with pytest.raises(LensError, match="not as an inline array"):
            prj.add_part(prj.load(project.root), store, "RV-3149-C3")
        assert project.path.read_text(encoding="utf-8") == 'part = [{mpn = "OPA2134", note = "n"}]\n'

        # A file that changed after load() is not written over.
        loaded = prj.load(project.root)
        project.path.write_text(project.path.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
        with pytest.raises(LensError, match="changed after datasheet-lens read it"):
            prj.sync(loaded, store)

        # A dry run does not change the project.
        loaded = prj.load(project.root)
        before = copy.deepcopy(loaded.parts)
        prj.sync(loaded, store, apply=False)
        assert loaded.parts == before

        # A multi-line string, two blank lines and a header with no blank line
        # before it: only the changed value changes.
        original = ('folder = "datasheets"\n\n\n[[part]]\nmpn = "OPA2134"\nnote = """\nline1\n\n\nline2\n[x]\n"""\n'
                    '[[part]]\nmpn = "RV-3149-C3"\n')
        project.path.write_text(original, encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts[1]["manufacturer"] = "Micro Crystal"
        prj.save(loaded)
        assert project.path.read_text(encoding="utf-8") == original + 'manufacturer = "Micro Crystal"\n'

        # The first document of a part that is not the last one: the comment
        # above the next part stays above it.
        project.path.write_text('[[part]]\nmpn = "OPA2134"\n\n# part B: important\n[[part]]\nmpn = "RV-3149-C3"\n',
                                encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts[0]["documents"].append({"sha256": sha, "file": "a.pdf", "role": "datasheet"})
        prj.save(loaded)
        assert '# part B: important\n[[part]]\nmpn = "RV-3149-C3"' in project.path.read_text(encoding="utf-8")

        # A removed part: the comment above the next part stays.
        loaded = prj.load(project.root)
        del loaded.parts[0]
        prj.save(loaded)
        assert project.path.read_text(encoding="utf-8").startswith("\n# part B: important\n[[part]]")

        # A table goes to the document with the same content first, and only then
        # to the document with the same file name.
        project.path.write_text(f'[[part]]\nmpn = "OPA2134"\n\n[[part.document]]\nsha256 = "{"1" * 64}"\n'
                                'file = "a.pdf"\nnote = "about 1"\n', encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts[0]["documents"] = [{"sha256": "2" * 64, "file": "a.pdf", "role": "datasheet"},
                                        {"sha256": "1" * 64, "file": "c.pdf", "role": "datasheet"}]
        prj.save(loaded)
        text = project.path.read_text(encoding="utf-8")
        assert f'sha256 = "{"1" * 64}"\nfile = "c.pdf"\nnote = "about 1"' in text

        # A comment above a header stays above it when a table is added before it.
        project.path.write_text(f'[[part]]\nmpn = "OPA2134"\n\n[[part.document]]\nsha256 = "{sha}"\n'
                                'file = "a.pdf"\n\n# part B: important\n[[part]]\nmpn = "RV-3149-C3"\n',
                                encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts[0]["documents"].append({"sha256": "1" * 64, "file": "b.pdf", "role": "errata"})
        prj.save(loaded)
        text = project.path.read_text(encoding="utf-8")
        assert '# part B: important\n[[part]]\nmpn = "RV-3149-C3"' in text
        assert 'role = "errata"\n\n# part B' in text

        # A file that was removed after load() is not made again.
        project.path.unlink()
        with pytest.raises(LensError, match="was removed"):
            prj.save(loaded)


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
    # The table of the old reference goes to the document with its content (the
    # store copy under a new name); the reference with the new sha256 gets a new table.
    docs = prj.load(project.root).parts[0]["documents"]
    assert docs[0]["sha256"] == sha256_file(opa) and docs[0]["file"] != "opa2134.pdf"
    assert docs[1] == {"sha256": "0" * 64, "file": "opa2134.pdf", "role": "datasheet"}


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
    # A folder that is not in the normal form.
    assert prj.init_project(project.root.parent / "other", folder="./docs/").folder == "docs"
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


def test_layout(store_home, text_pdf, tmp_path):
    """The copies go to the subfolder of the layout. A change of the layout moves
    them. A file that the user put into the folder stays (pinned)."""
    opa, rtc = text_pdf("opa2134.pdf", OPA), text_pdf("rtc.pdf", RTC)
    with Store() as store:
        doc = store.add_document(opa)
        store.link(store.add_part("OPA2134", "TI"), doc.id)
        store.add_part("RV-3149-C3", "")
        project = prj.init_project(tmp_path / "board")  # default: {manufacturer}
        assert prj.load(project.root).layout == "{manufacturer}"
        prj.add_part(project, store, "OPA2134", category="Op amp")
        assert prj.sync(project, store)["copied"] == ["TI/opa2134.pdf"]

        # A new layout with the category: sync moves the copy, and removes the
        # empty subfolder.
        text = project.path.read_text(encoding="utf-8").replace('layout = "{manufacturer}"',
                                                                 'layout = "{category}/{manufacturer}"')
        project.path.write_text(text, encoding="utf-8")
        project = prj.load(project.root)
        assert prj.sync(project, store, apply=False)["moved"] == ["TI/opa2134.pdf -> Op amp/TI/opa2134.pdf"]
        report = prj.sync(project, store)
        assert report["moved"] == ["TI/opa2134.pdf -> Op amp/TI/opa2134.pdf"]
        assert (project.folder_path / "Op amp/TI/opa2134.pdf").is_file()
        assert not (project.folder_path / "TI").exists()
        assert prj.load(project.root).parts[0]["documents"][0]["file"] == "Op amp/TI/opa2134.pdf"

        # A file that the user put into the folder: its reference is pinned, and
        # sync does not move it. A part with no category and no manufacturer
        # would go to _unknown/_unknown.
        shutil.copyfile(rtc, project.folder_path / "rv3149.pdf")
        report = prj.sync(project, store)
        sha = report["unassigned"][0]["sha256"]
        part = prj.add_part(project, store, "RV-3149-C3", document=sha[:12])
        assert part["documents"] == [{"sha256": sha, "file": "rv3149.pdf", "role": "datasheet", "pinned": True}]
        assert prj.sync(project, store)["moved"] == []
        assert "pinned = true" in project.path.read_text(encoding="utf-8")
        assert prj.layout_dir(project, {"mpn": "X", "manufacturer": ""}) == "_unknown/_unknown"


def test_layout_checks(tmp_path):
    for bad, message in (("{vendor}", "use only"), ("../{mpn}", "no '..'"), ("{mpn", "use")):
        with pytest.raises(LensError, match=message):
            prj.init_project(tmp_path / bad.replace("/", "_"), layout=bad)
    project = prj.init_project(tmp_path / "flat", layout="")
    assert prj.layout_dir(project, {"mpn": "X", "manufacturer": "TI"}) == ""
    assert prj.layout_dir(prj.Project(tmp_path, layout="{manufacturer}"),
                          {"mpn": "X", "manufacturer": "A/B: C"}) == "A_B_ C"


def test_moved_document_block(store_home, text_pdf, tmp_path):
    """A [[part.document]] block that a person moved under a different part: sync
    tells it (the store links the document to the first part), and it does not
    move the copy into the folder of the wrong part."""
    opa, rtc = text_pdf("opa2134.pdf", OPA), text_pdf("rtc.pdf", RTC)
    with Store() as store:
        store.link(store.add_part("OPA2134", "TI"), store.add_document(opa).id)
        doc = store.add_document(rtc)
        store.link(store.add_part("RV-3149-C3", "Micro Crystal"), doc.id)
        project = prj.init_project(tmp_path / "board")  # layout {manufacturer}
        prj.add_part(project, store, "OPA2134")
        prj.add_part(project, store, "RV-3149-C3")
        assert not prj.sync(project, store)["misplaced"]
        # Move the document block of RV-3149-C3 under OPA2134 (above the second part).
        text = project.path.read_text(encoding="utf-8")
        head, rtc_part = text.split('[[part]]\nmpn = "RV-3149-C3"', 1)
        rtc_doc = rtc_part[rtc_part.index("[[part.document]]"):]
        rtc_head = rtc_part[:rtc_part.index("[[part.document]]")]
        project.path.write_text(head + rtc_doc + '\n[[part]]\nmpn = "RV-3149-C3"' + rtc_head, encoding="utf-8")
        report = prj.sync(prj.load(project.root), store)
    item = report["misplaced"][0]
    assert (item["part"], item["linked"]) == ("OPA2134", ["RV-3149-C3 (Micro Crystal)"])
    lines = prj.format_report(report, True, "add {mpn} {sha}")
    assert any("was the block moved?" in line and f"add OPA2134 {doc.sha256[:12]}" in line for line in lines)
    assert (project.folder_path / "Micro Crystal/rtc.pdf").is_file()
    assert not (project.folder_path / "TI/rtc.pdf").exists()


def test_shared_document_on_a_new_machine(store_home, text_pdf, tmp_path, monkeypatch):
    """One datasheet under two parts (variants), and an empty store: no false
    report of a moved block, and the store links it to the two parts."""
    with Store() as store:
        doc = store.add_document(text_pdf("adc.pdf", [[(50, 60, 14, "ADC34J24")]]))
        store.link_parts(doc.id, "ADC34J24", ["ADC34J22"], "TI")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "ADC34J24")
        prj.add_part(project, store, "ADC34J22")
        prj.sync(project, store)
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path / "store2"))
    with Store() as store:
        for _ in range(2):
            report = prj.sync(prj.load(project.root), store)
            assert not report["misplaced"], report["misplaced"]
        assert {r["mpn"] for r in store.parts_of_document(store.document(doc.sha256).id)} == {"ADC34J24",
                                                                                            "ADC34J22"}
