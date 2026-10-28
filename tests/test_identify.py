from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.identify import _is_mpn, propose
from datasheet_lens.store import Store

PAGE = [(50, 60, 14, "SN74LV1T34 Single Power Supply Single Buffer GATE"),
        (50, 90, 10, "The SN74LV1T34 is a 12-Bit, 50-MSPS buffer in SOT-23."),
        (50, 760, 8, "Copyright 2024, TEXAS INSTRUMENTS")]


def test_mpn_tokens():
    for token in ("SN74LV1T34", "74HC595", "LM20144-Q1", "ADC34J24", "STM32F407VG"):
        assert _is_mpn(token), token
    for token in ("12-Bit", "50-MSPS", "3A", "SOT-23", "Rev", "SLAS123A", "AEC-Q100", "2024"):
        assert not _is_mpn(token), token


def test_proposal(store_home, text_pdf):
    path = text_pdf("SN74LV1T34DBVRG4_Texas_Instruments.pdf", [PAGE])
    with Store() as store:
        p = propose(store, store.add_document(path))
    assert (p.mpn, p.manufacturer) == ("SN74LV1T34", "Texas Instruments")
    assert "file name" in p.sources and "page 1" in p.sources
    assert p.candidates[0] == "SN74LV1T34DBVRG4"


def test_proposal_uses_the_store_spelling(store_home, text_pdf):
    with Store() as store:
        store.add_part("OPA2134", "TI")
        p = propose(store, store.add_document(text_pdf("x.pdf", [PAGE])))
    assert p.manufacturer == "TI"


