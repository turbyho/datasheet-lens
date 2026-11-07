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
        # and the index comes after sync, as in sync_project (after the lock of the folder).
        doc_id = store.document(sha256_file(opa)).id
        with pytest.raises(LensError, match="has no index"):
            index_markdown(store, doc_id)
        report = prj.sync(project, store)
        prj.build_indexes(store, report)
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
        assert project.path.read_text(encoding="utf-8") == '# part B: important\n[[part]]\nmpn = "RV-3149-C3"\n'

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


def test_removed_tables_leave_one_gap(setup):
    """A removed part, document or purchase source takes one gap with it: the
    gap between two headers stays as it was, and the end of the file stays as
    it was."""
    project = setup[0]

    def part(mpn: str, *sources: str) -> str:
        text = f'[[part]]\nmpn = "{mpn}"\nmanufacturer = "M"\n'
        for number in sources:
            text += f'\n[[part.source]]\nsource = "jlcpcb"\nnumber = "{number}"\n'
        return text

    head = 'folder = "datasheets"\n\n'
    full = head + "\n".join([part("A", "C1"), part("B", "C2"), part("C"), part("D")])
    cases = [
        ({"B"}, head + "\n".join([part("A", "C1"), part("C"), part("D")])),
        ({"B", "C"}, head + "\n".join([part("A", "C1"), part("D")])),  # two removed parts in a row
        ({"D"}, head + "\n".join([part("A", "C1"), part("B", "C2"), part("C")])),  # the last part
        ({"A"}, head + "\n".join([part("B", "C2"), part("C"), part("D")])),  # the first part
        ({"A", "B", "C", "D"}, 'folder = "datasheets"\n'),  # all parts
    ]
    for removed, expected in cases:
        project.path.write_text(full, encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts = [p for p in loaded.parts if p["mpn"] not in removed]
        prj.save(loaded)
        assert project.path.read_text(encoding="utf-8") == expected, removed

    # The purchase sources of a part that is not the last part.
    nested = head + "\n".join([part("A", "C1", "C2"), part("B")])
    for removed, expected in [({"C2"}, head + "\n".join([part("A", "C1"), part("B")])),
                              ({"C1"}, head + "\n".join([part("A", "C2"), part("B")])),
                              ({"C1", "C2"}, head + "\n".join([part("A"), part("B")]))]:
        project.path.write_text(nested, encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts[0]["sources"] = [s for s in loaded.parts[0]["sources"] if s["number"] not in removed]
        prj.save(loaded)
        assert project.path.read_text(encoding="utf-8") == expected, removed

    # A comment directly above the header of a removed part goes with the part.
    # A comment with a blank line after it, and a comment above the next part, stay.
    project.path.write_text(head + part("A") + "\n# about A, B and C\n\n# about B\n" + part("B")
                            + "\n# about C\n" + part("C"), encoding="utf-8")
    loaded = prj.load(project.root)
    loaded.parts = [p for p in loaded.parts if p["mpn"] != "B"]
    prj.save(loaded)
    assert project.path.read_text(encoding="utf-8") == (head + part("A") + "\n# about A, B and C\n\n# about C\n"
                                                        + part("C"))
    for removed, expected in [({"A"}, head + "# about B\n" + part("B") + "\n" + part("C")),
                              ({"A", "B"}, head + part("C"))]:
        project.path.write_text(head + "# about A\n" + part("A") + "\n# about B\n" + part("B") + "\n" + part("C"),
                                encoding="utf-8")
        loaded = prj.load(project.root)
        loaded.parts = [p for p in loaded.parts if p["mpn"] not in removed]
        prj.save(loaded)
        assert project.path.read_text(encoding="utf-8") == expected, removed

    # Blank lines at the start of the file: the file starts with the header
    # of the next part.
    project.path.write_text("\n\n" + part("A") + "\n" + part("B"), encoding="utf-8")
    loaded = prj.load(project.root)
    del loaded.parts[0]
    prj.save(loaded)
    assert project.path.read_text(encoding="utf-8") == part("B")

    # A new document in place of the only document of a part, and an array of
    # purchase sources after it: the comment above the next part stays there.
    sha_old, sha_new = "1" * 64, "2" * 64
    a = ('[[part]]\nmpn = "A"\nmanufacturer = "M"\n\n[[part.document]]\nsha256 = "{}"\nfile = "{}.pdf"\n'
         '\n[[part.source]]\nsource = "jlcpcb"\nnumber = "C1"\n\n# above B\n')
    project.path.write_text(a.format(sha_old, "old") + part("B"), encoding="utf-8")
    loaded = prj.load(project.root)
    loaded.parts[0]["documents"] = [{"sha256": sha_new, "file": "new.pdf", "role": "datasheet"}]
    prj.save(loaded)
    assert project.path.read_text(encoding="utf-8") == a.format(sha_new, "new") + part("B")

    # A new part in place of the only part, at the start of the file.
    project.path.write_text(part("A"), encoding="utf-8")
    loaded = prj.load(project.root)
    loaded.parts = [dict(loaded.parts[0], mpn="B")]
    prj.save(loaded)
    assert project.path.read_text(encoding="utf-8") == part("B")


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
        assert prj.sync(project, store)["copied"] == ["Texas Instruments/opa2134.pdf"]

        # A new layout with the category: sync moves the copy, and removes the
        # empty subfolder.
        text = project.path.read_text(encoding="utf-8").replace('layout = "{manufacturer}"',
                                                                 'layout = "{category}/{manufacturer}"')
        project.path.write_text(text, encoding="utf-8")
        project = prj.load(project.root)
        assert prj.sync(project, store, apply=False)["moved"] == ["Texas Instruments/opa2134.pdf -> Op amp/Texas Instruments/opa2134.pdf"]
        report = prj.sync(project, store)
        assert report["moved"] == ["Texas Instruments/opa2134.pdf -> Op amp/Texas Instruments/opa2134.pdf"]
        assert (project.folder_path / "Op amp/Texas Instruments/opa2134.pdf").is_file()
        assert not (project.folder_path / "TI").exists()
        assert prj.load(project.root).parts[0]["documents"][0]["file"] == "Op amp/Texas Instruments/opa2134.pdf"

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


def test_layout_checks(store_home, tmp_path):
    for bad, message in (("{vendor}", "use only"), ("../{mpn}", "no '..'"), ("{mpn", "use")):
        with pytest.raises(LensError, match=message):
            prj.init_project(tmp_path / bad.replace("/", "_"), layout=bad)
    project = prj.init_project(tmp_path / "flat", layout="")
    by_maker = prj.Project(tmp_path, layout="{manufacturer}")
    assert prj.layout_dir(project, {"mpn": "X", "manufacturer": "TI"}) == ""
    assert prj.layout_dir(by_maker, {"mpn": "X", "manufacturer": "A/B: C"}) == "A_B_ C"
    # The built-in name: one folder for each manufacturer, also when the
    # distributors and the datasheets write its name in other forms.
    for name in ("TI", "Texas Instruments Inc.", "TEXAS INSTRUMENTS"):
        assert prj.layout_dir(by_maker, {"mpn": "X", "manufacturer": name}) == "Texas Instruments"
    assert prj.layout_dir(by_maker, {"mpn": "X", "manufacturer": "Microchip Technology"}) == "Microchip"
    # Not a relation of the store: it differs between the machines.
    with Store() as store:
        store.maker_relation("alias", "TXN", "Texas Instruments")
    assert prj.layout_dir(by_maker, {"mpn": "X", "manufacturer": "TXN"}) == "TXN"


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
    assert not (project.folder_path / "Texas Instruments/rtc.pdf").exists()


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
        assert lines == ["copied: Texas Instruments/tps7a02.pdf", "1 copies are current"], lines
        # The next sync and a move to a new layout count the file one time too.
        assert prj.format_report(prj.sync(project, store), True, ("", "")) == ["1 copies are current"]
        project.layout = "{category}"
        lines = prj.format_report(prj.sync(project, store), True, ("", ""))
    assert lines == ["moved to its subfolder: Texas Instruments/tps7a02.pdf -> _unknown/tps7a02.pdf", "1 copies are current"], lines


def test_the_add_hints_of_the_cli_and_the_server():
    """The hints of the real CLI and MCP templates: the base argument only when
    there is a base part."""
    from datasheet_lens import cli, server

    report = {k: [] for k in ("ok", "copied", "restored", "changed", "missing", "imported", "added", "moved",
                              "unknown_parts", "problems", "misplaced", "suggested", "unassigned",
                              "inbox_assigned", "inbox_known", "inbox_waiting", "inbox_new")}
    report["inbox_waiting"] = [{"file": "_inbox/d.pdf", "sha256": "d" * 64, "proposal": "", "other": []}]
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
    assert 'project add-part "<MPN>" --document dddddddddddd' in text
    assert "project_add_part(mpn='<MPN>', document='dddddddddddd')" in mcp


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
    # A DNP line is a part of the CAD project, with its datasheet (decision D1 of
    # plans/session-review-plan.md).
    assert "CAD lines with a part: 4" in out
    assert "not fitted (DNP): U9 (XYZ123). DNP is a setting of the CAD tool" in out
    assert "the same in the CAD project and in the project file: 2" in out
    assert "extra: NRF9151-LACA-R7" in out and "missing: LM3580DR/NOPB (U8)" in out
    assert "missing: XYZ123 (U9)" in out and "next steps" not in out
    assert "no part selected (a general value and no MPN): C1, C2, J2." in out
    # One column: the sniffer cannot find a delimiter.
    bom.write_text("MPN\nOPA2134\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0


def test_check_dnp_and_next_steps(store_home, tmp_path):
    """A symbol that is not fitted (DNP) is a part of the CAD project: not
    missing, not extra. A symbol is not fitted only when no variant fits it. With
    no step left, the last line gives the next steps."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("XQ1001", "YQ2002", "ZQ3003"):
            prj.add_part(project, store, mpn)
        cad = [{"mpn": "XQ1001", "reference": "U1"}, {"mpn": "YQ2002", "reference": "U2", "dnp": True},
               {"mpn": "ZQ3003", "reference": "R1, R2", "dnp": True}]
        result = prj.check_parts(project, store, cad)
        assert result["extra"] == [] and result["missing"] == [] and result["lines"] == 3
        assert result["not_fitted"] == ["U2 (YQ2002)", "R1, R2 (ZQ3003)"]
        # The parts that no symbol fits go to project_availability: their availability
        # is information only.
        assert result["not_fitted_parts"] == ["YQ2002", "ZQ3003"]
        text = prj.format_check(result)
        assert text[-1].startswith("next steps: project_sync copies the documents into the project folder. "
                                   "Then project_availability(not_fitted=['YQ2002', 'ZQ3003']) gives the "
                                   "lifecycle state")
        assert prj.format_check(result, cli=True)[-1].startswith(
            'next steps: datasheet-lens sync copies the documents into the project folder. Then datasheet-lens '
            'stock --dnp "YQ2002" "ZQ3003" gives')
        # Fitted symbols only: no list.
        assert prj.check_parts(project, store, cad[:1])["not_fitted_parts"] == []
        # A variant that fits U2 and R1: only R2 is not fitted in each variant.
        variant = [{"mpn": "YQ2002", "reference": "U2", "variant": "b"},
                   {"mpn": "ZQ3003", "reference": "R1", "variant": "b"},
                   {"mpn": "ZQ3003", "reference": "R2", "variant": "b", "dnp": True}]
        assert prj.check_parts(project, store, cad + variant)["not_fitted"] == ["R2 (ZQ3003)"]
        # Not fitted is a state of a part of a symbol: a variant that fits U2 with no
        # part, or with an other part, does not fit YQ2002.
        for other in ("-", "XQ1001"):
            lines = [{"mpn": other, "reference": "U1"}, {"mpn": "YQ2002", "reference": "U2", "dnp": True},
                     {"mpn": other, "reference": "U2", "variant": "b"}]
            assert prj.check_parts(project, store, lines)["not_fitted"] == ["U2 (YQ2002)"]
        lines = [{"mpn": "XQ1001", "reference": "U2"}, {"mpn": "YQ2002", "reference": "U2", "variant": "b", "dnp": True}]
        assert prj.check_parts(project, store, lines)["not_fitted"] == ["U2 (YQ2002)"]
        # The references in the order of the schematic.
        lines = [{"mpn": "ZQ3003", "reference": "R2, R10, R11", "dnp": True},
                 {"mpn": "ZQ3003", "reference": "R11", "variant": "b"}]
        assert prj.check_parts(project, store, lines)["not_fitted"] == ["R2, R10 (ZQ3003)"]
        # A symbol that is not fitted gets the check of its fields: the user can fit it.
        lines = [{"value": "op amp", "mpn": "YQ2002", "value_confirmed": "", "mpn_confirmed": "", "reference": "U2",
                  "dnp": True}]
        assert "not confirmed: U2:" in "\n".join(prj.format_check(prj.check_parts(project, store, lines)))
        # A JLCPCB number of a symbol that no variant fits: information, no step (the
        # assembly does not place it). A fitted symbol with no number: a step.
        prj.set_source(project, project.parts[0], "jlcpcb", "C1")
        lines = [{"mpn": "XQ1001", "reference": "U1", "lcsc": ""},
                 {"mpn": "YQ2002", "reference": "U2", "lcsc": "", "dnp": True},
                 {"mpn": "ZQ3003", "reference": "R1, R2", "lcsc": "", "dnp": True}]
        result = prj.check_parts(project, store, lines[1:] + [dict(lines[0], lcsc="C1")])
        assert result["lcsc_ok"] == 1
        assert [f["dnp"] for f in result["lcsc"] if f["kind"] == "none"] == [True, True]
        text = prj.format_check(result)
        assert ("LCSC: no JLCPCB number: U2 (YQ2002), R1, R2 (ZQ3003). Not fitted (DNP): the assembly does not place "
                "it. Tell the user; it is no step for the next production.") in "\n".join(text)
        assert text[-1].startswith("next steps: project_sync")
        result = prj.check_parts(project, store, lines)
        assert "next steps" not in "\n".join(prj.format_check(result))  # U1 needs its number
        # A part that the CAD project does not have: no next steps.
        result = prj.check_parts(project, store, cad[:2])
        assert result["extra"] == ["ZQ3003"] and "next steps" not in "\n".join(prj.format_check(result))
    # No part at all (an empty BOM and an empty project): no next steps.
    with Store() as store:
        empty = prj.init_project(tmp_path / "empty")
        assert "next steps" not in "\n".join(prj.format_check(prj.check_parts(empty, store, [])))


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
    assert ("MPN from Manufacturer Part Number 1; references from Designator; no column Value_Confirmed; no column "
            "MPN_Confirmed; read as cp1252") in out
    # No confirmed columns: nothing to confirm.
    assert "not confirmed" not in out and "the old convention" not in out
    assert "the same in the CAD project and in the project file: 1" in out
    bom = tmp_path / "eagle.csv"
    # Each library puts the MPN into a different attribute: the first filled one of the row.
    bom.write_text("Qty;Value;Parts;MP;MANUFACTURER_PART_NUMBER\n2;10k;R1 R2;;\n1;OPA2134;U1;OPA2134PA;\n"
                   "1;X;U2;;LM358DR/NOPB\n1;1N4148W;D3;;\n", encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert ("MPN from MANUFACTURER_PART_NUMBER, MP; Value from Value; references from Parts; no column "
            "Value_Confirmed; no column MPN_Confirmed") in out
    assert "CAD lines with a part: 2" in out and "missing: LM358DR/NOPB (U2)" in out
    # The MPN is in the field MPN (the convention of the four fields): no completion. With
    # no confirmed columns, a change is not found: the tool tells it.
    assert "no field MPN_Confirmed: 2 CAD parts. A change of a part (an other MPN) is not found" in out
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
    reference. (The cases of the review, in the model of the four fields.)"""
    from datasheet_lens.project import CadSymbol, symbol_state

    def state(value, mpn="", confirmed=None, mpn_confirmed=None):
        return symbol_state(CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed,
                                      mpn_confirmed=mpn_confirmed))

    # TBD: an empty field MPN (no part selected yet); N/A: the symbol needs no part.
    s = state("100nF", "TBD", "100nF", "TBD")
    assert (s.no_part, s.no_mpn, s.parts, s.conflicts) == (False, True, [], [])
    s = state("100nF", "N/A", "100nF", "N/A")
    assert (s.no_part, s.no_mpn, s.conflicts) == (True, False, [])
    s = state("2N7002", "TBD, 2N7002LT1G", "2N7002", "TBD, 2N7002LT1G")
    assert (s.parts, s.conflicts) == (["2N7002LT1G"], [])  # TBD does not tell "no part"
    bom = tmp_path / "tbd.csv"
    bom.write_text("Reference,Value,MPN,MP\nC1,100nF,TBD,\nTP1,TestPoint,N/A,\nQ1,2N7002,TBD,2N7002LT1G\n",
                   encoding="utf-8")
    assert [(p.mpn, p.conflict) for p in prj.bom_parts(str(bom)).cad] == [
        ("", ""), ("N/A", ""), ("2N7002LT1G", "")]
    # A list of MPNs has no decimal comma; an MPN can have no digit; the text of KiCad
    # for different values is not an MPN.
    assert state("Q", "2N7002,2N7000").parts == ["2N7002", "2N7000"]
    assert state("J", "PJ-SMD").parts == ["PJ-SMD"]
    assert state("10k", "-- mixed values --").conflicts == ["MPN -- mixed values --: not an MPN"]
    # TBD is an empty field: it has nothing to confirm.
    s = state("10k", "TBD", "", "")
    assert (s.unconfirmed, s.no_mpn) == (False, True)
    # A field MPN that is empty now, after a confirmation: a change, not a first one.
    s = state("10k", "", "10k", "A")
    assert (s.parts, s.removed, s.unconfirmed) == (["A"], ["A"], False)
    assert s.change == "MPN '(empty)', MPN_Confirmed 'A': the MPN is not the confirmed MPN"
    # A symbol with a conflict gets no proposal, and no "no part selected".
    s = state("10k", "-- mixed values --", "", "")
    assert (s.no_mpn, s.unconfirmed, s.change) == (False, False, "")
    s = symbol_state(CadSymbol(value="10k", mpn="A", other_mpns=[("MP", "B")]))
    assert (s.parts, s.unconfirmed) == (["A", "B"], False)
    # A new general value with the confirmed MPN: a new part, or the same part.
    s = state("220nF", "GRM1", "100nF", "GRM1")
    assert (s.select, s.removed, s.parts) == (True, [], ["GRM1"])
    # The confirmation writes the text of MPN: MPN_Confirmed is a copy.
    assert state("10k", "A, A", "", "").write_mpn == "A, A"
    # Two equal MPNs are one part, and no change.
    s = state("2N7002", "2N7002LT1G, 2N7002LT1G", "2N7002", "2N7002LT1G, 2N7002LT1G")
    assert (s.parts, s.change, s.conflicts) == (["2N7002LT1G"], "", [])
    # A change that removes the MPNs names them.
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q1", value="100nF", mpn="", value_confirmed="100nF", mpn_confirmed="2N7002LT1G")])
    assert "Not in MPN now: 2N7002LT1G." in "\n".join(prj.format_check(result))
    # A variant line with the Datasheet field of the default variant inherits it
    # (KiCad 10): it is not the datasheet of the other part of the variant.
    url = "https://ti.com/tps62130.pdf"

    def fitted(ref, mpn, **kw):
        return CadSymbol(reference=ref, value=mpn[:8], mpn=mpn, value_confirmed=mpn[:8], mpn_confirmed=mpn, **kw)

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
    from datasheet_lens.project import ValueParts, _words, field_items, field_list, format_mpns, split_value

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
    # A general value compares by its words (decision 26): the spaces between words do
    # not count, the words and their case count.
    assert _words(" 100nF   10V ") == _words("100nF 10V") == "100nF 10V"
    assert _words("10 k") != _words("10k") and _words("4m7") != _words("4M7")
    assert _words("10k (1%)") != _words("10k(1%)")
    assert field_items('2N7002,2N7000; "BC847B,215"') == ["2N7002", "2N7000", "BC847B,215"]
    assert field_items("A|B or C") == ["A|B or C"]  # the separators are , and ;
    assert field_items("ABC(1)") == ["ABC(1)"]  # a parenthesis needs no quotes in the field MPN
    # A list that is not clear: one item, the full text (no MPN is lost), and an error.
    items, error = field_list("BC847B,215, BSS138")
    assert items == ["BC847B,215, BSS138"] and "write an MPN with , ; or a parenthesis in quotes" in error
    # Quotes only where they are necessary.
    assert format_mpns(["BC847B,215"]) == "BC847B,215" and format_mpns(["A", "B"]) == "A, B"
    assert format_mpns(["BC847B,215", "BSS138"]) == '"BC847B,215", BSS138'
    assert format_mpns(["A(1)", 'B"C']) == 'A(1), B"C'
    assert format_mpns(['"A', "B;C"]) == '"""A", "B;C"'
    assert format_mpns(["~"]) == '"~"' and field_list('"~"') == (["~"], "")  # the text gives the items again


def test_symbol_state():
    """The model of plans/confirmed-mpns.md (decisions 26 to 30): Value is the general
    value, MPN the MPNs of the designer, Value_Confirmed and MPN_Confirmed the copies of
    the last confirmation. The confirmation of the user is the truth: until the user
    confirms a change, the confirmed MPNs give the parts."""
    from datasheet_lens.project import CadSymbol, symbol_state

    def state(value, mpn="", confirmed=None, mpn_confirmed=None, **kw):
        return symbol_state(CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed,
                                      mpn_confirmed=mpn_confirmed, **kw))

    both = "2N7002LT1G, BSS138LT1G"
    # Confirmed: the same general value (spaces do not count) and the same list (the
    # separators and the quotes that are not necessary do not count).
    s = state("2N7002", both, "2N7002 ", "2N7002LT1G; BSS138LT1G")
    assert (s.parts, s.change, s.unconfirmed, s.move) == (["2N7002LT1G", "BSS138LT1G"], "", False, {})
    assert not state("Q", '"A", B', "Q", "A, B").change
    # Not confirmed: an empty MPN_Confirmed or Value_Confirmed.
    s = state("2N7002", both, "", "")
    assert (s.parts, s.unconfirmed, s.write_mpn, s.new) == (
        ["2N7002LT1G", "BSS138LT1G"], True, both, ["2N7002LT1G", "BSS138LT1G"])
    assert state("2N7002", both, "2N7002", "").unconfirmed
    assert state("2N7002", both, "", both).unconfirmed
    assert state("2N7002", both, None, "").unconfirmed  # no column Value_Confirmed
    assert not state("2N7002", both).unconfirmed  # no confirmed columns: nothing to write
    # A change of the part: the confirmed MPNs stay the parts until the confirmation.
    s = state("2N7002", "2N7002LT1G", "2N7002", both)
    assert (s.parts, s.new, s.removed, s.write_mpn) == (["2N7002LT1G", "BSS138LT1G"], [], ["BSS138LT1G"],
                                                         "2N7002LT1G")
    assert s.change == ("MPN '2N7002LT1G', MPN_Confirmed '2N7002LT1G, BSS138LT1G': the MPN is not the "
                        "confirmed MPN")
    assert state("2N7002", "BSS138LT1G, 2N7002LT1G", "2N7002", both).change  # an other order
    assert state("Q", "bss138lt1g", "Q", "BSS138LT1G").change  # case counts
    s = state("2N7002", "", "2N7002", both)  # the field MPN is empty after a confirmation
    assert (s.parts, s.removed) == (["2N7002LT1G", "BSS138LT1G"], ["2N7002LT1G", "BSS138LT1G"]) and s.change
    # A change of the value and of the part.
    s = state("220nF", "GRM2", "100nF", "GRM1")
    assert (s.parts, s.new, s.removed, s.select) == (["GRM1"], ["GRM2"], ["GRM1"], False)
    assert "the value and the part are not the confirmed ones" in s.change
    # A new general value with the confirmed MPN: a new part, or the same part.
    s = state("220nF", "GRM1", "100nF", "GRM1")
    assert (s.parts, s.select, s.write_mpn) == (["GRM1"], True, "GRM1")
    assert s.change == "Value '220nF', Value_Confirmed '100nF': the MPN 'GRM1' can be wrong now"
    assert state("10 k", "A", "10k", "A").change  # the words count
    assert state("220nF", "", "100nF", "").change.endswith(": no MPN is confirmed")
    s = state("TestPoint2", "-", "TestPoint", "-")
    assert "the symbol needs no part (MPN '-'): this can be wrong now" in s.change and s.write_mpn == "-"
    assert not s.select  # no new part for a symbol that needs no part
    # The old convention (decision 30): a list of MPNs in the parentheses of Value and no
    # MPN_Confirmed. The move keeps the state: the old field MPN is the confirmed copy.
    s = state("2N7002 (2N7002LT1G, BSS138LT1G)", "2N7002LT1G", "2N7002 (2N7002LT1G)")
    assert s.move == {"value": "2N7002", "mpn": both, "mpn_confirmed": "2N7002LT1G",
                      "value_confirmed": "2N7002", "old": "Value"}
    assert (s.parts, s.change, s.unconfirmed) == (["2N7002LT1G"], "", False)
    assert state("LM358 (LM358DR)").move == {"value": "LM358", "mpn": "LM358DR", "mpn_confirmed": "",
                                             "value_confirmed": None, "old": "Value"}
    assert state("J (\"A(1)\")", "A(1)", "", "").move["mpn"] == "A(1)"
    assert not state("10k (1%)", "", "10k (1%)", "-").move  # MPN_Confirmed: the new convention
    # Other MPNs in the field MPN: the new convention (review of 2026-11-07), also with
    # no MPN_Confirmed: no move, and "1%" is no part.
    s = state("10k (1%)", "RC0603FR-0710KL", "", "")
    assert (s.move, s.parts, s.unconfirmed) == ({}, ["RC0603FR-0710KL"], True)
    assert state("10k (1%)", "RC0603FR-0710KL").parts == ["RC0603FR-0710KL"]
    # "(TBD)": the old convention with an empty list.
    assert state("10k (TBD)").move == {"value": "10k", "mpn": "", "mpn_confirmed": "", "value_confirmed": None,
                                       "old": "Value"}
    assert not state("10k (A) (B)").move and not state("10k (GRM1, -)").move  # no valid list
    # The move of a symbol with an MPN_Confirmed of the old fields: no move, the text.
    assert state("10k (1%)", "RC1", "10k (1%)", "RC1").parts == ["RC1"]
    # No part selected; no part needed.
    assert state("100nF", confirmed="", mpn_confirmed="").no_mpn
    assert state("100nF", "TBD", "100nF", "TBD").no_mpn
    s = state("TestPoint", "-", "TestPoint", "-")
    assert (s.parts, s.no_mpn, s.no_part, s.conflicts, s.change) == ([], False, True, [], "")
    assert state("TestPoint", "-", "", "").unconfirmed  # a no-part text, not confirmed yet
    # Conflicts: a list error, a no-part text and an MPN in one list, more MPN columns.
    assert "a comma before three digits" in state("Q", "BC847B,215, BSS138").conflicts[0]
    s = state("Q", "X", "Q", "BC847B,215, BSS138")
    assert "MPN_Confirmed 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
    assert s.parts == ["BC847B,215, BSS138"]  # the confirmed MPNs (one item: no MPN is lost)
    s = state("Q", '"BC847B,215", BSS138', other_mpns=[("MP", "BC847B,215, BSS138")])
    assert "MP 'BC847B,215, BSS138': a comma before three digits" in s.conflicts[0]
    assert state("Q", "GRM1, -").conflicts == ["MPN GRM1, -: - tells that the symbol needs no part"]
    s = state("2N7002", "2N7002LT1G", "2N7002", "2N7002LT1G", other_mpns=[("MP", "BSS138LT1G")])
    assert (s.parts, s.extra, s.conflicts) == (["2N7002LT1G", "BSS138LT1G"], ["BSS138LT1G"],
                                               ["MPN 2N7002LT1G, MP BSS138LT1G"])
    assert not state("Q", "X", "Q", "X", other_values=["Q (BC847B,215)"]).extra  # Value holds no MPN
    # No MPN_Confirmed: the MPNs of the old convention (also of an other Value column) are
    # parts (no MPN is lost).
    assert state("Q (X)", "X", other_values=["Q (Y)"]).parts == ["X", "Y"]


def test_check_tells_about_changes(store_home, tmp_path, capsys):
    """The messages of project_check and of the CLI check: a change, a symbol that
    is not confirmed, the move of the old convention, a symbol with no part, the
    variants, the power symbols, and the column of Value_Confirmed."""
    from datasheet_lens.project import CadSymbol

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("2N7002LT1G", "BSS138LT1G"):
            prj.add_part(project, store, mpn)
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q1", value="2N7002", mpn="2N7002LT1G, BSS138LT1G", value_confirmed="2N7002",
                      mpn_confirmed="2N7002LT1G"),
            CadSymbol(reference="U1", value="ESP32-S3", mpn="ESP32-S3-WROOM-1-N16R8", value_confirmed="",
                      mpn_confirmed=""),
            CadSymbol(reference="TP1", value="TestPoint_Small", mpn="-", value_confirmed="TestPoint",
                      mpn_confirmed="-"),
            CadSymbol(reference="#PWR01", value="+3V3", value_confirmed="", mpn_confirmed=""),
            CadSymbol(reference="R1", value="10k", value_confirmed="", mpn_confirmed=""),
            CadSymbol(reference="R5", value="10k (RC0603FR-0710KL)", mpn="RC0603FR-0710KL",
                      value_confirmed="10k (RC0603FR-0710KL)"),
            CadSymbol(reference="C5", value="220nF", mpn="GRM1", value_confirmed="100nF", mpn_confirmed="GRM1"),
            CadSymbol(reference="Q2", value="BSS138", mpn="BSS138LT1G", value_confirmed="BSS138",
                      mpn_confirmed="BSS138LT1G", variant="b"),
            CadSymbol(reference="Q2", value="BSS138", mpn="BSS138LT1G", value_confirmed="BSS138",
                      mpn_confirmed="BSS138LT1G"),
            CadSymbol(reference="Q3", value="BSS138", mpn="BSS138LT1G", value_confirmed="BSS138",
                      mpn_confirmed="BSS138LT1G"),
            CadSymbol(reference="Q3", value="2N7002", mpn="2N7002LT1G", value_confirmed="BSS138",
                      mpn_confirmed="BSS138LT1G", variant="b")])
    out = "\n".join(prj.format_check(result))
    assert ("change in the CAD project: Q1: MPN '2N7002LT1G, BSS138LT1G', MPN_Confirmed '2N7002LT1G': the MPN is "
            "not the confirmed MPN. New: BSS138LT1G. Find and add the datasheet of each new MPN now (list_documents, "
            "search_distributors, part_links, then add_document): this changes no project. Tell the user.") in out
    assert ("Write MPN_Confirmed = '2N7002LT1G, BSS138LT1G' and Value_Confirmed = '2N7002' into the symbol") in out
    assert ("not confirmed: U1: Not confirmed: ESP32-S3-WROOM-1-N16R8. When the user agrees: Write MPN_Confirmed = "
            "'ESP32-S3-WROOM-1-N16R8' and Value_Confirmed = 'ESP32-S3' into the symbol.") in out
    assert "TP1" in out and "Write MPN_Confirmed = '-'" in out
    assert "#PWR01" not in out and "no part selected (a general value and no MPN): R1." in out
    assert ("the old convention: R5: the MPNs are in the parentheses of Value. Move them into the field MPN") in out
    assert ("Write Value = '10k', MPN = 'RC0603FR-0710KL', MPN_Confirmed = 'RC0603FR-0710KL' and Value_Confirmed = "
            "'10k' into the symbol, then run project_check again") in out
    assert ("C5: Value '220nF', Value_Confirmed '100nF': the MPN 'GRM1' can be wrong now. Ask the user: a new part "
            "for the new value, or the same part. A new part: select it with the user (find_parts by its "
            "parameters, search_distributors and get_ordering_codes by its MPN), write its MPN into the symbol (the "
            "field MPN), then run project_check again. The same part: Write MPN_Confirmed = 'GRM1' and "
            "Value_Confirmed = '220nF' into the symbol.") in out
    assert "Q2" not in out  # the variant gives the same fields as the default variant
    assert "Q3 (variant b)" in out and "into the fields of the symbol in the variant of the BOM file b" in out
    assert result["lines"] == 6  # Q1, U1, R5, C5, Q2, Q3
    # A line of a group (Q5,Q6) and a line of one of its symbols in a variant file.
    with Store() as store:
        result = prj.check_parts(project, store, [
            CadSymbol(reference="Q5,Q6", value="2N7002", mpn="2N7002LT1G", value_confirmed="", mpn_confirmed=""),
            CadSymbol(reference="Q5", value="2N7002", mpn="2N7002LT1G", value_confirmed="", mpn_confirmed="",
                      variant="b.csv")])
    assert (len(result["unconfirmed"]), result["lines"]) == (1, 1)
    with Store() as store:
        result = prj.check_parts(project, store, [
            CadSymbol(reference="U6", value="TPS62130", mpn="TPS62130RGTR",
                      datasheet="https://ti.com/tps62130.pdf")])
    (missing,) = result["missing"]
    assert missing["datasheet"] == "https://ti.com/tps62130.pdf"
    assert result["no_mpn_confirmed"] == 1  # no column MPN_Confirmed: a change of a part is not found
    assert "no field MPN_Confirmed: 1 CAD parts. A change of a part (an other MPN) is not found" in "\n".join(
        prj.format_check(result))

    bom = tmp_path / "altium.csv"
    bom.write_text("Designator,Value,MPN,Custom 3,MPN_Confirmed\nQ1,2N7002,2N7002LT1G,2N7002,2N7002LT1G\n",
                   encoding="utf-8")
    assert main(["check", str(bom), "-p", str(project.root), "--confirmed-column", "Custom 3"]) == 0
    out = capsys.readouterr().out
    assert "Value_Confirmed from Custom 3" in out and "not confirmed" not in out and "change in the" not in out
    assert "MPN_Confirmed from MPN_Confirmed" in out
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
    # The column of MPN_Confirmed: its own option, and no other role.
    kicad.write_text("Reference,Value,MPN,Value_Confirmed,MC,MPN_Confirmed\nQ1,2N7002,2N7002LT1G,2N7002,2N7002LT1G,\n",
                     encoding="utf-8")
    with pytest.raises(LensError, match="is the column of the field MPN_Confirmed: give the column of the MPN"):
        prj.bom_parts(str(kicad), column="MPN_Confirmed")
    (q1,) = prj.bom_parts(str(kicad), mpn_confirmed_column="MC").cad
    assert q1.mpn_confirmed == "2N7002LT1G" and q1.mpn_confirmed_fields == ["MC"]
    with pytest.raises(LensError, match="no column 'MX' for the mpn_confirmed option"):
        prj.bom_parts(str(kicad), mpn_confirmed_column="MX")
    with pytest.raises(LensError, match="of the mpn_confirmed option is the column of MPN"):
        prj.bom_parts(str(kicad), mpn_confirmed_column="MPN")
    assert main(["check", str(bom), "-p", str(project.root)]) == 0
    assert "or use an unused custom column of the BOM and give its name with --confirmed-column" in \
        capsys.readouterr().out


