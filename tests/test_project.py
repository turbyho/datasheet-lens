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
        assert any("proposal: RV-3149-C3" in l and f"add RV-3149-C3 {sha}" in l and f"add <MPN> -b RV-3149-C3 {sha}" in l
                   for l in lines), lines

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
    with Store() as store:
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

    bom = tmp_path / "bom.csv"
    bom.write_text('"Reference","Value","Datasheet","DNP"\n"U1","OPA2134","~",""\n"C1","100nF","~",""\n'
                   '"C2","4.7uF/10V","~",""\n"J2","Conn_01x05_Socket","~",""\n"U7","LM358","~",""\n'
                   '"U8","LM3580DR/NOPB","~",""\n"U9","XYZ123","~","DNP"\n', encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "MPN from Value; references from Reference; 3 lines with an MPN" in out and "the same in the CAD project and in the project file: 2" in out
    assert "extra: NRF9151-LACA-R7" in out and "missing: LM3580DR/NOPB (U8)" in out
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
    assert "MPN from Manufacturer Part Number 1; references from Designator; read as cp1252" in out
    assert "the same in the CAD project and in the project file: 1" in out
    bom = tmp_path / "eagle.csv"
    # Each library puts the MPN into a different attribute: the first filled one of the row.
    bom.write_text("Qty;Value;Parts;MP;MANUFACTURER_PART_NUMBER\n2;10k;R1 R2;;\n1;OPA2134;U1;OPA2134PA;\n"
                   "1;X;U2;;LM358DR/NOPB\n1;1N4148W;D3;;\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "MPN from MANUFACTURER_PART_NUMBER, MP, Value; references from Parts; 3 lines with an MPN" in out
    assert "missing: LM358DR/NOPB (U2)" in out and "missing: 1N4148W (D3)" in out


def test_mpn_in_the_value_field():
    """A Value field can hold a value, an MPN, a value with an MPN, or more MPNs
    (alternatives)."""
    from datasheet_lens.project import cad_mpns, mpns_in_value

    assert mpns_in_value("100nF GRM155R71C104KA88D") == ["GRM155R71C104KA88D"]
    assert mpns_in_value("10uF/10V (GRM188R61A106KE69D)") == ["GRM188R61A106KE69D"]
    assert mpns_in_value("390uF (EEU-FS1H391L)") == ["EEU-FS1H391L"]
    # Alternatives with any separator; a slash with no spaces only between two full MPNs.
    for value in ("AP2205-33YR-13 / AP7370-33Y-13", "AP2205-33YR-13, AP7370-33Y-13", "AP2205-33YR-13;AP7370-33Y-13",
                  "AP2205-33YR-13 | AP7370-33Y-13", "AP2205-33YR-13/AP7370-33Y-13", "AP2205-33YR-13 or AP7370-33Y-13"):
        assert mpns_in_value(value) == ["AP2205-33YR-13", "AP7370-33Y-13"], value
    assert mpns_in_value("MCP1827S-3302E/EB") == ["MCP1827S-3302E/EB"]
    assert mpns_in_value("MAX20361BEWC+ (U5)") == ["MAX20361BEWC+"]
    assert mpns_in_value("LM358DR/NOPB") == ["LM358DR/NOPB"] and mpns_in_value("nRF9151-LAXX") == ["nRF9151-LAXX"]
    for value in ("100nF", "4.7uF/10V", ">=120R", "10nH RF LQ", "Conn_01x05_Socket", "Battery LiPo - 1 cell",
                  "47uF/poly", "120R/0.7A", "10k"):
        assert mpns_in_value(value) == [], value
    # An MPN field and alternatives in the Value; a Value of a different part.
    assert cad_mpns([("MPN", "AP2205-33YR-13")], "AP2205-33YR-13 / AP7370-33Y-13") == (
        ["AP2205-33YR-13", "AP7370-33Y-13"], "")
    assert cad_mpns([("MP", "BSS138")], "2N7002") == (["BSS138"], "MP BSS138, Value 2N7002. The check uses "
                                                         "BSS138")
    assert cad_mpns([("MPN", "")], "AP2205-33YR-13 / AP7370-33Y-13")[0] == ["AP2205-33YR-13", "AP7370-33Y-13"]


def test_check_tells_about_two_parts_in_one_symbol(store_home, tmp_path, capsys):
    """An MPN field and a Value that name different parts are a conflict; the
    same part with other signs (#) or a longer code is not."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0218PYCHR")
    bom = tmp_path / "bom.csv"
    bom.write_text("Reference,Value,MPN\nU2,TPS7A0218PYCHR,TPS7A0233PYCHR\nU4,LTC3588EMSEPBF,LTC3588EMSE#PBF\n"
                   "U5,TMP112D0,TMP112D0IDPWR\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert ("conflict in the CAD project: U2: MPN TPS7A0233PYCHR, Value TPS7A0218PYCHR. The check uses "
            "TPS7A0233PYCHR") in out
    assert out.count("conflict in the CAD project") == 1


def test_mpn_fields_review_cases(store_home, tmp_path, capsys):
    """A base part in the Value is no conflict; words of one part give one MPN;
    an ordering suffix and a generic symbol name are no alternative; a column
    Part Number is not an MPN; a UTF-16 file; --column still reads the Value."""
    from datasheet_lens.project import cad_mpns, mpns_in_value

    assert cad_mpns([("MPN", "TLV75533PDBVR")], "TLV755P") == (["TLV75533PDBVR"], "")
    assert cad_mpns([("MPN", "TPS7A0233PYCHR")], "TPS7A02") == (["TPS7A0233PYCHR"], "")
    assert cad_mpns([("MPN", "")], "OPA2134 (OPA2134PA)") == (["OPA2134PA"], "")
    assert cad_mpns([("MPN", "ESP32-S3-WROOM-1-N16R8")], "ESP32-S3-WROOM-1 N16R8") == (
        ["ESP32-S3-WROOM-1-N16R8"], "")
    assert mpns_in_value("USB_C_Receptacle_GCT_USB4105") == []
    assert cad_mpns([("MP", "AP2205-18YR-13")], "AP2205-33YR-13 / AP7370-33Y-13")[1].startswith("MP AP2205-18YR-13")

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134PA")
    bom = tmp_path / "bom.csv"
    bom.write_text("Reference\tValue\tPart Number\tMPN\nU1\tOPA2134PA / OPA1612AIDR\tPN-00123\tOPA2134PA\n",
                   encoding="utf-16")
    assert main(["check", str(bom), "-p", str(project.root), "--column", "MPN"]) == 0
    out = capsys.readouterr().out
    assert "MPN from MPN, Value" in out and "conflict" not in out and "PN-00123" not in out
    assert "missing: OPA1612AIDR (U1) [alternative of OPA2134PA]" in out


def test_bom_formats(tmp_path):
    """The reader reads CSV (RFC 4180). It finds the delimiter in the header line
    only when one delimiter splits it; the caller gives an other delimiter and the
    quote of the presets Semicolons and TSV of KiCad (BOM_FMT_PRESET)."""
    from datasheet_lens.errors import LensError
    from datasheet_lens.project import bom_parts

    def mpns(name, text, delimiter="", quote=""):
        bom = tmp_path / name
        bom.write_text(text, encoding="utf-8")
        return [p["mpns"] for p in bom_parts(str(bom), "", delimiter, quote).cad]

    def error(name, text, delimiter="", quote=""):
        with pytest.raises(LensError) as e:
            mpns(name, text, delimiter, quote)
        return str(e.value).split(": ", 1)[1]

    filler = "".join(f'"R{i}","10k",""\n' for i in range(300))  # more than 4096 characters
    assert mpns("csv.csv", '"Reference","Value","MPN"\n' + filler + '"U1","ICL7660","ICL7660""CPA"\n') == [
        ['ICL7660"CPA']]
    # Excel: quotes only where necessary, a value over two lines, apostrophes, and
    # no empty values at the end of a line.
    assert mpns("excel.csv", '"Qty, pcs",Reference,MPN,Note\n1,C1,"GRM188, X9",n\n2,C2,"A\nB"\n3,U1,X9\n'
                "4,U2,'t Hooft,a'\n") == [["GRM188, X9"], ["A\nB"], ["X9"], ["'t Hooft"]]
    assert mpns("names.csv", 'Reference;"Value, a, b, c";MPN\nR1;10k;X9\n') == [["X9"]]
    assert mpns("trail.csv", "\nReference,Value,MPN,\nR1,10k,X9\nR2,1k,Y9,\n") == [["X9"], ["Y9"]]
    assert mpns("one.csv", "MPN\nGRM188, X9\n") == [["GRM188, X9"]]
    # The presets of KiCad: Semicolons ("'") and TSV (no quotes).
    semi = "'Reference';'Value';'MPN';\n'U1';'10k; 1%';'J''X9';\n"
    assert mpns("semi.csv", semi, quote="'") == [["J'X9"]]
    assert "each name of the header is between ': give the quote '" in error("semi2.csv", semi)
    tsv = 'Reference\tValue\tMPN\nU1\t5" LCD\tX1\nU2\t"Smart" LED\tX2\n'
    assert mpns("kicad.tsv", tsv, quote="none") == [["X1"], ["X2"]]
    # A quote inside a value with no quotes does not follow RFC 4180.
    assert "line 2 is not valid CSV: a quote that is not around a full value" in error("kicad2.tsv", tsv)
    # A delimiter that the reader does not find, or more delimiters in the header.
    assert mpns("pipe.csv", '"Reference":"Value":"MPN"\n"U1":"a:b":"X9"\n', delimiter=":") == [["X9"]]
    assert "give the delimiter" in error("tie.csv", "Description, long, text;MPN\nA;X9\n")
    assert mpns("tie2.csv", "Description, long, text;MPN\nA;X9\n", delimiter=";") == [["X9"]]
    assert error("extra.csv", 'Reference,Value,MPN\nC1,"10uF, 25V",G\nC2,1,G,extra\n').startswith(
        "line 3 has more values than the header line")
    assert error("broken.csv", 'Reference,Value,MPN\nR1,"5"" LCD",LCD-1\nR2,"bad"x,Y9\n').startswith(
        "line 3 is not valid CSV")
    assert error("open.csv", '"Reference","MPN"\n"U1","abc\n"U2","X9"\n').startswith("line 2 is not valid CSV")
    assert error("none.csv", "").startswith("the BOM file has no header line")
    # A doubled quote in a name of the header (RFC 4180), and one column with a
    # delimiter at the end of the lines.
    assert mpns("names2.csv", '"Size ""A""",MPN\n1,X9\n') == [["X9"]]
    assert mpns("names3.csv", "'Manufacturer''s PN';'MPN'\n'a';'X9'\n", quote="'") == [["X9"]]
    assert mpns("col.csv", "MPN,\nX9,\n") == [["X9"]]
    assert mpns("col2.csv", "'MPN';\n'X9';\n", quote="'") == [["X9"]]
    assert "with ',': a quote that is not around a full value" in error(
        "space.csv", 'Reference, "Value", MPN\nU1,A,X9\n')
    assert error("data.csv", 'Ref,Value,MPN\nR1,10k "x",Y9\n').startswith(
        "line 2 is not valid CSV: a quote that is not around a full value")
    # Excel writes "sep=;" for the delimiter. An unnamed last column holds values.
    assert mpns("sep.csv", "sep=;\nRef;Value;MPN\nU1;A, B;X9\n") == [["X9"]]
    assert mpns("unnamed.csv", "Ref,MPN,\nR1,X9,note\n") == [["X9"]]
    # Two columns of one name (fields of symbols from different libraries) are two
    # MPN fields: the first filled one gives the MPN; a different MPN in an other
    # one is a conflict, with the name of its column.
    bom = tmp_path / "dupes.csv"
    bom.write_text("Ref,MPN,mpn,MP\nU1,X9,,\nU2,,Y9,\nU3,Z9,W9,\nU4,A1,,B1\n", encoding="utf-8")
    parts = bom_parts(str(bom)).cad
    assert [p["mpns"] for p in parts] == [["X9"], ["Y9"], ["Z9"], ["A1"]]
    assert [p["conflict"].split(".")[0] for p in parts] == ["", "", "MPN Z9, mpn W9", "MPN A1, MP B1"]
    # More columns of one role: the first filled one, and a conflict when they are
    # different.
    def parts_of(name, text):
        bom = tmp_path / name
        bom.write_text(text, encoding="utf-8")
        return [(p["mpns"], p["conflict"]) for p in bom_parts(str(bom)).cad]

    assert parts_of("values.csv", "Ref,Value,value\nU1,GRM188R61A106KE69D,\nU2,TPS7A02,X9\n") == [
        (["GRM188R61A106KE69D"], ""), (["TPS7A02"], "Value TPS7A02, value X9")]
    # LibreOffice saved a "sep=;" file again with commas; quoted names with spaces;
    # one column with a delimiter in the data; a UTF-8 byte order mark in a cp1252 file.
    assert mpns("sep10.csv", '"sep=;",,\nRef,MPN,Value\nU1,X9,Y9\n') == [["X9"]]
    # The quote none is an option of the caller: the names keep their quotes.
    assert 'no MPN column' in error("qs.tsv", '"Ref" \t"MPN"\nU1\tX9\n', quote="none")
    assert "no delimiter splits the header line, thus the reader uses one column" in error(
        "onecol.csv", 'MPN\n"X9",Y9\n')
    bom = tmp_path / "bom1252.csv"
    bom.write_bytes(b'\xef\xbb\xbf"Reference","MPN"\n"U1","X9 \xb5"\n')
    assert [p["mpns"] for p in bom_parts(str(bom)).cad] == [["X9 µ"]]
    assert "no delimiter of , ; tab | splits the header line" in error("colon.csv", "Reference:MPN\nU1:X9\n")
    assert mpns("sep4.csv", "SEP=;;;\nRef;MPN;;\nU1;X9;;\n") == [["X9"]]
    assert mpns("sep5.csv", '"sep=;"\nRef;MPN\nU1;X9\n') == [["X9"]]
    assert mpns("sep7.tsv", "sep=\t\nRef\tMPN\nU1\tX9\n") == [["X9"]]
    assert mpns("sep8.csv", "sep=\t\t\t\nRef\tMPN\nU1\tX9\n") == [["X9"]]
    assert mpns("sep9.csv", "'sep=;'\nRef;MPN\nU1;X9\n", quote="'") == [["X9"]]
    # One column with the name of the column option; a given delimiter that does
    # not split the header; CR and CRLF in a value and as line ends.
    bom = tmp_path / "partno.csv"
    bom.write_text("PartNo\nX1\nX2\n", encoding="utf-8")
    assert [p["mpns"] for p in bom_parts(str(bom), "PartNo").cad] == [["X1"], ["X2"]]
    assert "the delimiter ',' does not split the header line" in error(
        "given.csv", "Reference;Value;MPN\nU1;A;X9\n", delimiter=",")
    bom = tmp_path / "cr.csv"
    bom.write_bytes(b'Reference,Description,MPN\r"U1","A\rB",X9\r\n"U2","C\r\nD",Y9\rU3,E,Z9')
    parts = bom_parts(str(bom))
    assert [p["mpns"] for p in parts.cad] == [["X9"], ["Y9"], ["Z9"]]
    from datasheet_lens.project import _read_bom
    assert [row[1] for row in _read_bom(str(bom))[1]] == ["A\rB", "C\r\nD", "E"]
    assert "(none for a file with no quotes)" in error("inch.csv", 'Ref,Size 5",MPN\nU1,A,X9\n')
    bom = tmp_path / "spaces3.csv"
    bom.write_text("Reference, Value, MPN\nU1, A, X9\n", encoding="utf-8")
    assert "MPN from MPN, Value; references from Reference;" in bom_parts(str(bom)).summary
    assert '"Ref", "MPN"' in error("none2.csv", '"Ref","MPN"\n"U1","X9"\n', quote="none")
    assert "give the delimiter" in error("desc.csv", "Desc, long;\nA;\n")
    # A name over two lines, a partly quoted header, two names of one column, and
    # the Excel line "sep=".
    assert mpns("two.csv", '"Ref\r\nerence",MPN\r\nU1,X9\r\n') == [["X9"]]
    # One name between "'" is a valid name of CSV: the reader keeps it.
    bom = tmp_path / "part.csv"
    bom.write_text("Ref,'Note',MPN\nU1,a,X9\n", encoding="utf-8")
    assert [p["mpns"] for p in bom_parts(str(bom)).cad] == [["X9"]]
    # Synonyms of one role, a header with no names, a quote that is a delimiter of
    # the list, and the line "sep=" with spaces before it or a letter.
    # A line is not fitted only when each DNP column says so; a conflict is fitted.
    assert parts_of("syn.csv", "Ref,MPN,DNP,Do not populate\nU1,X9,,1\nU2,Y9,DNP,1\n") == [
        (["X9"], "DNP (empty), Do not populate 1")]
    assert parts_of("syn2.csv", "Reference,Designator,MPN\nU1,U1,X9\nU2,R2,Y9\n") == [
        (["X9"], ""), (["Y9"], "Reference U2, Designator R2")]
    # A line with only delimiters is an empty line, also before the header.
    assert mpns("empty2.csv", ",,\nRef,MPN\nU1,X9\n") == [["X9"]]
    # "~" in a DNP column is empty (fitted); a broken line "sep="; an explicit quote
    # '"' with names between "'"; a conflict on a line with no MPN; values of one
    # part in two columns.
    assert mpns("tdnp.csv", "Reference,MPN,DNP\nR1,TLV75533PDBVR,~\n") == [["TLV75533PDBVR"]]
    assert "the line 'sep=' of the file does not give one delimiter" in error("sepx.csv", "sep=;,;\nRef;MPN\n")
    bom = tmp_path / "apos.csv"
    bom.write_text("'Reference','MPN'\nU1,X9\n", encoding="utf-8")
    assert "no MPN column" in str(pytest.raises(LensError, bom_parts, str(bom), "", "", '"').value)
    assert parts_of("passive.csv", "Reference,Value,value\nC1,100nF,10nF\n") == [([], "Value 100nF, value 10nF")]
    # Each MPN of each Value column counts; the values of all pairs are compared;
    # "~" in an MPN field is empty; a line of delimiters before "sep="; an overlap
    # after a value between quotes.
    assert parts_of("v2.csv", "Ref,Value,value\nU1,TLV755P,TLV75533PDBVR\n") == [(["TLV75533PDBVR"], "")]
    assert parts_of("v3.csv", "Ref,Value,value,VALUE\nU1,TLV755P,TLV75533PDBVR,TLV75518PDBVR\n")[0][1] == (
        "Value TLV755P, value TLV75533PDBVR, VALUE TLV75518PDBVR")
    assert parts_of("tm.csv", "Ref;Value;MPN\nR2;10k;~\nU1;OPA2134;~\n") == [(["OPA2134"], "")]
    assert mpns("sepd.csv", ",,,\nsep=;\nRef;MPN\nU1;X9\n") == [["X9"]]
    assert "can start at more than one place" in error("ovq.csv", 'Ref||MPN\n"a"|||b\n', delimiter="||")
    assert parts_of("same.csv", "Reference,Designator,MPN,Value,value\nR1 R2,\"R1, R2\",X9,TLV75533PDBVR,"
                    "tlv75533\n") == [(["X9"], "MPN X9, Value TLV75533PDBVR. The check uses X9")]
    assert mpns("pipeq.csv", "|Ref|,|MPN|\n|U1|,|X9|\n", quote="|") == [["X9"]]
    assert mpns("sep11.csv", "  sep=;\nRef;MPN\nU1;X9\n") == [["X9"]]
    assert "the line 'sep=' of the file does not give one delimiter" in error("sep12.csv", "sep=a\nRefaMPN\n")
    assert "the line 'sep=' of the file gives the delimiter '\"', which is the quote" in error(
        "sep2.csv", 'sep="\nA"B\n')
    # A preset of KiCad with a delimiter and a quote of more characters.
    assert mpns("preset.csv", "`Ref`||`Value`||`MPN`\n`U1`||`a||``b`||`X9`\n", delimiter="||", quote="`") == [["X9"]]
    # One column with a name that is not known, and a name between "'".
    assert "give its name with the column option" in error("partno2.csv", "PartNo\nX9\n")
    assert "each name of the header is between ': give the quote '" in error("q1.csv", "'MPN'\n'X9'\n")
    assert parts_of("dnp2.csv", "Ref,MPN,DNP,dnp\nU1,X9,0,DNP\n") == [(["X9"], "DNP 0, dnp DNP")]
    bom = tmp_path / "dupes2.csv"
    bom.write_text("Ref,MPN,MPN\nU1,X9,Y9\n", encoding="utf-8")
    assert "MPN from MPN (column 2), MPN (column 3)" in bom_parts(str(bom)).summary
    assert parts_of("dupes3.csv", "Ref,MPN,MPN\nU1,X9,Y9\n")[0][1].startswith("MPN (column 2) X9, MPN (column 3) Y9")
    assert "the delimiter can start at more than one place ('||'): put the value between quotes" in error(
        "overlap.csv", "Ref||MPN\nU1|||X9\n", delimiter="||")
    # Empty values with a delimiter of more characters, two Value columns with the
    # MPN in the second one, and "~" (the empty Datasheet of KiCad).
    assert mpns("empty.csv", "Ref||Value||MPN\nU1||||X9\n", delimiter="||") == [["X9"]]
    assert mpns("empty2.csv", "Ref, Value, MPN\nU1, , X9\n", delimiter=", ") == [["X9"]]
    assert parts_of("values2.csv", "Ref,Value,value\nU1,10k,GRM188R61A106KE69D\n") == [
        (["GRM188R61A106KE69D"], "Value 10k, value GRM188R61A106KE69D")]
    bom = tmp_path / "tilde.csv"
    bom.write_text("Ref,MPN,Datasheet,Datasheet URL\nU1,X9,~,http://a\n", encoding="utf-8")
    assert [(p["datasheet"], p["conflict"]) for p in bom_parts(str(bom)).cad] == [("http://a", "")]
    assert mpns("sepblank.csv", "sep=;\n\nRef;MPN\nU1;X9\n") == [["X9"]]
    assert "if the file uses the quote ', give it" in error("semicut.csv", "'Ref';'Value; tol';'MPN'\n'U1';'a';'X9'\n")
    assert "but the delimiter option gives ','" in error("sep3.csv", "sep=;\nRef;MPN\nU1;X9\n", delimiter=",")
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
    bom.write_text("Reference,MPN\n" + "U1,X9\n" * 3, encoding="utf-8")
    with pytest.raises(LensError, match="larger than"):
        prj.bom_parts(str(bom))
    bom.write_text("MPN\nX9\n", encoding="utf-8")
    assert [p["mpns"] for p in prj.bom_parts(str(bom)).cad] == [["X9"]]


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
