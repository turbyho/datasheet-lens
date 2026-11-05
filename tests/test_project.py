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
        lines = prj.format_report(report, True, ("add {part}{base} {sha}", " -b {base}"))
        sha = item["sha256"][:12]
        assert any("proposal: RV-3149-C3" in line and f"add RV-3149-C3 {sha}" in line
                   and f"add <MPN> -b RV-3149-C3 {sha}" in line for line in lines), lines

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
        # An MPN of a manufacturer that is not the same in the registry: an error.
        with pytest.raises(LensError, match="does not know if Burr-Brown is the same manufacturer"):
            prj.add_part(project, store, "OPA2134", manufacturer="Burr-Brown")
        # An MPN of a different manufacturer (a second source) is a different part.
        store.maker_relation("distinct", "Burr-Brown", "TI")
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
    lines = prj.format_report(report, True, ("add {part}{base} {sha}", " -b {base}"))
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


def test_key_order(store_home, text_pdf, tmp_path):
    """A new key goes to its place, and sync puts the keys of a file in their
    sequence; a comment at the end of a line moves with its key."""
    project = prj.init_project(tmp_path / "board", layout="")
    project.path.write_text(
        'folder = "datasheets"\n\n[[part]]\nbase = "TLV755P"\nmpn = "TLV75533PDBVR"  # LDO 3.3 V\n'
        'note = "keep"\n', encoding="utf-8")
    loaded = prj.load(project.root)
    loaded.parts[0]["manufacturer"] = "TI"
    prj.save(loaded)
    text = project.path.read_text(encoding="utf-8")
    assert ('mpn = "TLV75533PDBVR"  # LDO 3.3 V\nmanufacturer = "TI"\nbase = "TLV755P"\nnote = "keep"' in text), text


def test_one_copy_for_a_document_of_two_parts(store_home, text_pdf, tmp_path):
    """Two concrete parts of one base part share its datasheet: sync copies it
    one time and counts it one time."""
    with Store() as store:
        doc = store.add_document(text_pdf("tps7a02.pdf", OPA))
        store.link(store.add_part("TPS7A02", "TI"), doc.id)
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0233PYCHR", base="TPS7A02")
        prj.add_part(project, store, "TPS7A0218PYCHR", base="TPS7A02")
        lines = prj.format_report(prj.sync(project, store), True, ("", ""))
        assert lines == ["copied: TI/tps7a02.pdf", "1 copies are current"], lines
        # The next sync and a move to a new layout count the file one time too.
        assert prj.format_report(prj.sync(project, store), True, ("", "")) == ["1 copies are current"]
        project.layout = "{category}"
        lines = prj.format_report(prj.sync(project, store), True, ("", ""))
    assert lines == ["moved to its subfolder: TI/tps7a02.pdf -> _unknown/tps7a02.pdf", "1 copies are current"], lines


def test_the_add_hints_of_the_cli_and_the_server():
    """The hints of the real CLI and MCP templates: the base argument only when
    there is a base part."""
    from datasheet_lens import cli, server

    report = {k: [] for k in ("ok", "copied", "restored", "changed", "missing", "imported", "added", "moved",
                              "unknown_parts", "problems", "misplaced", "suggested", "unassigned")}
    report["misplaced"] = [{"file": "a.pdf", "mpn": "TLV755P", "part": "TLV75533PDBVR", "base": "TLV755P",
                            "sha256": "a" * 64, "linked": ["X"]},
                           {"file": "b.pdf", "mpn": "OPA2134", "part": "OPA2134", "base": "", "sha256": "b" * 64,
                            "linked": ["Y"]}]
    report["unassigned"] = [{"file": "c.pdf", "sha256": "c" * 64}]
    mcp = "\n".join(prj.format_report(report, False, server.PROJECT_ADD_HINT))
    assert "project_add_part(mpn='TLV75533PDBVR', base='TLV755P', document='aaaaaaaaaaaa')" in mcp
    assert "project_add_part(mpn='OPA2134', document='bbbbbbbbbbbb')" in mcp
    assert "project_add_part(mpn='<MPN>', document='cccccccccccc')" in mcp
    text = "\n".join(prj.format_report(report, False, cli.PROJECT_ADD_HINT))
    assert 'project add-part "TLV75533PDBVR" --base "TLV755P" --document aaaaaaaaaaaa' in text
    assert 'project add-part "OPA2134" --document bbbbbbbbbbbb' in text


def test_pdf_files_outside_the_folder(store_home, text_pdf, tmp_path):
    """sync tells about the PDF files in other folders of the repository: how
    many, and how many the store and the project have. It does not import them."""
    project = prj.init_project(tmp_path / "board")
    used = project.root / "docs" / "used"
    used.mkdir(parents=True)
    shutil.copy(text_pdf("opa.pdf", OPA), used / "opa.pdf")
    shutil.copy(text_pdf("rtc.pdf", RTC), used / "rtc.pdf")
    (project.root / ".git").mkdir()
    shutil.copy(text_pdf("hidden.pdf", OPA), project.root / ".git" / "hidden.pdf")
    with Store() as store:
        doc = store.add_document(used / "opa.pdf")
        store.link(store.add_part("OPA2134", "TI"), doc.id)
        prj.add_part(project, store, "OPA2134")
        report = prj.sync(project, store)
    assert report["outside"] == [{"folder": "docs/used", "files": 2, "in_store": 1, "in_project": 1}]
    assert report["imported"] == []
    assert any("PDF files outside the project folder: docs/used/ (2 files, 1 in the store, 1 documents of"
               in line for line in prj.format_report(report, True, ("", "")))