def test_write_field_names(store_home, tmp_path):
    """A confirmation writes into the columns of MPN_Confirmed and Value_Confirmed, also
    a column of an other name. A move writes the MPN into each column of the MPN of the
    line; with no MPN in the line and more names of MPN columns, the field is not known:
    the message names the choices (the same rules as check_symbol_fields of
    kicad-workbench)."""
    bom = tmp_path / "altium.csv"
    bom.write_text("Designator,Value,Manufacturer Part Number,Custom 3,MPN_Confirmed\n"
                   "U1,LM358,LM358DR,,\nU2,LM358 (LM358DT),,,\n", encoding="utf-8")
    symbols = prj.bom_parts(str(bom), confirmed_column="Custom 3").cad
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        result = prj.check_parts(project, store, symbols)
    out = "\n".join(prj.format_check(result))
    assert "Write MPN_Confirmed = 'LM358DR' and Custom 3 = 'LM358' into the symbol." in out
    assert ("Write Value = 'LM358', Manufacturer Part Number = 'LM358DT', MPN_Confirmed = '' and Custom 3 = "
            "'' into the symbol, then run project_check again") in out
    # Eagle: each library has its own attribute. No MPN in the line: the user selects
    # the attribute of the symbol for the move.
    bom.write_text("Parts,Value,MP,MANUFACTURER_PART_NUMBER\nU1,X,A,A\nU2,X (B),,\nR1,10k,,\n",
                   encoding="utf-8")
    u1, u2, r1 = prj.bom_parts(str(bom)).cad
    assert (u1.mpn_fields, u1.mpn_known) == (["MANUFACTURER_PART_NUMBER", "MP"], True)
    assert (u2.mpn_fields, u2.mpn_known) == (["MANUFACTURER_PART_NUMBER", "MP"], False)
    assert (u1.confirmed_fields, u1.confirmed_known) == (["Value_Confirmed"], True)
    assert (u1.mpn_confirmed_fields, u1.mpn_confirmed_known) == (["MPN_Confirmed"], True)
    with Store() as store:
        out = "\n".join(prj.format_check(prj.check_parts(project, store, [u1, u2, r1])))
    assert ("Write Value = 'X', the MPN field = 'B' (the BOM has the columns MANUFACTURER_PART_NUMBER, MP: ask the "
            "user which one the symbol uses) and MPN_Confirmed = '' into the symbol, then run project_check again") in out
    assert "write its MPN into the field MPN of the symbol" in out
    # The parts of the parameter parts: the names of the CAD tool, else the default names.
    with Store() as store:
        out = "\n".join(prj.format_check(prj.check_parts(project, store, [
            {"reference": "U1", "value": "LM358", "mpn": "LM358DR", "value_confirmed": "", "mpn_confirmed": "",
             "mpn_fields": [" Manufacturer Part Number ", " "], "confirmed_field": "VC",
             "mpn_confirmed_field": "MC"},
            {"reference": "U2", "value": "LM358", "mpn": "LM358DT", "value_confirmed": "", "mpn_confirmed": ""}])))
    assert "MC = 'LM358DR' and VC = 'LM358'" in out
    assert "Write MPN_Confirmed = 'LM358DT' and Value_Confirmed = 'LM358'" in out


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
            prj.CadSymbol(reference="U1", value="LM358", mpn="LM358DR", value_confirmed="", mpn_confirmed=""),
            prj.CadSymbol(reference="U1", value="LM358", mpn="LM358DR", value_confirmed="", mpn_confirmed="",
                          variant="b", mpn_confirmed_fields=["MC"])])
    assert [c["mpn_confirmed_fields"] for c in result["unconfirmed"]] == [["MPN_Confirmed"], ["MC"]]