def test_add_in_a_project(store_home, text_pdf, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    project = prj.init_project(tmp_path / "board")
    path = text_pdf("SN74LV1T34DBVRG4_Texas_Instruments.pdf", [PAGE])

    # No terminal and no --yes: the proposal is not confirmed, no part, no project.
    assert main(["add", str(path), "--project", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "part: SN74LV1T34 (Texas Instruments)" in out and "not confirmed" in out
    assert "not added (the document has no part)" in out

    # --yes: one command gives the part, the project reference and the copy.
    assert main(["add", str(path), "--project", str(project.root), "--yes", "--category", "Logic"]) == 0
    out = capsys.readouterr().out
    assert "project: datasheets/Texas Instruments/SN74LV1T34DBVRG4_Texas_Instruments.pdf" in out
    part = prj.load(project.root).parts[0]
    assert (part["mpn"], part["manufacturer"], part["category"]) == ("SN74LV1T34", "Texas Instruments", "Logic")
    assert (project.folder_path / "Texas Instruments/SN74LV1T34DBVRG4_Texas_Instruments.pdf").is_file()

    # Outside a project: the store only.
    assert main(["add", str(text_pdf("other.pdf", [[(50, 60, 12, "LM20144-Q1 Buck")]])), "--part", "LM20144-Q1"]) == 0
    assert "project" not in capsys.readouterr().out


def test_mcp_add_gives_the_proposal(store_home, text_pdf, monkeypatch):
    import asyncio

    from datasheet_lens import server

    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    path = text_pdf("SN74LV1T34DBVRG4_Texas_Instruments.pdf", [PAGE])
    result = asyncio.run(server.mcp.call_tool("add_document", {"path": str(path)}))
    blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    text = "\n".join(getattr(b, "text", "") for b in blocks)
    assert "the PDF proposes SN74LV1T34 (Texas Instruments" in text and "project_add_part" in text
    for job in list(server._builds.values()):
        job["thread"].join(10)
    server._builds.clear()


def test_review_cases(store_home, text_pdf, capsys):
    """Real MPNs that look like standards or literature numbers, lists of MPNs,
    one MPN in two spellings, and a manufacturer that a text names in passing."""
    from datasheet_lens.identify import TOKEN_RE, _manufacturer

    for token in ("ADXL345", "ISO7741", "USB2514", "SMBJ100A", "TUSB320", "PCAL6416"):
        assert _is_mpn(token), token
    assert TOKEN_RE.findall("AD8605/AD8606/AD8608") == ["AD8605", "AD8606", "AD8608"]
    with Store() as store:
        text = "Texas Instruments ... compatible with NXP Semiconductors I2C ... TEXAS INSTRUMENTS"
        assert _manufacturer(store, [text]) == "Texas Instruments"
        p = propose(store, store.add_document(text_pdf("nrf54l15.pdf", [[(50, 60, 14, "nRF54L15 Product")]])))
    assert p.mpn == "nRF54L15" and "NRF54L15" not in p.candidates

    # An MPN of two manufacturers with no manufacturer: the document is in the
    # store with its index, not linked, and the command says why.
    with Store() as store:
        store.add_part("LM358", "TI")
        store.add_part("LM358", "onsemi")
    assert main(["add", str(text_pdf("lm358.pdf", [[(50, 60, 12, "LM358 Dual Op Amp")]])), "--part", "LM358"]) == 0
    assert "not linked: part LM358 is not unique" in capsys.readouterr().err
    with Store() as store:
        assert store.build(1, "index") is not None or store.build(2, "index") is not None


def test_variants(store_home, text_pdf, capsys, monkeypatch, tmp_path):
    """A datasheet of a family: the store links it to each variant, and the
    project part (the variant that the design uses) lists the variants."""
    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    page = [(50, 60, 14, "ADC34J2x Quad-Channel 12-Bit ADC"),
            (50, 90, 10, "Devices: ADC34J22, ADC34J23, ADC34J24, ADC34J25"),
            (50, 760, 8, "TEXAS INSTRUMENTS")]
    path = text_pdf("adc34j24.pdf", [page])
    with Store() as store:
        p = propose(store, store.add_document(path))
    assert p.mpn == "ADC34J24" and p.variants == ["ADC34J22", "ADC34J23", "ADC34J24", "ADC34J25"]

    project = prj.init_project(tmp_path / "board")
    assert main(["add", str(path), "--project", str(project.root), "--yes"]) == 0
    assert "the datasheet covers the variants: ADC34J22, ADC34J23, ADC34J24, ADC34J25" in capsys.readouterr().out
    with Store() as store:
        for mpn in p.variants:
            assert [r["filename"] for r in store.part_documents(mpn)] == ["adc34j24.pdf"]
    part = prj.load(project.root).parts[0]
    assert part["mpn"] == "ADC34J24" and part["variants"] == p.variants
    assert 'variants = ["ADC34J22", "ADC34J23", "ADC34J24", "ADC34J25"]' in project.path.read_text(encoding="utf-8")


def test_variant_rules(store_home, text_pdf, capsys, monkeypatch):
    from datasheet_lens.identify import _variant

    assert _variant("ADC34J24", "ADC34J22", ["page 1"]) and _variant("TL072", "TL071", ["title"])
    assert _variant("LM20144", "LM20144-Q1", [], file_name=True)
    for other in ("LM358DR", "LM358D"):  # orderable codes
        assert not _variant("LM358", other, ["page 1"], file_name=True)
    assert not _variant("LM20144", "LM20144Q", ["page 1"])
    with Store() as store:
        p = propose(store, store.add_document(text_pdf("lm358dr.pdf", [[(50, 60, 14, "LM358 Dual Op Amp")]])))
    assert p.mpn == "LM358" and p.variants == []

    # A document that the store links to more variants: no silent choice.
    adc = text_pdf("adc.pdf", [[(50, 60, 14, "ADC34J24")]])
    with Store() as store:
        doc = store.add_document(adc)
        assert store.link_parts(doc.id, "ADC34J24", ["ADC34J22"], "TI") == (store.find_part("ADC34J24"), [])
        # The chosen part fails: no variant is linked.
        store.add_part("X1000", "A")
        store.add_part("X1000", "B")
        part_id, failures = store.link_parts(doc.id, "X1000", ["X1001"])
        assert part_id is None and "not unique" in failures[0]
        assert not store.match_parts("X1001")
    assert main(["add", str(adc), "--no-project"]) == 0
    assert "give the variant that you use with --part" in capsys.readouterr().out


def test_mcp_variant_failures(store_home, text_pdf, monkeypatch):
    import asyncio

    from datasheet_lens import server

    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    with Store() as store:
        store.add_part("ADC34J23", "TI")
        store.add_part("ADC34J23", "ADI")
    path = text_pdf("adc34j24.pdf", [[(50, 60, 14, "ADC34J24")]])
    result = asyncio.run(server.mcp.call_tool("add_document", {"path": str(path), "part": "ADC34J24",
                                                               "variants": ["ADC34J23"]}))
    blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    text = "\n".join(getattr(b, "text", "") for b in blocks)
    assert "part ADC34J23: not linked" in text and "element map" in text
    for job in list(server._builds.values()):
        job["thread"].join(10)
    server._builds.clear()


def test_empty_variants_leave_the_file(store_home, text_pdf, tmp_path):
    with Store() as store:
        doc = store.add_document(text_pdf("adc34j24.pdf", [[(50, 60, 14, "ADC34J24")]]))
        store.link_parts(doc.id, "ADC34J24", ["ADC34J22"], "TI")
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "ADC34J24")
        assert "variants = " in project.path.read_text(encoding="utf-8")
        store.link(store.find_part("ADC34J22"), doc.id, "other")  # no longer its datasheet
        prj.sync(project, store)
    assert "variants" not in project.path.read_text(encoding="utf-8")