def test_check_with_the_cad_project(store_home, text_pdf, tmp_path, capsys):
    """project_check and the CLI check compare the project with the CAD parts:
    the same, different (one part with two MPNs), missing and extra."""
    with Store() as store:
        opa = store.add_document(text_pdf("opa.pdf", OPA))
        store.link(store.add_part("OPA2134", "TI"), opa.id)
        rtc = store.add_document(text_pdf("rtc.pdf", RTC))
        store.link(store.add_part("RV-3149-C3", "Micro Crystal"), rtc.id)
        store.link(store.add_part("TMP112D", "TI"), opa.id)
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134")
        prj.add_part(project, store, "NRF9151-LACA-R7")
        prj.add_part(project, store, "LM358")
        cad = [{"mpn": "OPA2134", "reference": "U1"}, {"mpn": "OPA2134", "reference": "U2"},
               {"mpn": "nRF9151-LAXX", "reference": "U3"}, {"mpn": "RV-3149-C3", "reference": "U4"},
               {"mpn": "TMP112D0IDPWR", "reference": "U5"},
               {"mpn": "XYZ123", "reference": "U6", "datasheet": "https://example.com/xyz.pdf"}]
        result = prj.check_parts(project, store, cad)
    assert result["ok"] == ["OPA2134"] and [(d["cad"], d["mpn"]) for d in result["different"]] == [
        ("nRF9151-LAXX", "NRF9151-LACA-R7")]
    assert result["extra"] == ["LM358"]
    missing = {m["mpn"]: m for m in result["missing"]}
    assert missing["RV-3149-C3"]["documents"] == [rtc.sha256[:12]] and missing["TMP112D0IDPWR"]["bases"] == ["TMP112D"]
    text = "\n".join(prj.format_check(result))
    assert f"missing: RV-3149-C3 (U4): the store has its documents {rtc.sha256[:12]}: project_add_part(" in text
    assert "project_add_part(mpn='TMP112D0IDPWR', base='TMP112D')" in text
    assert "add_document(path='https://example.com/xyz.pdf')" in text and "extra: LM358" in text

    # A BOM with only a Value column: the MPNs are the texts in parentheses.
    bom = tmp_path / "bom.csv"
    bom.write_text('"Reference","Value","Datasheet","DNP"\n"U1","OPA2134 (OPA2134)","~",""\n"C1","100nF","~",""\n'
                   '"C2","4.7uF/10V","~",""\n"J2","Conn_01x05_Socket","~",""\n"U7","LM358 (LM358)","~",""\n'
                   '"U8","LM358 (LM3580DR/NOPB)","~",""\n"U9","XYZ (XYZ123)","~","DNP"\n', encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "MPN from (no column); Value from Value; references from Reference; no column Value_Confirmed" in out
    assert "CAD lines with a part: 3" in out
    assert "the same in the CAD project and in the project file: 2" in out
    assert "extra: NRF9151-LACA-R7" in out and "missing: LM3580DR/NOPB (U8)" in out
    assert "no part selected (a general value and no MPN): C1, C2, J2." in out
    # One column: the sniffer cannot find a delimiter.
    bom.write_text("MPN\nOPA2134\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0


def test_near_parts():
    """One part with two MPNs, and parts that only start alike."""
    def near(cad, mpn, **kw):
        return prj._near(cad, {"mpn": mpn, **kw})

    assert near("TMP112D0", "TMP112D0IDPWR") and near("STM32F103C8Tx", "STM32F103C8T6")
    assert near("nRF9151-LAXX", "NRF9151-LACA-R7") and near("TLV75533PDBVR", "TLV755P", variants=["TLV75533PDBVR"])
    assert not near("LM3580", "LM358") and not near("LM3", "LM358") and not near("OPA134", "OPA2134")


def test_check_reads_bom_files_of_other_cad_tools(store_home, text_pdf, tmp_path, capsys):
    """Altium (Designator, "Manufacturer Part Number 1"), Eagle/Fusion (Parts,
    MPN), a semicolon and a cp1252 file from Windows."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134PA")
    bom = tmp_path / "altium.csv"
    bom.write_bytes('"Designator";"Comment";"Manufacturer Part Number 1"\n"U1";"Op amp µ";"OPA2134PA"\n'
                    .encode("cp1252"))
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert ("MPN from Manufacturer Part Number 1; references from Designator; no column Value_Confirmed; read as "
            "cp1252") in out
    # No Value column: the tool does not know the Value, thus it does not complete it.
    assert "not confirmed" not in out and "complete the fields" not in out
    assert "the same in the CAD project and in the project file: 1" in out
    bom = tmp_path / "eagle.csv"
    # Each library puts the MPN into a different attribute: the first filled one of the row.
    bom.write_text("Qty;Value;Parts;MP;MANUFACTURER_PART_NUMBER\n2;10k;R1 R2;;\n1;OPA2134;U1;OPA2134PA;\n"
                   "1;X;U2;;LM358DR/NOPB\n1;1N4148W;D3;;\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert ("MPN from MANUFACTURER_PART_NUMBER, MP; Value from Value; references from Parts; no column "
            "Value_Confirmed") in out
    assert "CAD lines with a part: 2" in out and "missing: LM358DR/NOPB (U2)" in out
    # The MPN is only in the field MPN: the tool completes Value by the convention. The write
    # goes into the attribute of the MPN of the line (MP).
    assert ("complete the fields: U1: the MPN is not in Value (the convention: the MPNs in parentheses in Value). "
            "The parts do not change. Write Value = 'OPA2134 (OPA2134PA)' and MP = 'OPA2134PA' into the "
            "symbol.") in out
    assert "no part selected (a general value and no MPN): R1 R2, D3." in out


def test_symbol_state_sixth_review_cases(store_home, tmp_path):
    """A "no part" text in a BOM file; the packing code of NXP after a comma; an MPN
    with a digit first and a slash; Value_Confirmed of the BOM files of variants."""
    from datasheet_lens.project import CadSymbol, field_items, field_list, symbol_state

    bom = tmp_path / "tp.csv"
    bom.write_text("Reference,Value,MPN\nTP1,TestPoint,-\n", encoding="utf-8")
    (symbol,) = prj.bom_parts(str(bom)).cad
    assert (symbol.mpn, symbol_state(symbol).no_mpn) == ("-", False)
    assert field_list('"BC847B,215", "BC857B,215"') == (["BC847B,215", "BC857B,215"], "")
    assert field_items("BC847B,215") == ["BC847B,215"]  # one MPN with a packing code
    assert field_items("100nF, 100") == ["100nF", "100"]  # no comma in the code: a space
    s = symbol_state(CadSymbol(value="74HC595D (74HC595D,118)", mpn="74HC595D,118",
                               value_confirmed="74HC595D (74HC595D,118)"))
    assert (s.parts, s.change, s.conflicts) == (["74HC595D,118"], "", [])
    s = symbol_state(CadSymbol(value="74HC595D", mpn="74HC595D/T3", value_confirmed="74HC595D"))
    assert (s.parts, s.conflicts) == (["74HC595D/T3"], [])
    with Store() as store:
        result = prj.check_parts(prj.init_project(tmp_path / "board"), store, [
            CadSymbol(reference="C1", value="100nF", mpn="GRM188R71H104KA93D", value_confirmed="100nF"),
            CadSymbol(reference="C1", value="220nF", mpn="GRM188R71H104KA93D", variant="b.csv"),
            CadSymbol(reference="C2", value="1uF", mpn="GRM188R61A105KA61D", variant="b.csv"),
            CadSymbol(reference="C2", value="1uF", mpn="GRM188R61A105KA61D", variant="c.csv")])
    assert (result["no_value_confirmed"], result["lines"]) == (2, 2)


def test_review_cases_of_2026_11_05(store_home, tmp_path):
    """TBD is "not selected"; a comma between digits in a list of MPNs; an MPN with no
    digit; two equal MPNs; a change that removes the MPNs; the inherited Datasheet
    field of a variant; two spellings of one part; a variant line with no
    reference."""
    from datasheet_lens.project import CadSymbol, symbol_state

    def state(value, mpn="", confirmed=None):
        return symbol_state(CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed))

    # TBD: an empty field MPN (no part selected yet); N/A: the symbol needs no part.
    s = state("100nF", "TBD", "100nF")
    assert (s.no_part, s.no_mpn, s.parts, s.conflicts) == (False, True, [], [])
    s = state("100nF", "N/A", "100nF")
    assert (s.no_part, s.no_mpn, s.conflicts) == (True, False, [])
    s = state("2N7002 (TBD, 2N7002LT1G)", "TBD, 2N7002LT1G", "2N7002 (TBD, 2N7002LT1G)")
    assert (s.parts, s.conflicts) == (["2N7002LT1G"], [])  # TBD does not tell "no part"
    bom = tmp_path / "tbd.csv"
    bom.write_text("Reference,Value,MPN,MP\nC1,100nF,TBD,\nTP1,TestPoint,N/A,\nQ1,2N7002,TBD,2N7002LT1G\n",
                   encoding="utf-8")
    assert [(p.mpn, p.conflict) for p in prj.bom_parts(str(bom)).cad] == [
        ("", ""), ("N/A", ""), ("2N7002LT1G", "")]
    # A list of MPNs has no decimal comma; an MPN can have no digit; the text of KiCad
    # for different values is not an MPN.
    assert state("Q (2N7002,2N7000)", "2N7002,2N7000").parts == ["2N7002", "2N7000"]
    assert state("J (PJ-SMD)", "PJ-SMD").parts == ["PJ-SMD"]
    assert state("10k", "-- mixed values --").conflicts == ["MPN -- mixed values --: not an MPN"]
    # TBD is an empty field, also in the parentheses: (TBD) has nothing to confirm.
    s = state("10k (A)", "TBD", "")
    assert (s.parts, s.unconfirmed, s.change, s.write_mpn) == (["A"], True, "", "A")
    s = state("10k (TBD)", "", "")
    assert (s.unconfirmed, s.no_mpn, s.write_mpn) == (False, True, "")
    # A field MPN that is empty now, after a confirmation: a change, not a first one.
    s = state("10k (A)", "", "10k (A)")
    assert (s.parts, s.new, s.unconfirmed) == ([], ["A"], False)
    assert s.change == "Value '10k (A)', confirmed MPN '(empty)': the MPNs in the parentheses are not the confirmed MPNs"
    # A symbol with a conflict gets no proposal, and no "no part selected".
    s = state("10k", "-- mixed values --")
    assert (s.no_mpn, s.complete) == (False, "")
    s = state("10k (A)", "-- mixed values --", "")
    assert (s.parts, s.change, s.unconfirmed, s.write_mpn) == (["A"], "", False, "")
    s = symbol_state(CadSymbol(value="10k", mpn="A", other_mpns=[("MP", "B")]))
    assert (s.parts, s.complete) == (["A", "B"], "")
    # A new general value: the confirmed MPN goes, also when the old Value had no MPN.
    assert state("220nF", "GRM1", "100nF").removed == ["GRM1"]
    # The completion writes each MPN one time.
    s = state("10k", "A, A")
    assert (s.complete, s.write_mpn) == ("10k (A)", "A")
    # Two equal MPNs are one part, and no change.
    s = state("2N7002 (2N7002LT1G, 2N7002LT1G)", "2N7002LT1G, 2N7002LT1G", "2N7002 (2N7002LT1G, 2N7002LT1G)")
    assert (s.parts, s.change, s.conflicts) == (["2N7002LT1G"], "", [])
    # A change that removes the MPNs from Value names them.
    s = state("100nF", "2N7002LT1G", "2N7002 (2N7002LT1G)")
    assert (s.select, s.removed) == (True, ["2N7002LT1G"])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q1", value="100nF", mpn="2N7002LT1G", value_confirmed="2N7002 (2N7002LT1G)")])
    assert "Not in Value now: 2N7002LT1G." in "\n".join(prj.format_check(result))
    # A variant line with the Datasheet field of the default variant inherits it
    # (KiCad 10): it is not the datasheet of the other part of the variant.
    url = "https://ti.com/tps62130.pdf"

    def fitted(ref, mpn, **kw):
        value = f"{mpn[:8]} ({mpn})"
        return CadSymbol(reference=ref, value=value, mpn=mpn, value_confirmed=value, **kw)

    with Store() as store:
        result = prj.check_parts(project, store, [
            fitted("U6", "TPS62140RGTR", datasheet=url, variant="b.csv"),
            fitted("U6", "TPS62130RGTR", datasheet=url),
            fitted("U6", "TPS62150RGTR", datasheet="https://ti.com/tps62150.pdf", variant="c.csv")])
    assert {m["mpn"]: m["datasheet"] for m in result["missing"]} == {
        "TPS62130RGTR": url, "TPS62140RGTR": "", "TPS62150RGTR": "https://ti.com/tps62150.pdf"}
    # Two spellings of one part (signs, case): one CAD part, the first spelling.
    with Store() as store:
        result = prj.check_parts(project, store, [fitted("U1", "LTC3588EMSE#PBF"), fitted("U2", "LTC3588EMSEPBF")])
    assert [(m["mpn"], m["references"]) for m in result["missing"]] == [("LTC3588EMSE#PBF", ["U1", "U2"])]
    # With more variants, each line needs its reference.
    with Store() as store, pytest.raises(LensError, match="variant b.csv: a line with no reference"):
        prj.check_parts(project, store, [fitted("U1", "TPS62130RGTR"), fitted("", "TPS62140RGTR", variant="b.csv")])


def test_split_value():
    """The convention of the parentheses: the MPNs are the text in parentheses, the
    other text is the general value."""
    from datasheet_lens.project import ValueParts, _value_key, field_items, field_list, format_mpns, split_value

    assert split_value("2N7002 (2N7002LT1G, BSS138LT1G)") == ValueParts("2N7002", ["2N7002LT1G", "BSS138LT1G"])
    assert split_value("100nF  10V (GRM155R71A104KA01D) X7R") == ValueParts("100nF 10V X7R", ["GRM155R71A104KA01D"])
    assert split_value('("BC847B,215"; BC857B)') == ValueParts("", ["BC847B,215", "BC857B"])
    assert split_value("Q (BC847B,215)") == ValueParts("Q", ["BC847B,215"])  # one MPN with a packing code
    # Quotes: each character in an MPN, "" is a quote; a quote only at the start of an item.
    assert split_value('J ("A(1)", "B;2", "5"" LCD")').mpns == ["A(1)", "B;2", '5" LCD']
    assert split_value('5" LCD (ICL7660"CPA)') == ValueParts('5" LCD', ['ICL7660"CPA'])
    assert split_value("U (ABC 123)").mpns == ["ABC 123"]  # a space needs no quotes
    assert split_value("4,7k 10V") == ValueParts("4,7k 10V", None)  # no parentheses: no MPN
    assert split_value("10k (1%)").mpns == ["1%"]  # each text in parentheses is a list of MPNs
    assert split_value("10k ()") == ValueParts("10k", [])
    assert split_value("~") == ValueParts("", None)
    for text, error in (("a (b) (c)", "more than one text in parentheses"), ("a (b", "not closed"),
                        ("a ) b (", "not opened"), ("a (b))", "not opened"), ("a (b(1))", "a parenthesis in an MPN"),
                        ('a ("b)', "a quote that is not closed"), ('a ("b" c)', "a text after a quote")):
        parts = split_value(text)
        assert parts.mpns is None and error in parts.error and "Write the MPNs in one text" in parts.error
    # A list that is not clear: an error, and one item, the full text (no MPN is lost).
    parts = split_value("Q (BC847B,215, BSS138) X")
    assert (parts.general, parts.mpns) == ("Q X", ["BC847B,215, BSS138"])
    assert "a comma before three digits can be a separator" in parts.error
    # The spaces around an item do not count, also in quotes; an item of spaces is empty.
    assert split_value('Q (" A ", " ", B)').mpns == ["A", "B"]
    assert field_list("A,100), B") == (["A", "100)", "B"], "")  # in the field MPN, ")" is text
    # The comparison: the words of the general value and the list of the MPNs.
    assert _value_key("2N7002 (A; B)") == _value_key('2N7002  ("A",B)')  # the quotes that are not necessary
    assert _value_key("10 k") != _value_key("10k") and _value_key("4m7") != _value_key("4M7")
    assert _value_key("10k") != _value_key("10k (A)") != _value_key("10k (B)")
    assert field_items('2N7002,2N7000; "BC847B,215"') == ["2N7002", "2N7000", "BC847B,215"]
    assert field_items("A|B or C") == ["A|B or C"]  # the separators are , and ;
    assert field_items("ABC(1)") == ["ABC(1)"]  # a parenthesis needs no quotes in the field MPN
    # A list that is not clear: one item, the full text (no MPN is lost), and an error.
    items, error = field_list("BC847B,215, BSS138")
    assert items == ["BC847B,215, BSS138"] and "write an MPN with , ; or a parenthesis in quotes" in error
    # Quotes only where they are necessary.
    assert format_mpns(["BC847B,215"]) == "BC847B,215" and format_mpns(["A", "B"]) == "A, B"
    assert format_mpns(["BC847B,215", "BSS138"]) == '"BC847B,215", BSS138'
    assert format_mpns(["A(1)", 'B"C']) == 'A(1), B"C' and format_mpns(["A(1)"], value=True) == '"A(1)"'
    assert format_mpns(['"A', "B;C"]) == '"""A", "B;C"'
    assert format_mpns(["~"]) == '"~"' and field_list('"~"') == (["~"], "")  # the text gives the items again


def test_symbol_state():
    """The model of plans/confirmed-mpns.md (decisions 13 to 16). The confirmation of
    the user is the truth: until the user confirms a change, the confirmed field MPN
    gives the parts."""
    from datasheet_lens.project import CadSymbol, symbol_state

    def state(value, mpn="", confirmed=None, **kw):
        return symbol_state(CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed, **kw))

    both = "2N7002 (2N7002LT1G, BSS138LT1G)"
    # Confirmed: the same MPNs, and Value equal to Value_Confirmed (spaces and the
    # separators of the list do not count).
    s = state(both, "2N7002LT1G; BSS138LT1G", "2N7002  (2N7002LT1G; BSS138LT1G)")
    assert (s.parts, s.change, s.unconfirmed, s.complete) == (["2N7002LT1G", "BSS138LT1G"], "", False, "")
    # Not confirmed: an empty Value_Confirmed, or an empty field MPN.
    assert state(both, "2N7002LT1G, BSS138LT1G", "").unconfirmed
    s = state(both, "", None)
    assert (s.parts, s.unconfirmed, s.write_mpn) == (["2N7002LT1G", "BSS138LT1G"], True, "2N7002LT1G, BSS138LT1G")
    # An other list in the field MPN: a change; the confirmed MPN stays the part.
    s = state(both, "2N7002LT1G")
    assert (s.parts, s.new, s.removed, s.write_mpn) == (["2N7002LT1G"], ["BSS138LT1G"], [], "2N7002LT1G, BSS138LT1G")
    assert s.change.endswith("the MPNs in the parentheses are not the confirmed MPNs")
    assert state(both, "BSS138LT1G, 2N7002LT1G").change  # an other order
    assert state("Q (bss138lt1g)", "BSS138LT1G").change  # the field is a copy: case counts
    # Value is not equal to Value_Confirmed: a change.
    s = state("220nF (GRM2)", "GRM1", "100nF (GRM1)")
    assert (s.parts, s.new, s.removed, s.write_mpn, s.select) == (["GRM1"], ["GRM2"], ["GRM1"], "GRM2", False)
    s = state("220nF", "GRM1", "100nF (GRM1)")  # a new general value: the user selects the part
    assert (s.parts, s.select, s.removed) == (["GRM1"], True, ["GRM1"])
    assert state("220nF", "GRM1", "100nF").select  # a confirmed Value with no MPN in it
    assert state("10 k (A)", "A", "10k (A)").change  # the words count
    assert state("220nF", "", "100nF").change.endswith(": no MPN is confirmed")
    s = state("TestPoint2", "-", "TestPoint")
    assert "the symbol needs no part (MPN '-'): this can be wrong now" in s.change and s.write_mpn == "-"
    # No parentheses and an MPN in the field: complete the fields by the convention.
    s = state("10k", "RC0603FR-0710KL", "10k")
    assert (s.parts, s.change, s.complete, s.write_mpn) == (["RC0603FR-0710KL"], "", "10k (RC0603FR-0710KL)",
                                                            "RC0603FR-0710KL")
    assert state("10k", "RC0603FR-0710KL").complete == "10k (RC0603FR-0710KL)"  # also with no Value_Confirmed
    assert not state("", "RC0603FR-0710KL").complete  # no Value: a BOM with no Value column
    assert not state("10k", "RC0603FR-0710KL, -").complete  # a conflict: not into Value
    # Value_Confirmed with parentheses and a Value with none: the user removed the MPNs.
    s = state("10k", "RC0603FR-0710KL", "10k (RC0603FR-0710KL)")
    assert (s.select, s.removed, s.complete) == (True, ["RC0603FR-0710KL"], "")
    # No part selected; no part needed.
    assert state("100nF", confirmed="").no_mpn and state("100nF (TBD)", "TBD", "100nF (TBD)").no_mpn
    s = state("TestPoint", "-", "TestPoint")
    assert (s.parts, s.no_mpn, s.no_part, s.conflicts, s.change) == ([], False, True, [], "")
    assert state("TestPoint (-)", "", "").no_part  # a no-part text in Value, not confirmed yet
    # Conflicts: a Value that does not follow the convention, a no-part text and an
    # MPN in one list.
    s = state("10k (A) (B)", "A", "10k (A)")
    assert s.conflicts[0].startswith("Value '10k (A) (B)': more than one text in parentheses") and not s.change
    assert s.parts == ["A"]
    assert "a comma before three digits" in state("Q (X)", "BC847B,215, BSS138").conflicts[0]
    s = state("Q (BC847B,215, BSS138)", "", "")
    assert (s.parts, s.unconfirmed) == (["BC847B,215, BSS138"], False) and "a comma before" in s.conflicts[0]
    s = state('Q ("BC847B,215", BSS138)', '"BC847B,215", BSS138', other_mpns=[("MP", "BC847B,215, BSS138")])
    assert "MP 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
    s = state("Q (X)", "X", other_values=["Q (BC847B,215, BSS138)"])
    assert s.extra == ["BC847B,215, BSS138"]  # the MPNs of an other Value column are not lost
    # Quotes: the fields are equal also with quotes that are not necessary; a
    # completion writes quotes only where they are necessary.
    assert not state('Q ("A", B)', "A, B", "Q (A, B)").change
    s = state("Q", '"BC847B,215", BSS138')
    assert (s.complete, s.write_mpn) == ('Q ("BC847B,215", BSS138)', '"BC847B,215", BSS138')
    assert state("Q", "BC847B,215").complete == "Q (BC847B,215)"
    s = state("J", "A(1)")
    assert (s.complete, s.write_mpn) == ('J ("A(1)")', "A(1)")
    assert not state('J ("A(1)")', "A(1)", 'J ("A(1)")').change
    assert state("Q (GRM1, -)", "GRM1, -").conflicts == [
        "MPN GRM1, -: - tells that the symbol needs no part", "Value GRM1, -: - tells that the symbol needs no part"]
    # More columns: a conflict, and the MPNs of the other column are parts too.
    s = state("2N7002 (2N7002LT1G)", "2N7002LT1G", "2N7002 (2N7002LT1G)", other_mpns=[("MP", "BSS138LT1G")])
    assert (s.parts, s.extra, s.conflicts) == (["2N7002LT1G", "BSS138LT1G"], ["BSS138LT1G"],
                                               ["MPN 2N7002LT1G, MP BSS138LT1G"])
    s = state("2N7002 (2N7002LT1G)", "2N7002LT1G", "2N7002 (2N7002LT1G)", other_values=["BSS138 (BSS138LT1G)"])
    assert s.parts == ["2N7002LT1G", "BSS138LT1G"]
    assert state("2N7002 (2N7002LT1G)", "2N7002LT1G", other_values=["2N7002 (2N7002LT1G)"]).extra == []


def test_check_tells_about_changes(store_home, tmp_path, capsys):
    """The messages of project_check and of the CLI check: a change, a symbol that
    is not confirmed, the completion of the fields, a symbol with no part, the
    variants, the power symbols, and the column of Value_Confirmed."""
    from datasheet_lens.project import CadSymbol

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("2N7002LT1G", "BSS138LT1G"):
            prj.add_part(project, store, mpn)
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q1", value="2N7002 (2N7002LT1G, BSS138LT1G)", mpn="2N7002LT1G",
                      value_confirmed="2N7002 (2N7002LT1G)"),
            CadSymbol(reference="U1", value="ESP32-S3 (ESP32-S3-WROOM-1-N16R8)", value_confirmed=""),
            CadSymbol(reference="TP1", value="TestPoint_Small", mpn="-", value_confirmed="TestPoint"),
            CadSymbol(reference="#PWR01", value="+3V3", value_confirmed=""),
            CadSymbol(reference="R1", value="10k", value_confirmed=""),
            CadSymbol(reference="R5", value="10k", mpn="RC0603FR-0710KL", value_confirmed="10k"),
            CadSymbol(reference="C5", value="220nF", mpn="GRM1", value_confirmed="100nF (GRM1)"),
            CadSymbol(reference="Q2", value="BSS138 (BSS138LT1G)", mpn="BSS138LT1G",
                      value_confirmed="BSS138 (BSS138LT1G)", variant="b"),
            CadSymbol(reference="Q2", value="BSS138 (BSS138LT1G)", mpn="BSS138LT1G",
                      value_confirmed="BSS138 (BSS138LT1G)"),
            CadSymbol(reference="Q3", value="BSS138 (BSS138LT1G)", mpn="BSS138LT1G",
                      value_confirmed="BSS138 (BSS138LT1G)"),
            CadSymbol(reference="Q3", value="2N7002 (2N7002LT1G)", mpn="BSS138LT1G",
                      value_confirmed="BSS138 (BSS138LT1G)", variant="b")])
    out = "\n".join(prj.format_check(result))
    assert ("change in the CAD project: Q1: Value '2N7002 (2N7002LT1G, BSS138LT1G)', Value_Confirmed '2N7002 "
            "(2N7002LT1G)': the confirmed MPN '2N7002LT1G' can be wrong now. New: BSS138LT1G. Tell the user.") in out
    assert ("Write MPN = '2N7002LT1G, BSS138LT1G' and Value_Confirmed = '2N7002 (2N7002LT1G, BSS138LT1G)' into the "
            "symbol") in out
    assert ("not confirmed: U1: When the user agrees: Write MPN = 'ESP32-S3-WROOM-1-N16R8' and Value_Confirmed = "
            "'ESP32-S3 (ESP32-S3-WROOM-1-N16R8)' into the symbol.") in out
    assert "TP1" in out and "Write MPN = '-'" in out
    assert "#PWR01" not in out and "no part selected (a general value and no MPN): R1." in out
    assert ("complete the fields: R5: the MPN is not in Value (the convention: the MPNs in parentheses in Value). "
            "The parts do not change. Write Value = '10k (RC0603FR-0710KL)', MPN = 'RC0603FR-0710KL' and "
            "Value_Confirmed = '10k (RC0603FR-0710KL)' into the symbol.") in out
    assert ("C5: Value '220nF', Value_Confirmed '100nF (GRM1)': the confirmed MPN 'GRM1' can be wrong now. Not in "
            "Value now: GRM1. Tell the user. When the user confirms the change: First select the new part with the "
            "user (get_ordering_codes, search_distributors), and write its MPN into Value in parentheses") in out
    assert "Write MPN = '<the MPN of the new part>' and Value_Confirmed = '<the new Value>'" in out
    assert "Q2" not in out  # the variant gives the same fields as the default variant
    assert "Q3 (variant b)" in out and "into the fields of the symbol in the variant of the BOM file b" in out
    assert result["lines"] == 6  # Q1, U1, R5, C5, Q2, Q3
    # A line of a group (Q5,Q6) and a line of one of its symbols in a variant file.
    with Store() as store:
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q5,Q6", value="2N7002 (2N7002LT1G)", value_confirmed=""),
            CadSymbol(reference="Q5", value="2N7002 (2N7002LT1G)", value_confirmed="", variant="b.csv")])
    assert (len(result["unconfirmed"]), result["lines"]) == (1, 1)
    with Store() as store:
        result = prj.check_parts(project, store, [
            CadSymbol(reference="U6", value="TPS62130 (TPS62130RGTR)", mpn="TPS62130RGTR",
                      datasheet="https://ti.com/tps62130.pdf")])
    (missing,) = result["missing"]
    assert missing["datasheet"] == "https://ti.com/tps62130.pdf"

    bom = tmp_path / "altium.csv"
    bom.write_text("Designator,Value,MPN,Custom 3\nQ1,2N7002 (2N7002LT1G),2N7002LT1G,2N7002 (2N7002LT1G)\n",
                   encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root), "--confirmed-column", "Custom 3"]) == 0
    out = capsys.readouterr().out
    assert "Value_Confirmed from Custom 3" in out and "not confirmed" not in out and "change in the" not in out
    assert main(["check", str(bom), "-p", str(project.root), "--confirmed-column", "Custom 9"]) == 2
    assert "no column 'Custom 9' for --confirmed-column" in capsys.readouterr().err
    for column in ("Value", "MPN", "Designator"):
        with pytest.raises(LensError, match="is the column of"):
            prj.bom_parts(str(bom), confirmed_column=column)
    roles = tmp_path / "roles.csv"
    roles.write_text("Designator,Value,MPN,Datasheet,DNP\nQ1,2N7002,2N7002LT1G,~,\n", encoding="utf-8")
    for column, role in (("Datasheet", "Datasheet"), ("DNP", "DNP")):
        with pytest.raises(LensError, match=f"is the column of {role}:"):
            prj.bom_parts(str(roles), confirmed_column=column)
    # The column option with the default column of Value_Confirmed.
    kicad = tmp_path / "kicad.csv"
    kicad.write_text("Reference,Value,MPN,Value_Confirmed\nQ1,2N7002,2N7002LT1G,2N7002\n", encoding="utf-8")
    with pytest.raises(LensError, match="is the column of the field Value_Confirmed: give the column of the MPN"):
        prj.bom_parts(str(kicad), column="Value_Confirmed")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    assert "or use an unused custom column of the BOM and give its name with --confirmed-column" in \
        capsys.readouterr().out