def test_review_cases_of_2026_11_07(store_home, tmp_path):
    """A variant line with the same Value and other confirmed copies than the default line
    gives its own move; a BOM line with only confirmed fields (the user removed Value and
    MPN of a confirmed symbol) is a change. (The same cases as the tests of
    kicad-workbench.)"""
    with Store() as store:
        result = prj.check_parts(prj.init_project(tmp_path / "board"), store, [
            prj.CadSymbol(reference="U1", value="LM358 (X)", mpn="X", value_confirmed="LM358 (X)"),
            prj.CadSymbol(reference="U1", value="LM358 (X)", mpn="", value_confirmed="LM358 (X)", variant="b"),
            prj.CadSymbol(reference="U2", value="LM358 (Y)", mpn="Y", value_confirmed="LM358 (Y)"),
            prj.CadSymbol(reference="U2", value="LM358 (Y)", mpn="Y", value_confirmed="LM358 (Y)", variant="b")])
    assert [(m["reference"], m["mpn_confirmed"]) for m in result["move"]] == [
        ("U1", "X"), ("U2", "Y"), ("U1 (variant b)", "")]
    bom = tmp_path / "removed.csv"
    bom.write_text("Reference,Value,MPN,MPN_Confirmed,Value_Confirmed\nR1,,,A1,10k\nR2,,,,\n", encoding="utf-8")
    (r1,) = prj.bom_parts(str(bom)).cad
    with Store() as store:
        result = prj.check_parts(prj.init_project(tmp_path / "board2"), store, [r1])
    (change,) = result["changes"]
    assert change["change"] == ("Value '', Value_Confirmed '10k'; MPN '(empty)', MPN_Confirmed 'A1': the value and "
                                "the part are not the confirmed ones")
    assert change["removed"] == ["A1"]


