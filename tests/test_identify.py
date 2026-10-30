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


def test_more_tokens_that_are_not_mpns():
    """Packages, values, sizes, versions and document numbers of real datasheets."""
    for token in ("X2SON", "SOT23-5", "LGA-4", "DSBGA", "WLCSP", "MFF2", "TO-92", "98.01mm2", "10X10MM", "1.This",
                  "1.65V-3.6V", "8M-BIT", "2C-Bus", "AM1.5", "v1.4", "GD25LE64E-Rev1.3", "BST-BMI323-DS000-10",
                  "DS000-3159232", "3361126", "DS-AT25QL641-130", "SC-70"):
        assert not _is_mpn(token), token
    for token in ("DS1307", "2N7002", "1N4148", "BGA525N6", "SC16IS740", "SC4215", "V23079", "LP-1234567",
                  "AM26LS31", "RR123-1H02-612",
                  "LSM6DSO16IS", "TPS7A0233PYCHR", "DFE201612E-1R0M#", "ESP32-C3", "MSP430F5529"):
        assert _is_mpn(token), token


def test_manufacturer_from_the_mpn_prefix(store_home):
    """A name only in the logo: the prefix of the MPN tells the manufacturer. A
    manufacturer name is not an MPN."""
    from datasheet_lens.identify import _manufacturer
    with Store() as store:
        assert _manufacturer(["Product Specification v1.4"], "nRF52805") == "Nordic Semiconductor"
        assert _manufacturer(["Product Specification"], "ABC123") == ""
        assert _manufacturer(["www.cototechnology.com"], "RR123") == "Coto Technology"


def test_manufacturer_is_not_an_mpn(store_home, text_pdf):
    path = text_pdf("sim_s_io3_mff2_2.pdf", [[(50, 60, 14, "1GLOBAL FOR THINGS SIM DATASHEET")]])
    with Store() as store:
        p = propose(store.add_document(path))
    assert (p.mpn, p.manufacturer) == ("", "1GLOBAL")


def test_proposal(store_home, text_pdf):
    path = text_pdf("SN74LV1T34DBVRG4_Texas_Instruments.pdf", [PAGE])
    with Store() as store:
        p = propose(store.add_document(path))
    assert (p.mpn, p.manufacturer) == ("SN74LV1T34", "Texas Instruments")
    assert "file name" in p.sources and "page 1" in p.sources
    assert p.candidates[0] == "SN74LV1T34DBVRG4"


def test_proposal_gives_the_name_of_the_pdf(store_home, text_pdf):
    """The proposal does not take the name of the store: the manufacturer
    registry of the store knows the names of one manufacturer."""
    with Store() as store:
        store.add_part("OPA2134", "TI")
        p = propose(store.add_document(text_pdf("x.pdf", [PAGE])))
    assert p.manufacturer == "Texas Instruments"


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
    assert "(it can be wrong): SN74LV1T34 (Texas Instruments" in text and "project_add_part" in text
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
        assert _manufacturer([text]) == "Texas Instruments"
        p = propose(store.add_document(text_pdf("nrf54l15.pdf", [[(50, 60, 14, "nRF54L15 Product")]])))
    assert p.mpn == "nRF54L15" and "NRF54L15" not in p.candidates

    # An MPN of two manufacturers with no manufacturer: the document is in the
    # store with its index, not linked, and the command says why.
    with Store() as store:
        store.maker_relation("distinct", "TI", "onsemi")
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
        p = propose(store.add_document(path))
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
        p = propose(store.add_document(text_pdf("lm358dr.pdf", [[(50, 60, 14, "LM358 Dual Op Amp")]])))
    assert p.mpn == "LM358" and p.variants == []

    # A document that the store links to more variants: no silent choice.
    adc = text_pdf("adc.pdf", [[(50, 60, 14, "ADC34J24")]])
    with Store() as store:
        doc = store.add_document(adc)
        assert store.link_parts(doc.id, "ADC34J24", ["ADC34J22"], "TI") == (store.find_part("ADC34J24"), [])
        # The chosen part fails: no variant is linked.
        store.maker_relation("distinct", "A", "B")
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
        store.maker_relation("distinct", "TI", "ADI")
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


def test_document_names_are_not_mpns(store_home, text_pdf):
    """Words of a document name and onsemi publication order numbers are not MPNs.
    Short forms ("MCP6001/1R/1U/2/4", "ADS1113/4/5") give no invented MPN."""
    assert not _is_mpn("MCP1827-DATA-SHEET-DS20002001") and not _is_mpn("DS20002001E-page")
    from datasheet_lens.identify import _variant

    assert not _variant("MC74VHC50", "MC74VHC50-D", ["page 1"], file_name=True)
    assert _variant("TPS7A02", "TPS7A02-EP", ["page 1"]) and _variant("LT1086", "LT1086-5", ["page 1"])
    with Store() as store:
        for name, heading, wrong in (
                ("MCP1827-Data-Sheet-DS20002001.pdf", "MCP1827/S 1.5A Low Voltage LDO Regulator", "DATA"),
                ("mcp6001.pdf", "MCP6001/1R/1U/2/4 1 MHz Op Amp", "MCP60011R"),
                ("ads1113.pdf", "ADS1113/4/5 16-Bit ADC", "ADS11134")):
            p = propose(store.add_document(text_pdf(name, [[(50, 60, 14, heading)]])))
            assert not any(wrong in c for c in [p.mpn] + p.candidates + p.variants), (name, p)
    assert p.mpn == "ADS1113"