def test_write_field_names(store_home, tmp_path):
    """A confirmation writes into each column of the MPN of the line and into the column of
    Value_Confirmed, also a column of an other name. With no MPN in the line and more names
    of MPN columns, the field is not known: the message names the choices (the same rules
    as check_symbol_fields of kicad-workbench)."""
    bom = tmp_path / "altium.csv"
    bom.write_text("Designator,Value,Manufacturer Part Number,Custom 3\n"
                   "U1,LM358,LM358DR,\nU2,LM358 (LM358DT),,\n", encoding="utf-8")
    symbols = prj.bom_parts(str(bom), confirmed_column="Custom 3").cad
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        result = prj.check_parts(project, store, symbols)
    out = "\n".join(prj.format_check(result))
    assert ("Write Value = 'LM358 (LM358DR)', Manufacturer Part Number = 'LM358DR' and Custom 3 = "
            "'LM358 (LM358DR)' into the symbol.") in out
    assert "Write Manufacturer Part Number = 'LM358DT' and Custom 3 = 'LM358 (LM358DT)'" in out
    # Eagle: each library has its own attribute. Two filled columns: the write goes into the
    # two. No MPN in the line: the user selects the attribute of the symbol.
    bom.write_text("Parts,Value,MP,MANUFACTURER_PART_NUMBER\nU1,X,A,A\nU2,X (B),,\nR1,10k,,\n",
                   encoding="utf-8")
    u1, u2, r1 = prj.bom_parts(str(bom)).cad
    assert (u1.mpn_fields, u1.mpn_known) == (["MANUFACTURER_PART_NUMBER", "MP"], True)
    assert (u2.mpn_fields, u2.mpn_known) == (["MANUFACTURER_PART_NUMBER", "MP"], False)
    assert (u1.confirmed_fields, u1.confirmed_known) == (["Value_Confirmed"], True)
    with Store() as store:
        out = "\n".join(prj.format_check(prj.check_parts(project, store, [u1, u2, r1])))
    assert "Write Value = 'X (A)', MANUFACTURER_PART_NUMBER = 'A' and MP = 'A' into the symbol." in out
    assert ("Write the MPN field = 'B' (the BOM has the columns MANUFACTURER_PART_NUMBER, MP: ask the user "
            "which one the symbol uses) into the symbol.") in out
    assert "into the MPN field of the symbol, and the new Value into its field Value_Confirmed" in out
    # The parts of the parameter parts: the names of the CAD tool, else the default names.
    with Store() as store:
        out = "\n".join(prj.format_check(prj.check_parts(project, store, [
            {"reference": "U1", "value": "LM358", "mpn": "LM358DR", "value_confirmed": "",
             "mpn_fields": [" Manufacturer Part Number ", " "], "confirmed_field": "VC"},
            {"reference": "U2", "value": "LM358 (LM358DT)", "value_confirmed": ""}])))
    assert "Manufacturer Part Number = 'LM358DR' and VC = 'LM358 (LM358DR)'" in out
    assert "Write MPN = 'LM358DT' and Value_Confirmed = 'LM358 (LM358DT)'" in out