def test_decisions_32_and_33(store_home, tmp_path):
    """Decision 32: the old convention only in Value_Confirmed (the user removed the
    parentheses from Value) gets a move of the confirmed copies. Decision 33: a new general
    value with an MPN that the user never confirmed is a change, not select. (The same
    cases as the tests of kicad-workbench.)"""
    from datasheet_lens.project import CadSymbol, symbol_state

    def state(value, mpn="", confirmed=None, mpn_confirmed=None):
        return symbol_state(CadSymbol(reference="Q1", value=value, mpn=mpn, value_confirmed=confirmed,
                                      mpn_confirmed=mpn_confirmed))

    move = {"value": None, "mpn": None, "mpn_confirmed": "A1", "value_confirmed": "x", "old": "Value_Confirmed"}
    for mpn_confirmed in (None, ""):
        s = state("x", "A1", "x (A1)", mpn_confirmed)
        assert (s.move, s.parts, s.change, s.select) == (move, ["A1"], "", False)
    assert state("y", "A1", "x (A1)").move["value_confirmed"] == "x"  # a new value: a change after the move
    assert state("x", "A1, B1", "x (A1; B1)").move["mpn_confirmed"] == "A1, B1"
    s = state("x", "B1", "x (A1)")  # an other MPN: no move, a change of the value
    assert (s.move, bool(s.change)) == ({}, True)
    assert not state("x", "A1", "x (A1) (B)").move  # no valid list
    assert not state("x", "A1", "x (A1)", "A1").move  # MPN_Confirmed: the new convention
    assert not state("x (A1)", "A1", "x (A1)", "A1").move
    s = state("", "A1", "x (A1)", "")  # an empty Value is a removal: a change, no move
    assert (s.move, bool(s.change)) == ({}, True)
    s = state("220nF", "D1", "100nF", "")
    assert (s.select, s.new, s.parts) == (False, ["D1"], ["D1"])
    assert s.change == "Value '220nF', Value_Confirmed '100nF': the MPN 'D1' is not confirmed"
    assert state("220nF", "D1", "100nF").select  # no field MPN_Confirmed: the tool cannot know
    with Store() as store:
        result = prj.check_parts(prj.init_project(tmp_path / "board"), store, [
            CadSymbol(reference="Q1", value="x", mpn="A1", value_confirmed="x (A1)", mpn_confirmed="")])
    out = "\n".join(prj.format_check(result))
    with Store() as store:  # a variant line with an other Value writes the same copies: one move
        moves = prj.check_parts(prj.init_project(tmp_path / "board2"), store, [
            CadSymbol(reference="Q1", value="x", mpn="A1", value_confirmed="x (A1)"),
            CadSymbol(reference="Q1", value="y", mpn="A1", value_confirmed="x (A1)", variant="b")])["move"]
    assert [m["reference"] for m in moves] == ["Q1"]
    assert ("the old convention: Q1: Value_Confirmed has the MPNs in parentheses, and Value and the field MPN "
            "follow the new convention. Write the confirmed copies by the new convention: the confirmed part stays, "
            "and the parts do not change. Tell the user, and when the user agrees: Write MPN_Confirmed = 'A1' and "
            "Value_Confirmed = 'x' into the symbol, then run project_check again: it compares the fields again.") in out
    # An other Value column shows the old convention itself (decision 32: Value has no parentheses).
    s = symbol_state(CadSymbol(value="x", mpn="A1", value_confirmed="x (A1)", other_values=["x (-, B1)"]))
    assert (s.move["old"], s.extra) == ("Value_Confirmed", [])


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
    # A line that is not fitted still gives its part.
    assert parts_of("syn.csv", "Ref,MPN,DNP,Do not populate\nU1,XQ1001,,1\nU2,YQ2002,DNP,1\n") == [
        (["XQ1001"], "DNP (empty), Do not populate 1"), (["YQ2002"], "")]
    assert [s.fitted for s in bom_parts(str(tmp_path / "syn.csv")).cad] == [True, False]
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
    # different texts are one. MPN XQ1001 with other MPNs in the parentheses: the new
    # convention, the parentheses are text (no parts).
    assert parts_of("same.csv", "Reference,Designator,MPN,Value,value\nR1 R2,\"R1, R2\",XQ1001,(TLV75533PDBVR),"
                    "(tlv75533)\n") == [(["XQ1001"], "Value (TLV75533PDBVR), value (tlv75533)")]
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


