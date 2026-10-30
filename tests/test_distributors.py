import io
import json

from datasheet_lens import distributors
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.store import Store

PART = {"ManufacturerPartNumber": "TPS7A0233PYCHR", "Manufacturer": "Texas Instruments",
        "Description": "LDO Voltage Regulators", "LifecycleStatus": "New Product", "IsDiscontinued": "false",
        "AvailabilityInStock": "1234", "Availability": "1234 In Stock", "LeadTime": "6 Weeks", "Min": "1",
        "Mult": "1", "PriceBreaks": [{"Quantity": 1, "Price": "0,52 €", "Currency": "EUR"}],
        "ProductAttributes": [{"AttributeName": "Packaging", "AttributeValue": "Reel"},
                              {"AttributeName": "Package / Case", "AttributeValue": "DSBGA-4"}],
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
        found = [p for p in parts if p["ManufacturerPartNumber"].startswith(mpn)]
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
    assert "state: New Product" in line and "stock: 1234" in line and "DSBGA-4 / Reel" in line
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
    assert "TPS7A0233PYCHR: stock 1234" in lines
    assert ("MAX20361CEWC+T: Restricted Availability, stock unknown. WARNING: Mouser does not sell this part now "
            "(Restricted Availability). This is not the lifecycle state of the manufacturer: ask the manufacturer "
            "or a different distributor") in lines


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
    assert "TPS7A0233PYCHR: New Product, stock 1234" in out
    assert "TPS7A0218PYCHR: Obsolete, stock 0. WARNING: not orderable: Obsolete, replacement TPS7A0218PYCHR2" in out
    assert "TPS7A02: WARNING: Mouser does not have this MPN" in out


def test_offers_of_one_mpn_and_failures(store_home, monkeypatch, tmp_path):
    """Two offers of one MPN (cut tape, reel): orderable when one is, stock is
    the sum. No stock data is a warning. No connection stops the check."""
    import urllib.error

    import pytest
    from datasheet_lens.errors import LensError

    reel = dict(PART, MouserPartNumber="595-A", AvailabilityInStock="100")
    tape = dict(OLD, ManufacturerPartNumber="TPS7A0233PYCHR", MouserPartNumber="595-B", AvailabilityInStock="5")
    unknown = dict(PART, ManufacturerPartNumber="TMP112D0IDPWR", AvailabilityInStock=None, Availability="On Order")
    fake_api(monkeypatch, [tape, reel, unknown])
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("TPS7A0233PYCHR", "TMP112D0IDPWR"):
            prj.add_part(project, store, mpn)
    lines = distributors.availability(prj.load(project.root))
    assert "TPS7A0233PYCHR: New Product, stock 100" in lines
    assert "TMP112D0IDPWR: New Product, stock unknown. WARNING: no stock (On Order), lead time 6 weeks" in lines

    calls = []

    def offline(request, timeout):
        calls.append(1)
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(distributors.urllib.request, "urlopen", offline)
    distributors._cache.clear()
    lines = distributors.availability(prj.load(project.root))
    assert len(calls) == 1 and lines[-1].startswith("not checked: TPS7A0233PYCHR, TMP112D0IDPWR: Mouser API")
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
    assert distributors.availability(prj.load(root))[1].startswith("TPS7A0233PYCHR: New Product")


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