def test_write_fields_of_the_roles(tmp_path):
    """The rules of _write_fields: a "no part" mark, no MPN column, two names of
    Value_Confirmed, and a line of more symbols."""
    rows = "Reference,Value,MP,Value_Confirmed,Value Confirmed\nTP1,TP,-,,\nU1,X (A),,,\nR1 R2,10k,A,,\n"

    def read(text):
        bom = tmp_path / "bom.csv"
        bom.write_text(text, encoding="utf-8")
        return prj.bom_parts(str(bom)).cad

    tp1, u1, r12 = read(rows)
    assert (tp1.mpn_fields, tp1.mpn_known) == (["MP"], True)  # the column of the mark
    assert (u1.confirmed_fields, u1.confirmed_known) == (["Value_Confirmed", "Value Confirmed"], False)
    assert (r12.mpn_fields, r12.mpn_known) == (["MP"], True)  # one name in a line of more symbols
    (u1,) = read("Reference,Value,Manufacturer Part Number\nU1,X (A),\n")
    assert (u1.mpn_fields, u1.mpn_known) == (["Manufacturer Part Number"], True)
    (u1,) = read("Reference,Value,Value_Confirmed\nU1,X (A),\n")  # no MPN column: the default name
    assert (u1.mpn_fields, u1.mpn_known) == (["MPN"], True)
    # Two names in a line of more symbols: the names can come from different symbols.
    (r12,) = read("Parts,Value,MP,MANUFACTURER_PART_NUMBER\nR1 R2,10k,A,A\n")
    assert (r12.mpn_fields, r12.mpn_known) == (["MANUFACTURER_PART_NUMBER", "MP"], False)