INBOX_OPA = [[(50, 60, 14, "OPA2134 High Performance Audio Operational Amplifier"),
              (50, 760, 8, "TEXAS INSTRUMENTS")]]
INBOX_COPY = [[(50, 60, 14, "OPA2134 Audio Operational Amplifier"), (50, 760, 8, "ONSEMI")]]


def test_the_inbox(store_home, text_pdf, tmp_path):
    """The user puts PDF files into the inbox. sync imports each one, and gives
    it to the project parts that it agrees with: the copy goes into the
    subfolder of the part, and the file is not in the inbox then. A file with
    no part stays in the inbox for the user to decide. A datasheet of a
    different manufacturer with the same MPN is not given to the part."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        assert project.inbox_path.is_dir()
        prj.add_part(project, store, "OPA2134PA", manufacturer="TI", base="OPA2134")
        inbox = project.inbox_path
        shutil.copyfile(text_pdf("opa2134.pdf", INBOX_OPA), inbox / "opa2134.pdf")
        shutil.copyfile(text_pdf("copy.pdf", INBOX_COPY), inbox / "copy.pdf")
        (inbox / "more").mkdir()
        shutil.copyfile(text_pdf("widget.pdf", [[(50, 60, 14, "XYZ123 Widget")]]), inbox / "more" / "widget.pdf")
        dry = prj.sync(prj.load(project.root), store, apply=False)
        assert dry["inbox_new"] == ["_inbox/copy.pdf", "_inbox/more/widget.pdf", "_inbox/opa2134.pdf"]
        assert (inbox / "opa2134.pdf").is_file() and dry["imported"] == []  # a dry run changes nothing
        report = prj.sync(prj.load(project.root), store)
        assert [(a["file"], a["parts"]) for a in report["inbox_assigned"]] == [("_inbox/opa2134.pdf", ["OPA2134PA"])]
        [copy, widget] = report["inbox_waiting"]
        assert (copy["file"], copy["proposal"]) == ("_inbox/copy.pdf", "OPA2134 (onsemi)")
        assert copy["other"] == ["the MPN agrees with OPA2134PA (TI), but the datasheet is not of the same "
                                 "manufacturer, or the tool cannot read its manufacturer"]
        assert (widget["file"], widget["proposal"]) == ("_inbox/more/widget.pdf", "XYZ123 (manufacturer unknown)")
        lines = prj.format_report(report, True, ("add {part}{base} {sha}", " -b {base}"))
        assert any(line.startswith("inbox: _inbox/more/widget.pdf (") and "the PDF gives the part XYZ123" in line
                   and f"assign it with add <MPN> {widget['sha256'][:12]}" in line for line in lines)
        part = prj.load(project.root).parts[0]
        assert [(d["file"], d.get("pinned", False)) for d in part["documents"]] == [
            ("Texas Instruments/opa2134.pdf", False)]
        assert (project.folder_path / "Texas Instruments/opa2134.pdf").is_file()
        assert not (inbox / "opa2134.pdf").exists() and (inbox / "copy.pdf").is_file()
        assert store.document(widget["sha256"]) is not None  # in the store, with no part
        # The user decides: the widget is XYZ123. The next sync takes the file from the inbox.
        prj.add_part(prj.load(project.root), store, "XYZ123", document=widget["sha256"][:12])
        report = prj.sync(prj.load(project.root), store)
        assert report["inbox_known"] == ["_inbox/more/widget.pdf"]
        assert not (inbox / "more").exists() and inbox.is_dir()
        assert (project.folder_path / "_unknown/widget.pdf").is_file()  # XYZ123 has no manufacturer


INBOX_CODES = [[(50, 60, 14, "OPA2134 High Performance Audio Operational Amplifier"),
                (50, 300, 9, "ORDERING INFORMATION"), (50, 320, 9, "OPA2134UA SO-8 Tape and reel"),
                (50, 760, 8, "TEXAS INSTRUMENTS")]]


def test_the_inbox_rules(store_home, text_pdf, tmp_path, monkeypatch):
    """The ordering codes come from the index of a new document. A part with a
    datasheet does not get a second document from the inbox: the user decides
    (errata, manual, revision). A file whose document the project did not get
    stays in the inbox. An error of one file does not stop the sync. A copy
    in the inbox that a reference names is a copy, not a file of the inbox."""
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "OPA2134UA", manufacturer="TI")  # no base part: only the code agrees
        inbox = project.inbox_path
        shutil.copyfile(text_pdf("opa.pdf", INBOX_CODES), inbox / "opa.pdf")
        report = prj.sync(prj.load(project.root), store)
        assert [(a["file"], a["parts"]) for a in report["inbox_assigned"]] == [("_inbox/opa.pdf", ["OPA2134UA"])]
        # A second document for the same part: it waits for the user.
        shutil.copyfile(text_pdf("errata.pdf", [INBOX_CODES[0] + [(50, 400, 9, "Errata")]]), inbox / "errata.pdf")
        report = prj.sync(prj.load(project.root), store)
        [item] = report["inbox_waiting"]
        assert "OPA2134UA (TI), but the part has a datasheet already" in item["other"][0]
        assert (inbox / "errata.pdf").is_file()
        # The dry run tells a file that the project has already.
        prj.add_part(prj.load(project.root), store, "OPA2134UA", document=item["sha256"][:12], role="errata")
        assert prj.sync(prj.load(project.root), store, apply=False)["inbox_known"] == ["_inbox/errata.pdf"]
        assert prj.sync(prj.load(project.root), store)["inbox_known"] == ["_inbox/errata.pdf"]
        # The project does not get the document: the file stays, with a problem.
        prj.add_part(prj.load(project.root), store, "OPA2134PA", manufacturer="TI", base="OPA2134")
        shutil.copyfile(text_pdf("pa.pdf", INBOX_OPA), inbox / "pa.pdf")
        monkeypatch.setattr(prj, "store_part", lambda *args, **kwargs: (None, None))
        report = prj.sync(prj.load(project.root), store)
        assert report["inbox_assigned"] == [] and (inbox / "pa.pdf").is_file()
        assert any(p.startswith("_inbox/pa.pdf: the store links it to its parts, but the project did not get it")
                   for p in report["problems"])
        monkeypatch.undo()
        # An error of one file: a problem; the other files continue.
        from datasheet_lens import identify

        real = identify.propose
        monkeypatch.setattr(identify, "propose", lambda doc: (_ for _ in ()).throw(LensError("broken text"))
                            if doc.filename == "pa.pdf" else real(doc))
        shutil.copyfile(text_pdf("widget.pdf", [[(50, 60, 14, "XYZ123 Widget")]]), inbox / "widget.pdf")
        report = prj.sync(prj.load(project.root), store)
        assert "_inbox/pa.pdf: not taken from the inbox: broken text" in report["problems"]
        assert [w["file"] for w in report["inbox_waiting"]] == ["_inbox/widget.pdf"]
        monkeypatch.undo()
        # A reference to a file in the inbox (a pinned file of an older version): a copy.
        loaded = prj.load(project.root)
        widget = report["inbox_waiting"][0]["sha256"]
        prj.add_part(loaded, store, "XYZ123", document=widget[:12])
        loaded = prj.load(project.root)
        part = next(p for p in loaded.parts if p["mpn"] == "XYZ123")
        part["documents"] = [{"sha256": widget, "file": "_inbox/widget.pdf", "role": "datasheet", "pinned": True}]
        prj.save(loaded)
        for _ in range(2):
            report = prj.sync(prj.load(project.root), store)
            assert (inbox / "widget.pdf").is_file() and "_inbox/widget.pdf" not in report["inbox_known"]


def test_status_gives_the_parts_of_a_document_one_time(store_home, text_pdf, tmp_path):
    """Four parts of one datasheet: the status gives the parts at the first line
    of the document, and not at each part again."""
    with Store() as store:
        doc = store.add_document(text_pdf("res.pdf", [[(50, 60, 14, "RC0603FR-07")]]))
        mpns = ["RC0603FR-0710KL", "RC0603FR-071KL", "RC0603FR-0747KL", "RC0603FR-07100KL"]
        store.link_parts(doc.id, mpns[0], mpns[1:], "Yageo")
        other = store.add_document(text_pdf("ldo.pdf", [[(50, 60, 14, "TLV755P")]]))
        store.link_parts(other.id, "TLV755P", [], "TI")
        project = prj.init_project(tmp_path / "board")
        for mpn in mpns + ["TLV755P"]:
            prj.add_part(project, store, mpn)
        lines = prj.format_parts(prj.load(project.root), store)
    doc_lines = [line for line in lines if doc.sha256[:12] in line]
    assert len(doc_lines) == 4
    assert doc_lines[0].endswith("variants: " + ", ".join(sorted(mpns)))
    assert not any("variants" in line for line in doc_lines[1:])
    assert not any("variants" in line for line in lines if line[:1] != " ")  # not at the part lines
    ldo = next(line for line in lines if other.sha256[:12] in line)
    assert "variants" not in ldo  # a datasheet of one part


def test_status_gives_only_the_variants_of_a_datasheet(store_home, text_pdf, tmp_path):
    """A document of an other role (a package drawing of many parts) gives no
    list of parts: only a datasheet has variants."""
    with Store() as store:
        drawing = store.add_document(text_pdf("sot23.pdf", [[(50, 60, 14, "SOT-23")]]))
        for mpn in ("BSS138", "2N7002", "BSS84"):
            store.link(store.add_part(mpn, "onsemi"), drawing.id, "package")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "BSS138", "onsemi")
        lines = prj.format_parts(prj.load(project.root), store)
    assert any(drawing.sha256[:12] in line and "package" in line for line in lines), lines
    assert not any("variants" in line or "2N7002" in line for line in lines), lines


def test_role_package(store_home, text_pdf, tmp_path):
    """A package drawing in its own document gets the role package: in the
    store, in the project file, and at the team server."""
    from datasheet_lens.team.sync import Pusher, Rejected

    with Store() as store:
        drawing = store.add_document(text_pdf("sot23.pdf", [[(50, 60, 14, "SOT-23 package outline")]]))
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "BSS138", "onsemi", document=drawing.sha256[:12], role="package")
    part = prj.load(project.root).parts[0]
    assert [d["role"] for d in part["documents"]] == ["package"]
    assert Pusher.check_data("part_documents", {"role": "package"}) == {"role": "package"}
    with pytest.raises(Rejected, match="part_documents.role must be one of"):
        Pusher.check_data("part_documents", {"role": "drawing"})
