import io
import json

from datasheet_lens import distributors
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.store import Store

PART = {"ManufacturerPartNumber": "TPS7A0233PYCHR", "Manufacturer": "Texas Instruments",
        "Description": "LDO Voltage Regulators", "LifecycleStatus": "New Product", "IsDiscontinued": "false",
        "AvailabilityInStock": "1234", "Availability": "1234 In Stock", "LeadTime": "6 Weeks", "Min": "1",
        "Mult": "1", "PriceBreaks": [{"Quantity": 1, "Price": "0,52 €", "RawPrice": 0.52, "Currency": "EUR"}],
        # As the API gives them for a Czech account: the packaging options, no package.
        "ProductAttributes": [{"AttributeName": "Balení", "AttributeValue": "Reel"},
                              {"AttributeName": "Balení", "AttributeValue": "Cut Tape"},
                              {"AttributeName": "Standardní množství balíků", "AttributeValue": "3000"}],
        "DataSheetUrl": "https://www.ti.com/lit/ds/symlink/tps7a02.pdf", "MouserPartNumber": "595-TPS7A0233PYCHR"}
OLD = dict(PART, ManufacturerPartNumber="TPS7A0218PYCHR", LifecycleStatus="Obsolete", IsDiscontinued="true",
           SuggestedReplacement="TPS7A0218PYCHR2", AvailabilityInStock="0")


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_api(monkeypatch, parts, errors=()):
    """The Mouser API gives these parts; the requests are kept."""
    sent = []

    def urlopen(request, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        mpn = sent[-1][1]["SearchByPartRequest"]["mouserPartNumber"]
        found = [p for p in parts if p["ManufacturerPartNumber"].startswith(mpn)
                 or str(p.get("MouserPartNumber", "")).startswith(mpn)]
        return Answer(json.dumps({"Errors": [{"Message": m} for m in errors],
                                  "SearchResults": {"NumberOfResult": len(found), "Parts": found}}).encode())

    monkeypatch.setenv(distributors.MOUSER_ENV, "secret-key")
    monkeypatch.setattr(distributors.urllib.request, "urlopen", urlopen)
    distributors._cache.clear()
    return sent


def test_mouser_search(monkeypatch):
    sent = fake_api(monkeypatch, [PART, OLD])
    offers = distributors.mouser_search("TPS7A02", exact=False)
    assert [o.mpn for o in offers] == ["TPS7A0233PYCHR", "TPS7A0218PYCHR"]
    assert sent[0][1]["SearchByPartRequest"]["partSearchOptions"] == "None"
    line = distributors.format_offer(offers[0])
    assert "state: New Product" in line and "stock: 1234" in line
    assert offers[0].package == offers[0].packaging == "" and "Reel" not in line
    assert "NOT ORDERABLE: Obsolete, replacement TPS7A0218PYCHR2" in distributors.format_offer(offers[1])
    distributors.mouser_search("TPS7A02", exact=False)
    assert len(sent) == 1  # the cache


def test_mouser_texts(store_home, monkeypatch, tmp_path):
    """The lead time in the language of the Mouser account becomes English; 0
    is no lead time. An empty lifecycle is not shown. Restricted Availability is
    a warning about the distributor, not about the lifecycle."""
    lead = distributors._lead_time
    assert (lead("182 Dny"), lead("6 Weeks"), lead("3 Wochen"), lead("0 Dny"), lead("")) == (
        "182 days", "6 weeks", "3 weeks", "", "")
    assert lead("14 Tagen?") == "14 Tagen?"  # an unknown form stays as it is
    active = dict(PART, LifecycleStatus=None, LeadTime="182 Dny")
    line = distributors.format_offer(distributors._mouser_offer(active))
    assert "state:" not in line and "lead time 182 days" in line
    limited = dict(PART, ManufacturerPartNumber="MAX20361CEWC+T", LifecycleStatus="Restricted Availability",
                   AvailabilityInStock=None, Availability="", LeadTime="0 Dny", PriceBreaks=[])
    assert "not a lifecycle state" in distributors.format_offer(distributors._mouser_offer(limited))
    fake_api(monkeypatch, [active, limited])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("TPS7A0233PYCHR", "MAX20361CEWC+T"):
            prj.add_part(project, store, mpn)
    lines = distributors.availability(prj.load(project.root))
    assert "TPS7A0233PYCHR: orderable (Mouser). Mouser: stock 1234" in lines
    # Restricted: no lifecycle risk, but not orderable now.
    assert lines[lines.index("MAX20361CEWC+T: WARNING: NOT ORDERABLE at any source") + 1] == (
        "  Mouser: Restricted Availability, stock unknown. WARNING: Mouser does not sell this part now "
        "(Restricted Availability). This is not the lifecycle state of the manufacturer: ask the manufacturer "
        "or a different distributor")


def test_lifecycle_risks():
    """The lifecycle risks of the manufacturer, in the words of Mouser and
    DigiKey. A state of the distributor only is no risk; an unknown state is no
    risk and stays as the source gives it."""
    def risk(state, **kw):
        return distributors.risk_of(distributors.Offer("Mouser", "X", lifecycle=state, **kw))

    assert [risk(s) for s in ("Obsolete", "End of Life", "EOL", "Last Time Buy", "Not For New Designs",
                              "Not Recommended for New Designs", "NRND")] == (
        ["obsolete"] * 3 + ["last time buy"] + ["not for new designs"] * 3)
    assert [risk(s) for s in ("Active", "New Product", "Preliminary", "", "Restricted Availability",
                              "Discontinued at Digi-Key")] == [""] * 6
    assert risk("", discontinued=True) == "obsolete"
    assert distributors._worst(["", "not for new designs", "obsolete"]) == "obsolete"
    # The lifecycle of an MPN from its packagings: the orderable ones count, the most
    # severe risk wins, and the order of the offers does not change the result.
    offer = distributors.Offer
    active = offer("Mouser", "X", lifecycle="Active", distributor_pn="1")
    nrnd = offer("Mouser", "X", lifecycle="NRND", distributor_pn="2")
    old = offer("Mouser", "X", lifecycle="Obsolete", distributor_pn="3")
    assert distributors.lifecycle_of([active, nrnd]) == distributors.lifecycle_of([nrnd, active]) == (
        "not for new designs", "NRND", "")
    assert distributors.lifecycle_of([old, active]) == ("", "Active", "")
    assert distributors.lifecycle_of([old]) == ("obsolete", "Obsolete", "")


def test_verdict_of_a_part():
    """The verdict from all sources: the purchase source first, then the other
    sources with stock. A part with no warning gets one line."""
    state = distributors._SourceState
    part = {"mpn": "X1"}
    jlc = state("JLCPCB", ["C1"], text="stock 0, library Basic (C1)", found=True, orderable=True)
    mouser = state("Mouser", text="Active, stock 40", found=True, orderable=True, stock=40, lifecycle="Active")
    assert distributors._verdict(part, [mouser, jlc]) == [
        "X1 (C1, the purchase source): WARNING: the purchase source cannot deliver (JLCPCB: no stock). In stock at "
        "Mouser", "  Mouser: Active, stock 40", "  JLCPCB: stock 0, library Basic (C1)"]
    assert distributors._verdict(part, [mouser]) == ["X1: orderable (Mouser). Mouser: Active, stock 40"]
    # A source that was not checked: the verdict names it, with the details.
    failed = state("DigiKey", error="DigiKey does not answer")
    assert distributors._verdict(part, [mouser, failed]) == [
        "X1: orderable (Mouser). Not checked: DigiKey", "  Mouser: Active, stock 40",
        "  DigiKey: not checked: DigiKey does not answer"]
    # Last time buy: a risk also with stock.
    ltb = state("Mouser", text="Last Time Buy, stock 9", found=True, orderable=True, stock=9, risk="last time buy",
                lifecycle="Last Time Buy")
    assert distributors._verdict(part, [ltb])[0] == (
        "X1: WARNING: LAST TIME BUY at the manufacturer (Mouser: Last Time Buy). Orderable (Mouser). Select a "
        "replacement with the user (find_parts with the values of the datasheet)")


def test_mouser_errors_hide_the_key(monkeypatch):
    import urllib.error

    import pytest
    from datasheet_lens.errors import LensError

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setenv(distributors.MOUSER_ENV, "secret-key")
    monkeypatch.setattr(distributors.urllib.request, "urlopen", refuse)
    distributors._cache.clear()
    with pytest.raises(LensError) as e:
        distributors.mouser_search("X1")
    assert "401" in str(e.value) and "secret-key" not in str(e.value)
    fake_api(monkeypatch, [], errors=["Invalid unique identifier."])
    with pytest.raises(LensError, match="Invalid unique identifier.*not approved it yet"):
        distributors.mouser_search("X1")
    monkeypatch.delenv(distributors.MOUSER_ENV)
    with pytest.raises(LensError, match=distributors.MOUSER_ENV):
        distributors.mouser_search("X1")


def test_project_availability(store_home, monkeypatch, tmp_path, capsys):
    fake_api(monkeypatch, [PART, OLD])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("TPS7A0233PYCHR", "TPS7A0218PYCHR", "TPS7A02"):
            prj.add_part(project, store, mpn)
    assert main(["stock", "-p", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "TPS7A0233PYCHR: orderable (Mouser). Mouser: New Product, stock 1234" in out
    assert ("TPS7A0218PYCHR: WARNING: OBSOLETE at the manufacturer (Mouser: Obsolete, replacement TPS7A0218PYCHR2). "
            "NOT ORDERABLE at any source. Select a replacement with the user") in out
    assert "  Mouser: Obsolete, stock 0. WARNING: not orderable: Obsolete, replacement TPS7A0218PYCHR2" in out
    assert "TPS7A02: WARNING: no source has this MPN (a base part, or a wrong MPN?)\n  Mouser: does not have this MPN" in out


def test_offers_of_one_mpn_and_failures(store_home, monkeypatch, tmp_path):
    """Two offers of one MPN (cut tape, reel): orderable when one is, stock is
    the sum. No stock data is a warning. No connection stops the check."""
    import urllib.error

    import pytest
    from datasheet_lens.errors import LensError

    reel = dict(PART, MouserPartNumber="595-A", AvailabilityInStock="100")
    tape = dict(OLD, ManufacturerPartNumber="TPS7A0233PYCHR", MouserPartNumber="595-B", AvailabilityInStock="5")
    # As Mouser gives it (a live call, 2026-11-07): the text Availability is in the
    # language of the account; the data fields give the quantity on order.
    unknown = dict(PART, ManufacturerPartNumber="TMP112D0IDPWR", AvailabilityInStock=None, Availability="Žádný",
                   AvailabilityOnOrder=[{"Quantity": 2994, "Date": "2027-02-17T00:00:00"}], FactoryStock="0")
    fake_api(monkeypatch, [tape, reel, unknown])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("TPS7A0233PYCHR", "TMP112D0IDPWR"):
            prj.add_part(project, store, mpn)
    lines = distributors.availability(prj.load(project.root))
    # The orderable offer gives the state: the obsolete packaging is not a state of the MPN.
    assert "TPS7A0233PYCHR: orderable (Mouser). Mouser: New Product, stock 100" in lines
    assert lines[lines.index("TMP112D0IDPWR: WARNING: no stock at any source") + 1] == (
        "  Mouser: New Product, stock unknown. WARNING: no stock (2994 on order for 2027-02-17), lead time 6 weeks")
    assert not any("Žádný" in line for line in lines)
    offer = distributors._mouser_offer(dict(unknown, AvailabilityInStock="0", FactoryStock="40"))
    assert "stock: 0 (2994 on order for 2027-02-17, 40 at the factory)" in distributors.format_offer(offer)
    # No data field gives the state: the text of Mouser, with its source.
    offer = distributors._mouser_offer(dict(unknown, AvailabilityOnOrder=[], Availability="Non-Stocked"))
    assert "stock: unknown (Mouser: Non-Stocked)" in distributors.format_offer(offer)
    # A stock number makes the text a summary: not shown.
    assert distributors._mouser_offer(dict(unknown, AvailabilityInStock="0", AvailabilityOnOrder=[])).availability == ""

    calls = []

    def offline(request, timeout):
        calls.append(1)
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(distributors.urllib.request, "urlopen", offline)
    distributors._cache.clear()
    lines = distributors.availability(prj.load(project.root))
    assert len(calls) == 1 and lines[-1].startswith("not checked: TPS7A0233PYCHR, TMP112D0IDPWR: Mouser API")
    assert lines[:2] == ["TPS7A0233PYCHR: not checked", "  Mouser: not checked: Mouser does not answer"]
    with pytest.raises(LensError, match="not one MPN"):
        distributors.mouser_search("A|B")
    with pytest.raises(LensError, match="3 or more characters"):
        distributors.mouser_search("TP", exact=False)
    monkeypatch.delenv(distributors.MOUSER_ENV)
    assert "Findchips" in "\n".join(distributors.search_lines("TPS7A02"))
    with pytest.raises(LensError, match="the MPN is empty"):
        distributors.search_lines(" ")


def test_enabled_distributors_and_notes(store_home, monkeypatch, tmp_path, capsys):
    """The list of the project file wins over the global list, and that over the
    default. An enabled distributor with no key gives a note with the steps."""
    import pytest
    from datasheet_lens.errors import LensError

    distributors._cache.clear()
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0233PYCHR")
    root = str(project.root)
    assert distributors.enabled(prj.load(root)) == (["mouser"], "the default", [])
    assert main(["dist", "mouser", "-p", root]) == 0
    assert distributors.enabled(None)[1] == "the global configuration"
    out = capsys.readouterr().out
    assert 'run "datasheet-lens key mouser"' in out and "mouser.com/api-hub" in out
    # The project list: an empty list asks no distributor.
    assert main(["dist", "--local", "-p", root]) == 0
    text = (project.root / "datasheet-lens.toml").read_text(encoding="utf-8")
    assert "distributors" not in text
    prj.set_distributors(prj.load(root), [])
    loaded = prj.load(root)
    assert loaded.distributors == [] and loaded.parts[0]["mpn"] == "TPS7A0233PYCHR"
    assert distributors.availability(loaded) == ['No enabled distributor: "datasheet-lens dist" shows the settings.']
    with pytest.raises(LensError, match="not supported yet"):
        distributors.check_names(["farnell"])
    # No key: the note goes to the agent, with the search links.
    prj.set_distributors(prj.load(root), ["mouser"])
    lines = distributors.search_lines("TPS7A02", project=prj.load(root))
    assert lines[0].startswith("Mouser is enabled (the project file") and "Search links:" in lines
    fake_api(monkeypatch, [PART])
    assert distributors.availability(prj.load(root))[0].startswith("TPS7A0233PYCHR: orderable (Mouser)")


def test_distributor_settings_with_problems(store_home, tmp_path, capsys):
    """A name that is not supported is left out with a note. A project file with
    an error gives the error. A configuration that cannot be read is not
    written over."""
    import pytest
    from datasheet_lens.errors import LensError
    from datasheet_lens.setup import config_path, save_config

    save_config({"element_backend": "text", "distributors": ["mouser", "farnell"]})
    names, source, skipped = distributors.enabled(None)
    assert names == ["mouser"] and "farnell (in the global configuration) is not supported yet" in skipped[0]
    project = prj.init_project(tmp_path / "board")
    project.path.write_text(project.path.read_text(encoding="utf-8").replace(
        'folder = "datasheets"', 'distributors = "mouser"\nfolder = "datasheets"'), encoding="utf-8")
    with pytest.raises(LensError, match="distributors must be a list"):
        prj.load_optional(project.root)
    assert prj.load_optional(tmp_path / "elsewhere") is None
    config_path().write_text("{broken", encoding="utf-8")
    assert main(["dist", "mouser"]) == 2
    assert config_path().read_text(encoding="utf-8") == "{broken"


def test_mouser_gives_only_the_parts_of_the_text(monkeypatch, store_home, tmp_path):
    """Mouser also gives other parts: for "Exact", MPNs that only start with the
    text, and for a start, a part whose suggested replacement agrees. A part
    that Mouser lists with the number "N/A" has no number to order (live calls,
    2026-11-07)."""
    long_mpn = dict(PART, ManufacturerPartNumber="TPS7A0233PYCHR-TP", MouserPartNumber="833-TPS7A0233PYCHR-TP")
    other = dict(OLD, ManufacturerPartNumber="TPS79433DGNT", MouserPartNumber="595-TPS79433DGNT",
                 SuggestedReplacement="595-TPS7A0233PYCHR")
    no_number = dict(PART, Manufacturer="Other Maker", MouserPartNumber="N/A")
    sent = []

    def urlopen(request, timeout):  # Mouser gives each part for each text
        sent.append(json.loads(request.data))
        return Answer(json.dumps({"Errors": [], "SearchResults": {"Parts": [PART, long_mpn, other, no_number]}})
                      .encode())

    monkeypatch.setenv(distributors.MOUSER_ENV, "secret-key")
    monkeypatch.setattr(distributors.urllib.request, "urlopen", urlopen)
    distributors._cache.clear()
    offers = distributors.mouser_search("tps7a0233pychr")
    assert [(o.mpn, o.distributor_pn) for o in offers] == [("TPS7A0233PYCHR", "595-TPS7A0233PYCHR"),
                                                            ("TPS7A0233PYCHR", "")]
    assert [o.mpn for o in distributors.mouser_search("TPS7A02", exact=False)] == [
        "TPS7A0233PYCHR", "TPS7A0233PYCHR-TP", "TPS7A0233PYCHR"]
    # A Mouser number is a search text too (adopt).
    assert [o.distributor_pn for o in distributors.mouser_search("833-TPS7A0233PYCHR-TP")] == [
        "833-TPS7A0233PYCHR-TP"]
    line = distributors.format_offer(offers[1])
    assert "TPS7A0233PYCHR (Other Maker); state: New Product [NOT ORDERABLE: Mouser gives no part number to order]" \
        in line and "Mouser N/A" not in line
    # The lifecycle state comes first: an obsolete part keeps its replacement.
    obsolete = distributors._mouser_offer(dict(OLD, MouserPartNumber="N/A"))
    assert distributors.not_orderable(obsolete) == "Obsolete, replacement TPS7A0218PYCHR2"
    # The availability of a part of the manufacturer with no number: not orderable.
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0233PYCHR", manufacturer="Other Maker")
        lines = distributors.availability(prj.load(project.root), store)
    assert lines == ["TPS7A0233PYCHR: WARNING: NOT ORDERABLE at any source",
                     "  Mouser: New Product, stock 1234. WARNING: not orderable: Mouser gives no part number to order"]


def test_the_quantities_on_order_of_each_offer(monkeypatch, store_home, tmp_path):
    """The availability line gives the quantities on order of each offer of the
    part (cut tape and reel), not only of the first one."""
    reel = dict(PART, MouserPartNumber="595-A", AvailabilityInStock="0", AvailabilityOnOrder=[])
    tape = dict(PART, MouserPartNumber="595-B", AvailabilityInStock="0",
                AvailabilityOnOrder=[{"Quantity": 5000, "Date": "2027-03-01T00:00:00"}])
    fake_api(monkeypatch, [reel, tape])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0233PYCHR")
    lines = distributors.availability(prj.load(project.root))
    assert lines == ["TPS7A0233PYCHR: WARNING: no stock at any source",
                     "  Mouser: New Product, stock 0. WARNING: no stock (5000 on order for 2027-03-01), lead time 6 weeks"]


def test_mouser_prices_in_the_format_of_the_account():
    """Mouser gives the text Price in the format of the account, with its own
    currency sign ("2 012,60 Kč"), and the number RawPrice (a live call,
    2026-11-07). The price is the number with the currency code."""
    breaks = [{"Quantity": 2500, "Price": "2\u00a0012,60 Kč", "RawPrice": 2012.6, "Currency": "CZK"},
              {"Quantity": 10, "Price": "0,223 Kč", "RawPrice": 0.223, "Currency": "CZK"},
              {"Quantity": 100, "Price": "1 000,00 Kč", "RawPrice": 1000.0, "Currency": "CZK"},
              {"Quantity": 5, "Price": "1,50 €", "Currency": "EUR"},  # no RawPrice: the text, with its source
              {"Quantity": 6, "Price": "0,52 €", "RawPrice": 0.52, "Currency": ""},  # no currency code: the text
              {"Quantity": 7, "Price": "", "RawPrice": None, "Currency": "EUR"}]  # no price: left out
    offer = distributors._mouser_offer(dict(PART, PriceBreaks=breaks))
    assert offer.prices == [(2500, "2012.6 CZK"), (10, "0.223 CZK"), (100, "1000 CZK"), (5, "Mouser: 1,50 € (EUR)"),
                            (6, "Mouser: 0,52 €")]
    assert "price 2500: 2012.6 CZK, 10: 0.223 CZK, 100: 1000 CZK" in distributors.format_offer(offer)


def test_price_text_of_each_source():
    """One form of a price for each source: the number with the currency code.
    No number or no currency: no price (DigiKey can leave out UnitPrice)."""
    assert distributors.price_text(1000.0, "USD") == "1000 USD"
    assert distributors.price_text("0.0435", "USD") == "0.0435 USD"
    assert distributors.price_text(1e-05, "EUR") == "0.00001 EUR"
    for number, currency in ((None, "USD"), (True, "USD"), ("n/a", "USD"), ("NaN", "USD"), (1.5, "")):
        assert distributors.price_text(number, currency) == ""


def test_availability_of_the_purchase_source(monkeypatch, store_home, tmp_path):
    """A part with a purchase source of the distributor: the offer of its number
    (the packaging that the project orders), not each offer of the MPN. A
    number of a different MPN gives a warning; a number that the distributor
    does not have too. The parts with no purchase source: one line."""
    reel = dict(PART, MouserPartNumber="595-A", AvailabilityInStock="100")
    tape = dict(PART, MouserPartNumber="595-B", AvailabilityInStock="5")
    other = dict(PART, ManufacturerPartNumber="TMP112D0IDPWR", MouserPartNumber="595-T", AvailabilityInStock="7")
    fake_api(monkeypatch, [reel, tape, other])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("TPS7A0233PYCHR", "OPA2134PA", "LM358DR", "TMP112D0IDPWR"):
            prj.add_part(project, store, mpn)
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "mouser", "595-B")
    prj.set_source(project, project.parts[1], "mouser", "595-T")  # a number of a different MPN
    prj.set_source(project, project.parts[2], "mouser", "595-X")  # Mouser does not have it
    lines = distributors.availability(prj.load(project.root))
    assert ("TPS7A0233PYCHR (595-B, the purchase source): orderable at the purchase source (Mouser). Mouser: New "
            "Product, stock 5") in lines
    # A number of a different MPN: its stock is not the stock of the part.
    assert lines[lines.index("OPA2134PA (595-T, the purchase source): WARNING: the purchase source cannot deliver "
                             "(Mouser: 595-T is an other MPN). No other source has stock") + 1] == (
        "  Mouser: New Product, stock 7. WARNING: 595-T is TMP112D0IDPWR (Texas Instruments), not OPA2134PA")
    assert lines[lines.index("LM358DR (595-X, the purchase source): WARNING: the purchase source cannot deliver "
                             "(Mouser: does not have 595-X). No other source has stock") + 1] == (
        "  Mouser: WARNING: Mouser does not have 595-X, the purchase source in the project file")
    assert ("No purchase source of Mouser in the project file (checked by the MPN): TMP112D0IDPWR. adopt_part "
            "saves one.") in lines
    # Two numbers: the verdict gives the reason of each one.
    project = prj.load(project.root)
    prj.set_source(project, project.parts[2], "mouser", "595-T")
    lines = distributors.availability(prj.load(project.root))
    assert ("LM358DR (595-X, 595-T, the purchase sources): WARNING: the purchase source cannot deliver (Mouser: does "
            "not have 595-X; 595-T is an other MPN). No other source has stock") in lines


def test_purchase_source_that_is_not_checked(monkeypatch, store_home, tmp_path):
    """A purchase source at a source that the tool cannot ask (no key, no JLCPCB
    library) is in the verdict as not checked: the other sources do not hide it."""
    fake_api(monkeypatch, [PART])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TPS7A0233PYCHR")
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C1001")
    prj.set_source(project, project.parts[0], "digikey", "296-1-ND")
    lines = distributors.availability(prj.load(project.root))
    assert lines[:4] == [
        "TPS7A0233PYCHR (C1001, 296-1-ND, the purchase sources): WARNING: the purchase source cannot deliver "
        "(JLCPCB: not checked; DigiKey: not checked). In stock at Mouser. Not checked: JLCPCB, DigiKey",
        "  Mouser: New Product, stock 1234",
        "  JLCPCB: not checked: the JLCPCB parts library is not on this machine (datasheet-lens jlc update)",
        "  DigiKey: not checked: DigiKey is not enabled or has no API key"]


def test_offers_of_other_manufacturers_are_not_the_part(monkeypatch, store_home, tmp_path):
    """The offers of an other manufacturer give no stock and no lifecycle risk to
    the part: the verdict tells that the source has no offer of the
    manufacturer, and the details give the data with the warning."""
    # Two manufacturers of the MPN: _of_maker compares the names (one manufacturer only
    # is taken as it is, because the distributors write the names in their own forms).
    nrnd = dict(PART, Manufacturer="Other Maker", LifecycleStatus="Not Recommended for New Designs")
    fake_api(monkeypatch, [nrnd, dict(nrnd, Manufacturer="Third Maker", MouserPartNumber="595-Z")])
    # The part with no manufacturer first: the store then gives no manufacturer to it.
    for folder, maker in (("none", ""), ("ti", "Texas Instruments")):
        with Store() as store:
            project = prj.init_project(tmp_path / folder)
            prj.add_part(project, store, "TPS7A0233PYCHR", manufacturer=maker)
            lines = distributors.availability(prj.load(project.root), store)
        assert lines[0] == "TPS7A0233PYCHR: WARNING: no offer of the manufacturer of the part (Mouser)"
        assert lines[1] == "  Mouser: Not Recommended for New Designs, stock 2468. WARNING: " + (
            "Mouser has no part from Texas Instruments: the data is of Other Maker, Third Maker" if maker else
            "Mouser has the MPN from more manufacturers (Other Maker, Third Maker), and the project file gives no "
            "manufacturer of the part")