def test_write_targets_of_a_variant(store_home, tmp_path):
    """A variant line that writes into other fields than the default line gives its own
    message."""
    with Store() as store:
        result = prj.check_parts(prj.init_project(tmp_path / "board"), store, [
            prj.CadSymbol(reference="U1", value="LM358", mpn="LM358DR", value_confirmed=""),
            prj.CadSymbol(reference="U1", value="LM358", mpn="LM358DR", value_confirmed="", variant="b",
                          mpn_fields=["Manufacturer Part Number"])])
    assert [c["mpn_fields"] for c in result["complete"]] == [["MPN"], ["Manufacturer Part Number"]]


def test_bom_formats(tmp_path):
    """The reader reads CSV (RFC 4180). It finds the delimiter in the header line
    only when one delimiter splits it; the caller gives an other delimiter and the
    quote of the presets Semicolons and TSV of KiCad (BOM_FMT_PRESET)."""
    from datasheet_lens.errors import LensError
    from datasheet_lens.project import bom_parts, symbol_state

    def mpns(name, text, delimiter="", quote=""):
        bom = tmp_path / name
        bom.write_text(text, encoding="utf-8")
        return [parts for p in bom_parts(str(bom), "", delimiter, quote).cad if (parts := symbol_state(p).parts)]

    def error(name, text, delimiter="", quote=""):
        with pytest.raises(LensError) as e:
            mpns(name, text, delimiter, quote)
        return str(e.value).split(": ", 1)[1]

    filler = "".join(f'"R{i}","10k",""\n' for i in range(300))  # more than 4096 characters
    assert mpns("csv.csv", '"Reference","Value","MPN"\n' + filler + '"U1","ICL7660","ICL7660""CPA"\n') == [
        ['ICL7660"CPA']]
    # Excel: quotes only where necessary, a value over two lines, apostrophes, and
    # no empty values at the end of a line.
    # Each text of a field MPN is an MPN, also "A\nB" and "'t Hooft" (the user writes the field).
    assert mpns("excel.csv", '"Qty, pcs",Reference,MPN,Note\n1,C1,"GRM188, XQ1001",n\n2,C2,"A\nB"\n3,U1,XQ1001\n'
                "4,U2,'t Hooft,a'\n") == [["GRM188", "XQ1001"], ["A\nB"], ["XQ1001"], ["'t Hooft"]]
    assert mpns("names.csv", 'Reference;"Value, a, b, c";MPN\nR1;10k;XQ1001\n') == [["XQ1001"]]
    assert mpns("trail.csv", "\nReference,Value,MPN,\nR1,10k,XQ1001\nR2,1k,YQ2002,\n") == [["XQ1001"], ["YQ2002"]]
    assert mpns("one.csv", "MPN\nGRM188, XQ1001\n") == [["GRM188", "XQ1001"]]
    # The presets of KiCad: Semicolons ("'") and TSV (no quotes).
    semi = "'Reference';'Value';'MPN';\n'U1';'10k; 1%';'J''XQ1001';\n"
    assert mpns("semi.csv", semi, quote="'") == [["J'XQ1001"]]
    assert "each name of the header is between ': give the quote '" in error("semi2.csv", semi)
    tsv = 'Reference\tValue\tMPN\nU1\t5" LCD\tXR1001\nU2\t"Smart" LED\tXR2002\n'
    assert mpns("kicad.tsv", tsv, quote="none") == [["XR1001"], ["XR2002"]]
    # A quote inside a value with no quotes does not follow RFC 4180.
    assert "line 2 is not valid CSV: a quote that is not around a full value" in error("kicad2.tsv", tsv)
    # A delimiter that the reader does not find, or more delimiters in the header.
    assert mpns("pipe.csv", '"Reference":"Value":"MPN"\n"U1":"a:b":"XQ1001"\n', delimiter=":") == [["XQ1001"]]
    assert "give the delimiter" in error("tie.csv", "Description, long, text;MPN\nA;XQ1001\n")
    assert mpns("tie2.csv", "Description, long, text;MPN\nA;XQ1001\n", delimiter=";") == [["XQ1001"]]
    assert error("extra.csv", 'Reference,Value,MPN\nC1,"10uF, 25V",G\nC2,1,G,extra\n').startswith(
        "line 3 has more values than the header line")
    assert error("broken.csv", 'Reference,Value,MPN\nR1,"5"" LCD",LCD-1\nR2,"bad"x,YQ2002\n').startswith(
        "line 3 is not valid CSV")
    assert error("open.csv", '"Reference","MPN"\n"U1","abc\n"U2","XQ1001"\n').startswith("line 2 is not valid CSV")
    assert error("none.csv", "").startswith("the BOM file has no header line")
    # A doubled quote in a name of the header (RFC 4180), and one column with a
    # delimiter at the end of the lines.
    assert mpns("names2.csv", '"Size ""A""",MPN\n1,XQ1001\n') == [["XQ1001"]]
    assert mpns("names3.csv", "'Manufacturer''s PN';'MPN'\n'a';'XQ1001'\n", quote="'") == [["XQ1001"]]
    assert mpns("col.csv", "MPN,\nXQ1001,\n") == [["XQ1001"]]
    assert mpns("col2.csv", "'MPN';\n'XQ1001';\n", quote="'") == [["XQ1001"]]
    assert "with ',': a quote that is not around a full value" in error(
        "space.csv", 'Reference, "Value", MPN\nU1,A,XQ1001\n')
    assert error("data.csv", 'Ref,Value,MPN\nR1,10k "x",YQ2002\n').startswith(
        "line 2 is not valid CSV: a quote that is not around a full value")
    # Excel writes "sep=;" for the delimiter. An unnamed last column holds values.
    assert mpns("sep.csv", "sep=;\nRef;Value;MPN\nU1;A, B;XQ1001\n") == [["XQ1001"]]
    assert mpns("unnamed.csv", "Ref,MPN,\nR1,XQ1001,note\n") == [["XQ1001"]]
    # Two columns of one name (fields of symbols from different libraries) are two
    # MPN fields: the first filled one gives the MPN; a different MPN in an other
    # one is a conflict, with the name of its column, and a part too (no MPN is lost).
    bom = tmp_path / "dupes.csv"
    bom.write_text("Ref,MPN,mpn,MP\nU1,XQ1001,,\nU2,,YQ2002,\nU3,ZQ3003,WQ4004,\nU4,AQ5005,,BQ6006\n", encoding="utf-8")
    parts = bom_parts(str(bom)).cad
    assert [symbol_state(p).parts for p in parts] == [["XQ1001"], ["YQ2002"], ["ZQ3003", "WQ4004"],
                                                       ["AQ5005", "BQ6006"]]
    assert ["; ".join(symbol_state(p).conflicts) for p in parts] == [
        "", "", "MPN ZQ3003, mpn WQ4004", "MPN AQ5005, MP BQ6006"]
    # More columns of one role: the first filled one, and a conflict when they are
    # different.
    def parts_of(name, text):
        bom = tmp_path / name
        bom.write_text(text, encoding="utf-8")
        states = [symbol_state(p) for p in bom_parts(str(bom)).cad]
        return [(s.parts, "; ".join(s.conflicts)) for s in states if s.parts or s.conflicts]

    assert parts_of("values.csv", "Ref,Value,value\nU1,(GRM188R61A106KE69D),\nU2,(TPS7A02),(XQ1001)\n") == [
        (["GRM188R61A106KE69D"], ""), (["TPS7A02", "XQ1001"], "Value (TPS7A02), value (XQ1001)")]
    # LibreOffice saved a "sep=;" file again with commas; quoted names with spaces;
    # one column with a delimiter in the data; a UTF-8 byte order mark in a cp1252 file.
    assert mpns("sep10.csv", '"sep=;",,\nRef,MPN,Value\nU1,XQ1001,YQ2002\n') == [["XQ1001"]]
    # The quote none is an option of the caller: the names keep their quotes.
    assert 'no MPN column' in error("qs.tsv", '"Ref" \t"MPN"\nU1\tXQ1001\n', quote="none")
    assert "no delimiter splits the header line, thus the reader uses one column" in error(
        "onecol.csv", 'MPN\n"XQ1001",YQ2002\n')
    bom = tmp_path / "bom1252.csv"
    bom.write_bytes(b'\xef\xbb\xbf"Reference","MPN"\n"U1","XQ1001 \xb5"\n')
    assert [symbol_state(p).parts for p in bom_parts(str(bom)).cad] == [["XQ1001 µ"]]
    assert "no delimiter of , ; tab | splits the header line" in error("colon.csv", "Reference:MPN\nU1:XQ1001\n")
    assert mpns("sep4.csv", "SEP=;;;\nRef;MPN;;\nU1;XQ1001;;\n") == [["XQ1001"]]
    assert mpns("sep5.csv", '"sep=;"\nRef;MPN\nU1;XQ1001\n') == [["XQ1001"]]
    assert mpns("sep7.tsv", "sep=\t\nRef\tMPN\nU1\tXQ1001\n") == [["XQ1001"]]
    assert mpns("sep8.csv", "sep=\t\t\t\nRef\tMPN\nU1\tXQ1001\n") == [["XQ1001"]]
    assert mpns("sep9.csv", "'sep=;'\nRef;MPN\nU1;XQ1001\n", quote="'") == [["XQ1001"]]
    # One column with the name of the column option; a given delimiter that does
    # not split the header; CR and CRLF in a value and as line ends.
    bom = tmp_path / "partno.csv"
    bom.write_text("PartNo\nXR1001\nXR2002\n", encoding="utf-8")
    assert [symbol_state(p).parts for p in bom_parts(str(bom), "PartNo").cad] == [["XR1001"], ["XR2002"]]
    assert "the delimiter ',' does not split the header line" in error(
        "given.csv", "Reference;Value;MPN\nU1;A;XQ1001\n", delimiter=",")
    bom = tmp_path / "cr.csv"
    bom.write_bytes(b'Reference,Description,MPN\r"U1","A\rB",XQ1001\r\n"U2","C\r\nD",YQ2002\rU3,E,ZQ3003')
    parts = bom_parts(str(bom))
    assert [symbol_state(p).parts for p in parts.cad] == [["XQ1001"], ["YQ2002"], ["ZQ3003"]]
    from datasheet_lens.project import _read_bom
    assert [row[1] for row in _read_bom(str(bom))[1]] == ["A\rB", "C\r\nD", "E"]
    assert "(none for a file with no quotes)" in error("inch.csv", 'Ref,Size 5",MPN\nU1,A,XQ1001\n')
    bom = tmp_path / "spaces3.csv"
    bom.write_text("Reference, Value, MPN\nU1, A, XQ1001\n", encoding="utf-8")
    assert "MPN from MPN; Value from Value; references from Reference;" in bom_parts(str(bom)).summary
    assert '"Ref", "MPN"' in error("none2.csv", '"Ref","MPN"\n"U1","XQ1001"\n', quote="none")
    assert "give the delimiter" in error("desc.csv", "Desc, long;\nA;\n")
    # A name over two lines, a partly quoted header, two names of one column, and
    # the Excel line "sep=".
    assert mpns("two.csv", '"Ref\r\nerence",MPN\r\nU1,XQ1001\r\n') == [["XQ1001"]]
    # One name between "'" is a valid name of CSV: the reader keeps it.
    bom = tmp_path / "part.csv"
    bom.write_text("Ref,'Note',MPN\nU1,a,XQ1001\n", encoding="utf-8")
    assert [symbol_state(p).parts for p in bom_parts(str(bom)).cad] == [["XQ1001"]]
    # Synonyms of one role, a header with no names, a quote that is a delimiter of
    # the list, and the line "sep=" with spaces before it or a letter.
    # A line is not fitted only when each DNP column says so; a conflict is fitted.
    assert parts_of("syn.csv", "Ref,MPN,DNP,Do not populate\nU1,XQ1001,,1\nU2,YQ2002,DNP,1\n") == [
        (["XQ1001"], "DNP (empty), Do not populate 1")]
    assert parts_of("syn2.csv", "Reference,Designator,MPN\nU1,U1,XQ1001\nU2,R2,YQ2002\n") == [
        (["XQ1001"], ""), (["YQ2002"], "Reference U2, Designator R2")]
    # A line with only delimiters is an empty line, also before the header.
    assert mpns("empty2.csv", ",,\nRef,MPN\nU1,XQ1001\n") == [["XQ1001"]]
    # "~" in a DNP column is empty (fitted); a broken line "sep="; an explicit quote
    # '"' with names between "'"; a conflict on a line with no MPN; values of one
    # part in two columns.
    assert mpns("tdnp.csv", "Reference,MPN,DNP\nR1,TLV75533PDBVR,~\n") == [["TLV75533PDBVR"]]
    assert "the line 'sep=' of the file does not give one delimiter" in error("sepx.csv", "sep=;,;\nRef;MPN\n")
    bom = tmp_path / "apos.csv"
    bom.write_text("'Reference','MPN'\nU1,XQ1001\n", encoding="utf-8")
    assert "no MPN column" in str(pytest.raises(LensError, bom_parts, str(bom), "", "", '"').value)
    assert parts_of("passive.csv", "Reference,Value,value\nC1,100nF,10nF\n") == [([], "Value 100nF, value 10nF")]
    # Each MPN of each Value column counts; the values of all pairs are compared;
    # "~" in an MPN field is empty; a line of delimiters before "sep="; an overlap
    # after a value between quotes.
    # A second Value column with an other code of one part: a conflict, and a part too.
    assert parts_of("v2.csv", "Ref,Value,value\nU1,(TLV755P),(TLV75533PDBVR)\n") == [
        (["TLV755P", "TLV75533PDBVR"], "Value (TLV755P), value (TLV75533PDBVR)")]
    assert parts_of("v3.csv", "Ref,Value,value,VALUE\nU1,(TLV755P),(TLV75533PDBVR),(TLV75518PDBVR)\n")[0][1] == (
        "Value (TLV755P), value (TLV75533PDBVR), VALUE (TLV75518PDBVR)")
    assert parts_of("tm.csv", "Ref;Value;MPN\nR2;10k;~\nU1;OPA2134 (OPA2134);~\n") == [(["OPA2134"], "")]
    assert mpns("sepd.csv", ",,,\nsep=;\nRef;MPN\nU1;XQ1001\n") == [["XQ1001"]]
    assert "can start at more than one place" in error("ovq.csv", 'Ref||MPN\n"a"|||b\n', delimiter="||")
    # The same references in two columns are no conflict; two Value columns with
    # different texts are one; MPN and Value are a change, not a conflict.
    assert parts_of("same.csv", "Reference,Designator,MPN,Value,value\nR1 R2,\"R1, R2\",XQ1001,(TLV75533PDBVR),"
                    "(tlv75533)\n") == [(["XQ1001", "tlv75533"], "Value (TLV75533PDBVR), value (tlv75533)")]
    assert mpns("pipeq.csv", "|Ref|,|MPN|\n|U1|,|XQ1001|\n", quote="|") == [["XQ1001"]]
    assert mpns("sep11.csv", "  sep=;\nRef;MPN\nU1;XQ1001\n") == [["XQ1001"]]
    assert "the line 'sep=' of the file does not give one delimiter" in error("sep12.csv", "sep=a\nRefaMPN\n")
    assert "the line 'sep=' of the file gives the delimiter '\"', which is the quote" in error(
        "sep2.csv", 'sep="\nA"B\n')
    # A preset of KiCad with a delimiter and a quote of more characters.
    assert mpns("preset.csv", "`Ref`||`Value`||`MPN`\n`U1`||`a||``b`||`XQ1001`\n", delimiter="||",
                quote="`") == [["XQ1001"]]
    # One column with a name that is not known, and a name between "'".
    assert "give its name with the column option" in error("partno2.csv", "PartNo\nXQ1001\n")
    assert "each name of the header is between ': give the quote '" in error("q1.csv", "'MPN'\n'XQ1001'\n")
    assert parts_of("dnp2.csv", "Ref,MPN,DNP,dnp\nU1,XQ1001,0,DNP\n") == [(["XQ1001"], "DNP 0, dnp DNP")]
    bom = tmp_path / "dupes2.csv"
    bom.write_text("Ref,MPN,MPN\nU1,XQ1001,YQ2002\n", encoding="utf-8")
    assert "MPN from MPN (column 2), MPN (column 3)" in bom_parts(str(bom)).summary
    assert parts_of("dupes3.csv", "Ref,MPN,MPN\nU1,XQ1001,YQ2002\n")[0][1].startswith(
        "MPN XQ1001, MPN (column 3) YQ2002")
    assert "the delimiter can start at more than one place ('||'): put the value between quotes" in error(
        "overlap.csv", "Ref||MPN\nU1|||XQ1001\n", delimiter="||")
    # Empty values with a delimiter of more characters, two Value columns with the
    # MPN in the second one, and "~" (the empty Datasheet of KiCad).
    assert mpns("empty.csv", "Ref||Value||MPN\nU1||||XQ1001\n", delimiter="||") == [["XQ1001"]]
    assert mpns("empty2.csv", "Ref, Value, MPN\nU1, , XQ1001\n", delimiter=", ") == [["XQ1001"]]
    assert parts_of("values2.csv", "Ref,Value,value\nU1,10k,10k (GRM188R61A106KE69D)\n") == [
        (["GRM188R61A106KE69D"], "Value 10k, value 10k (GRM188R61A106KE69D)")]  # no MPN is lost
    bom = tmp_path / "tilde.csv"
    bom.write_text("Ref,MPN,Datasheet,Datasheet URL\nU1,XQ1001,~,http://a\n", encoding="utf-8")
    assert [(p.datasheet, p.conflict) for p in bom_parts(str(bom)).cad] == [("http://a", "")]
    assert mpns("sepblank.csv", "sep=;\n\nRef;MPN\nU1;XQ1001\n") == [["XQ1001"]]
    assert "if the file uses the quote ', give it" in error(
        "semicut.csv", "'Ref';'Value; tol';'MPN'\n'U1';'a';'XQ1001'\n")
    assert "but the delimiter option gives ','" in error("sep3.csv", "sep=;\nRef;MPN\nU1;XQ1001\n", delimiter=",")
    assert error("cr.csv", 'Ref,MPN\rU1,"bad"x\r').startswith("line 2 is not valid CSV")


def test_bom_format_options():
    """The options of the interfaces: tab, none, and the errors with the name of
    the option."""
    from datasheet_lens.errors import LensError
    from datasheet_lens.project import bom_format

    options = {"delimiter": "bom_delimiter", "quote": "bom_quote"}
    assert bom_format("tab", "none", options) == ("\t", "")
    assert bom_format("", '"', options) == ("", '"')
    assert bom_format("", "", options) == ("", '"')  # an empty option is the default
    # A preset of KiCad can have a delimiter and a quote of more characters.
    assert bom_format(", ", "`", options) == (", ", "`")
    with pytest.raises(LensError, match="is not a delimiter for bom_delimiter: a tab is 'tab'"):
        bom_format("\\t", '"', options)
    # Only the quote of the file cannot be the delimiter.
    assert bom_format("'", "none", options) == ("'", "")
    assert bom_format('"', "'", options) == ('"', "'")
    with pytest.raises(LensError, match="must be different"):
        bom_format('"', '"', options)
    with pytest.raises(LensError, match="cannot have a line end"):
        bom_format("\n", '"', options)


def test_bom_size_limit(tmp_path, monkeypatch):
    """A file larger than BOM_MAX_BYTES is not a BOM."""
    from datasheet_lens import project as prj
    from datasheet_lens.errors import LensError

    monkeypatch.setattr(prj, "BOM_MAX_BYTES", 20)
    bom = tmp_path / "big.csv"
    bom.write_text("Reference,MPN\n" + "U1,XQ1001\n" * 3, encoding="utf-8")
    with pytest.raises(LensError, match="larger than"):
        prj.bom_parts(str(bom))
    bom.write_text("MPN\nXQ1001\n", encoding="utf-8")
    assert [prj.symbol_state(p).parts for p in prj.bom_parts(str(bom)).cad] == [["XQ1001"]]


def test_bom_conflict_rules(tmp_path):
    """References as a set (a range of KiCad), "~" in the parts of project_check,
    and a long run of a delimiter of more characters (one check for each run)."""
    import time

    from datasheet_lens.project import _records, _references

    assert _references("R1-R3") == _references("R1,R2,R3") == _references("r1 r2 R3")
    assert _references("R1-R3") != _references("R1, R3")
    start = time.monotonic()
    assert len(list(_records("Ref||MPN\nU1||X1" + "||" * 8000 + "\n", "||", '"'))[1][1]) == 8002
    assert time.monotonic() - start < 1
