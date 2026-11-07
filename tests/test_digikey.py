"""The DigiKey adapter against a fake API. The data is made up: the terms of
DigiKey do not permit to publish DigiKey data in the repository."""

import http.client
import io
import json
import urllib.error
import urllib.parse

import pytest

from datasheet_lens import digikey, distributors
from datasheet_lens.cli import main
from datasheet_lens.errors import Choose, LensError, Unavailable

CATEGORIES = {"Categories": [{"CategoryId": 2, "Name": "Cables", "ProductCount": 9, "Children": [
    {"CategoryId": 20, "ParentId": 2, "Name": "Accessories", "ProductCount": 4, "Children": []}]},
    {"CategoryId": 3, "Name": "Fans", "ProductCount": 9, "Children": [
        {"CategoryId": 30, "ParentId": 3, "Name": "Accessories", "ProductCount": 5, "Children": []}]},
    {"CategoryId": 1, "Name": "Semiconductors", "ProductCount": 30, "Children": [
    {"CategoryId": 10, "ParentId": 1, "Name": "FETs, MOSFETs", "ProductCount": 30, "Children": [
        {"CategoryId": 100, "ParentId": 10, "Name": "Single FETs, MOSFETs", "ProductCount": 25, "Children": []},
        {"CategoryId": 101, "ParentId": 10, "Name": "FET, MOSFET Arrays", "ProductCount": 5, "Children": []}]}]}]}


def _filter(pid, name, values):
    return {"ParameterId": pid, "ParameterName": name, "ParameterType": "String",
            "FilterValues": [{"ValueId": vid, "ValueName": text} for vid, text in values]}


FILTERS = {"ProductsCount": 25, "FilterOptions": {"ParametricFilters": [
    _filter(5, "FET Type", [("51", "N-Channel"), ("52", "P-Channel")]),
    _filter(6, "Drain to Source Voltage (Vdss)", [("12 V", "12 V"), ("20 V", "20 V"), ("30 V", "30 V"),
                                                  ("60 V", "60 V")]),
    _filter(7, "Drive Voltage (Max Rds On, Min Rds On)", [("71", "1.8V, 4.5V"), ("72", "2.5V, 10V"),
                                                           ("73", "4.5V, 10V")]),
    _filter(8, "Rds On (Max) @ Id, Vgs", [("81", "4.5mOhm @ 20A, 8V"), ("82", "20mOhm @ 5A, 4.5V")]),
    _filter(9, "Supplier Device Package", [("91", "8-VSON (3.3x3.3)"), ("92", "SOT-23-3")]),
    _filter(11, "Package / Case", [("111", "8-PowerTDFN")])]}}
PRODUCT = {"ManufacturerProductNumber": "FAKE-FET-1", "Manufacturer": {"Name": "Maker A"}, "UnitPrice": "12.5",
           "QuantityAvailable": 900, "ProductStatus": {"Status": "Active"},
           "DatasheetUrl": "//example.com/fake-fet-1.pdf", "ProductUrl": "https://www.digikey.com/p/1",
           "ProductVariations": [{"DigiKeyProductNumber": "1-TR-ND", "PackageType": {"Name": "Tape & Reel (TR)"}},
                                 {"DigiKeyProductNumber": "1-CT-ND", "PackageType": {"Name": "Cut Tape (CT)"}}],
           "Parameters": [{"ParameterText": "Drain to Source Voltage (Vdss)", "ValueText": "30 V"},
                          {"ParameterText": "Supplier Device Package", "ValueText": "8-VSON (3.3x3.3)"}]}


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def api(monkeypatch):
    """The fake DigiKey API. It keeps each request (URL, headers, body)."""
    for cache in (digikey._token, digikey._categories, digikey._parameters, digikey._not_applied_cache):
        cache.clear()
    monkeypatch.setenv("DATASHEET_LENS_DIGIKEY_ID", "client-id")
    monkeypatch.setenv("DATASHEET_LENS_DIGIKEY_SECRET", "client-secret")
    state = {"sent": [], "fail": []}

    def urlopen(request, timeout):
        body = request.data.decode() if request.data else ""
        state["sent"].append((request.full_url, dict(request.header_items()), body))
        if state["fail"]:
            error = state["fail"].pop(0)
            if isinstance(error, int):
                raise urllib.error.HTTPError(request.full_url, error, "Error", {},
                                             io.BytesIO(b'{"detail": "a message"}'))
            raise error
        if request.full_url == digikey.TOKEN_URL:
            return Answer(json.dumps({"access_token": "token-1", "expires_in": 599}).encode())
        if request.full_url.endswith("/search/categories"):
            return Answer(json.dumps(CATEGORIES).encode())
        if request.full_url.endswith("/media"):
            if "NO-SUCH-PART" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
            return Answer(json.dumps({"MediaLinks": [
                {"MediaType": "Datasheets", "Title": "FAKE-FET-1 Datasheet", "Url": "https://example.com/ds.pdf"},
                {"MediaType": "Product Photos", "Title": "photo", "Url": "https://example.com/p.jpg"},
                {"MediaType": "EDA Models", "Title": "FAKE-FET-1 by SnapEDA",
                 "Url": "https://www.snapeda.com/parts/FAKE-FET-1/view-part/?ref=digikey"}]}).encode())
        known = [p for v in state.get("products", {}).values() for p in (v if isinstance(v, list) else [v])]
        if request.full_url.endswith("/productdetails"):
            # A DigiKey number, in upper or lower case. As DigiKey: an MPN of more
            # products gives HTTP 404.
            number = urllib.parse.unquote(request.full_url.split("/search/")[1].split("/")[0]).upper()
            found = [p for p in known
                     if number in [v["DigiKeyProductNumber"].upper() for v in p["ProductVariations"]]]
            if len(found) != 1:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
            return Answer(json.dumps({"Product": found[0]}).encode())
        query = urllib.parse.urlparse(request.full_url).query
        if "ExactMatches" in query:  # the offers of an MPN: the keyword search
            word = json.loads(body)["Keywords"].upper()
            found = known + [PRODUCT]
            return Answer(json.dumps({
                "ExactMatches": [p for p in found if p["ManufacturerProductNumber"].upper() == word],
                "Products": [p for p in found if word in p["ManufacturerProductNumber"].upper()]}).encode())
        if "includes" in query:  # a search with filters
            sent_filters = json.loads(body)["FilterOptionsRequest"]["ParameterFilterRequest"]["ParameterFilters"]
            if any(f["ParameterId"] in state.get("ignored", ()) for f in sent_filters) or (
                    state.get("no_part_for_more") and len(sent_filters) > 1):
                return Answer(json.dumps({"Products": []}).encode())  # as DigiKey: no ProductsCount
            return Answer(json.dumps({"ProductsCount": 1, "Products": [PRODUCT]}).encode())
        return Answer(json.dumps(FILTERS).encode())

    monkeypatch.setattr(digikey.urllib.request, "urlopen", urlopen)
    return state


def _searches(state):
    return [json.loads(body) for url, _, body in state["sent"] if "includes" in url]


def test_search_sends_the_value_ids_and_gives_the_source(api):
    filters = ["channel = N-Channel", "vdss >= 20 V", "vdss <= 40V", "drive <= 2.5V", "package has 3x3",
               "rds_on <= 10 mOhm at Vgs=2.5V"]
    lines = digikey.search_lines(digikey.search("100", filters, sort="price", limit=5))
    request = _searches(api)[0]
    groups = {f["ParameterId"]: [v["Id"] for v in f["FilterValues"]]
              for f in request["FilterOptionsRequest"]["ParameterFilterRequest"]["ParameterFilters"]}
    # Two filters on one parameter: the values of both. The condition filter is not sent.
    assert groups == {5: ["51"], 6: ["20 V", "30 V"], 7: ["71", "72"], 9: ["91"]}
    assert request["FilterOptionsRequest"]["ParameterFilterRequest"]["CategoryFilter"] == {"Id": "100"}
    assert request["FilterOptionsRequest"]["SearchOptions"] == ["InStock"]
    assert request["SortOptions"] == {"Field": "Price", "SortOrder": "Ascending"} and request["Limit"] == 5
    text = "\n".join(lines)
    assert lines[0].startswith("Source: DigiKey. Category 100 Semiconductors > FETs, MOSFETs > Single FETs")
    assert 'filter "vdss <= 40V": applied by DigiKey to "Drain to Source Voltage (Vdss)" (3 values)' in text
    assert 'filter "channel = N-Channel": applied by DigiKey to "FET Type" (1 value)' in text
    assert 'filter "rds_on <= 10 mOhm at Vgs=2.5V": NOT APPLIED' in text
    assert ("FAKE-FET-1 (Maker A); DigiKey 1-CT-ND; state: Active; stock: 900; price 1: 12.5 USD; "
            "Drain to Source Voltage (Vdss): 30 V") in text
    assert "datasheet https://example.com/fake-fet-1.pdf" in text and "read each value" in text.lower()
    # The credential goes only to the token request, never into a URL.
    assert all("client-secret" not in url for url, _, _ in api["sent"])
    headers = api["sent"][-1][1]
    assert headers["Authorization"] == "Bearer token-1" and headers["X-digikey-client-id"] == "client-id"
    assert headers["X-digikey-locale-language"] == "en"


def test_no_matching_value_asks_nothing(api):
    lines = digikey.search_lines(digikey.search("Single FETs, MOSFETs", ["vdss >= 2 kV"]))
    assert _searches(api) == [] and "0 parts agree" in lines[0]
    assert 'no value of "Drain to Source Voltage (Vdss)" agrees' in lines[1] and "12 V | 20 V" in lines[1]


def test_category_and_parameter_errors(api):
    with pytest.raises(Choose, match="fits more DigiKey categories") as e:
        digikey.search("mosfet", [])
    # The name in quotes to copy, then the ID, the path and the parts; a leaf category first.
    assert e.value.lines[1] == ('  "Single FETs, MOSFETs" (ID 100): Semiconductors > FETs, MOSFETs > Single FETs, '
                                'MOSFETs, 25 parts')
    assert "[has subcategories: select one of them]" in str(e.value)
    assert e.value.lines[-1].startswith('For example: datasheet-lens pick digikey "Single FETs, MOSFETs" -P')
    with pytest.raises(Choose, match='The category "FETs, MOSFETs" has subcategories') as e:
        digikey.search("10", [])
    assert '"FET, MOSFET Arrays" (ID 101)' in str(e.value)
    # The CLI and the MCP tools give the list as a normal result.
    assert distributors.pick_lines("digikey", "mosfet", [])[0].startswith("The category 'mosfet' fits more")
    with pytest.raises(LensError, match="no DigiKey category"):
        digikey.search("capacitor", [])
    with pytest.raises(LensError, match="fits more parameters"):  # Vdss and Drive Voltage
        digikey.search("100", ["voltage >= 2 V"])
    with pytest.raises(LensError, match="no parameter 'foo'"):
        digikey.search("100", ["foo = bar"])
    with pytest.raises(LensError, match="give NAME OP VALUE"):  # before a request
        digikey.search("100", ["vdss 20 V"])
    with pytest.raises(LensError, match="limit: 1 to 50"):
        digikey.search("100", [], limit=51)


def test_parameter_lines(api):
    lines = digikey.parameter_lines("100")
    assert lines[0].startswith("Source: DigiKey. Category 100") and "(25 parts)" in lines[0]
    assert "Drain to Source Voltage (Vdss) (short name vdss): 4 values: 12 V | 20 V | 30 V | 60 V" in lines
    digikey.parameter_lines("100")
    assert sum(1 for url, _, _ in api["sent"] if url.endswith("/search/keyword")) == 1  # the cache


def test_errors_of_the_api(api):
    api["fail"] = [401]
    with pytest.raises(LensError, match="HTTP 401 Error: a message. The key is not valid"):
        digikey.parameter_lines("100")
    api["fail"] = [429]
    with pytest.raises(Unavailable, match="HTTP 429"):
        digikey.parameter_lines("100")
    # The server ends a large answer early: the request is sent again.
    api["fail"] = [http.client.IncompleteRead(b"x", 10)]
    assert digikey.parameter_lines("100")[0].startswith("Source: DigiKey")
    digikey._parameters.clear()
    api["fail"] = [http.client.IncompleteRead(b"x", 10)] * digikey.ATTEMPTS
    with pytest.raises(Unavailable, match="ended the answer early 3 times"):
        digikey.parameter_lines("100")


def test_no_key_gives_the_steps(monkeypatch, store_home):
    with pytest.raises(LensError) as e:
        distributors.pick_lines("digikey", "100", [])
    text = str(e.value)
    assert 'run "datasheet-lens key digikey"' in text and "Client ID and the Client Secret" in text
    assert "DATASHEET_LENS_DIGIKEY_ID and DATASHEET_LENS_DIGIKEY_SECRET" in text
    # The default without a key: Mouser only. The steps also enable DigiKey.
    assert 'Then enable it: "datasheet-lens dist mouser digikey"' in text
    assert main(["pick", "digikey", "100", "vdss >= 20 V"]) == 2


def test_default_distributors_are_those_with_a_key(api, store_home):
    assert distributors.enabled(None) == (["digikey"], "the default: the distributors with a saved key", [])
    lines = distributors.pick_lines("digikey", "100", ["vdss >= 20 V"])
    assert lines[0].startswith("Source: DigiKey")


def test_cli_pick_and_locale(api, store_home, capsys):
    assert main(["dist", "--site", "cz", "--currency", "czk"]) == 0
    assert "global: site CZ" in capsys.readouterr().out and digikey.locale() == ("CZ", "CZK")
    assert main(["pick", "digikey", "100", "-P"]) == 0
    assert "short name vdss" in capsys.readouterr().out
    assert main(["pick", "digikey", "100", "vdss >= 20 V", "-a", "-n", "3", "-o", "6"]) == 0
    out = capsys.readouterr().out
    assert "price 1: 12.5 CZK" in out
    request = _searches(api)[-1]
    assert "SearchOptions" not in request["FilterOptionsRequest"] and (request["Limit"], request["Offset"]) == (3, 6)
    assert api["sent"][-1][1]["X-digikey-locale-site"] == "CZ"
    assert main(["dist", "--site", "CZE"]) == 2
    assert main(["dist", "--site", ""]) == 2  # an empty value is not "no change"
    assert main(["dist", "--local", "--site", "CZ"]) == 2


def test_mcp_tools(api, store_home):
    from datasheet_lens import server

    assert server.part_parameters("digikey", "100").startswith("Source: DigiKey")
    assert "FAKE-FET-1" in server.find_parts("digikey", "100", ["vdss >= 20 V"])


def _product(mpn, status="Active", stock=900):
    return dict(PRODUCT, ManufacturerProductNumber=mpn, ProductStatus={"Status": status}, QuantityAvailable=stock,
                Description={"ProductDescription": "MOSFET N-CH"},
                ProductVariations=[{"DigiKeyProductNumber": f"{mpn}-CT-ND", "PackageType": {"Name": "Cut Tape (CT)"},
                                    "MinimumOrderQuantity": 1,
                                    "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 12.5},
                                                        {"BreakQuantity": 10, "UnitPrice": 9.1}]}])


def test_offers_by_mpn(api, store_home):
    api["products"] = {"FAKE-FET-1": _product("FAKE-FET-1")}
    [offer] = digikey.offers("FAKE-FET-1")
    assert (offer.source, offer.mpn, offer.manufacturer, offer.lifecycle, offer.stock) == (
        "DigiKey", "FAKE-FET-1", "Maker A", "Active", 900)
    assert offer.prices == [(1, "12.5 USD"), (10, "9.1 USD")] and offer.distributor_pn == "FAKE-FET-1-CT-ND"
    assert offer.package == "8-VSON (3.3x3.3)" and offer.datasheet_url == "https://example.com/fake-fet-1.pdf"
    assert digikey.offers("NO-SUCH-PART") == []  # HTTP 404
    # The start of an MPN: the keyword search, only the MPNs that start with it.
    assert [o.mpn for o in digikey.offers("FAKE", exact=False)] == ["FAKE-FET-1"]
    assert digikey.offers("OTHER", exact=False) == []
    line = "\n".join(distributors.search_lines("FAKE-FET-1", exact=True))
    assert "Source: DigiKey (1 parts)" in line and "8-VSON (3.3x3.3) / Cut Tape (CT)" in line


def test_availability_with_digikey(api, store_home, tmp_path):
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    api["products"] = {"FAKE-FET-1": _product("FAKE-FET-1"),
                       "FAKE-FET-2": _product("FAKE-FET-2", "Discontinued at Digi-Key", 0),
                       "FAKE-FET-3": _product("FAKE-FET-3", "Obsolete", 0)}
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("FAKE-FET-1", "FAKE-FET-2", "FAKE-FET-3", "FAKE-FET-4"):
            prj.add_part(project, store, mpn)
    lines = distributors.availability(prj.load(project.root))
    assert lines[0] == "Source: DigiKey" and lines[1] == "FAKE-FET-1: Active, stock 900"
    assert lines[2].startswith("FAKE-FET-2: Discontinued at Digi-Key, stock 0. WARNING: DigiKey does not sell "
                               "this part now (Discontinued at Digi-Key). This is not the lifecycle state")
    assert lines[3] == "FAKE-FET-3: Obsolete, stock 0. WARNING: not orderable: Obsolete"
    assert lines[4].startswith("FAKE-FET-4: WARNING: DigiKey does not have this MPN")


def test_services_of_each_distributor(api, store_home):
    text = "\n".join(distributors.status_lines())
    assert "mouser: MPN search" in text and "Not available: parametric search" in text
    assert "digikey: parametric search (part_parameters, find_parts with source digikey)" in text
    # Only DigiKey has a key, and the JLCPCB database is not on this machine.
    assert text.count("Not enabled") == 2 and "jlcpcb: the local database is not on this machine" in text


def test_stock_with_no_quantity_available():
    """The API leaves out QuantityAvailable when it is 0: the stock of each
    packaging gives it. With no data at all, the stock is unknown."""
    product = {k: v for k, v in PRODUCT.items() if k != "QuantityAvailable"}
    product["ProductVariations"] = [{"QuantityAvailableforPackageType": 0}, {"QuantityAvailableforPackageType": "0"}]
    assert digikey.stock(product) == 0
    assert digikey.stock({"ProductVariations": [{}]}) is None
    # The packagings share one stock: the largest value, not the sum (a live
    # call, 2026-11-07: 126000, 126773 and 126773 gave 379546).
    assert digikey.stock({"ProductVariations": [{"QuantityAvailableforPackageType": 126000},
                                                {"QuantityAvailableforPackageType": 126773},
                                                {"QuantityAvailableforPackageType": "126773"}]}) == 126773
    assert digikey.stock(PRODUCT) == 900


def test_direct_links_of_digikey(api, store_home, capsys):
    lines = distributors.link_lines("FAKE-FET-1")
    assert "Direct links (Source: DigiKey):" in lines
    assert "  datasheet: FAKE-FET-1 Datasheet: https://example.com/ds.pdf" in lines
    assert ("  symbol, footprint, 3D model: FAKE-FET-1 by SnapEDA: "
            "https://www.snapeda.com/parts/FAKE-FET-1/view-part/?ref=digikey") in lines
    assert not any("photo" in line for line in lines)
    assert "Direct links (Source: DigiKey):" not in distributors.link_lines("NO-SUCH-PART")
    assert main(["links", "FAKE-FET-1"]) == 0 and "by SnapEDA" in capsys.readouterr().out


def test_links_with_no_digikey_key(store_home):
    lines = distributors.link_lines("FAKE-FET-1")
    assert "Direct links (Source: DigiKey):" not in lines and any("Findchips" in line for line in lines)


def test_review_fixes(api, store_home, monkeypatch):
    """A name of more categories, a connection that ends before the headers, a
    token that the API does not accept, a short MPN start, the locale of the
    offer cache, a negative offset."""
    with pytest.raises(Choose) as e:
        digikey.find_category("Accessories")
    assert '  "Accessories" (ID 30): Fans > Accessories, 5 parts [the name is not unique: give the ID]' in e.value.lines
    assert e.value.lines[-1].startswith("For example: datasheet-lens pick digikey 30 -P")
    api["fail"] = [urllib.error.URLError(http.client.RemoteDisconnected("closed"))]
    assert digikey.parameter_lines("100")[0].startswith("Source: DigiKey")
    api["fail"] = [urllib.error.URLError(OSError("no route"))]
    with pytest.raises(Unavailable, match="no connection"):  # no retry: not an early end
        digikey.call("GET", "/search/categories")
    # A token that the API does not accept: one new token, then the request again.
    tokens = sum(1 for url, _, _ in api["sent"] if url == digikey.TOKEN_URL)
    api["fail"] = [401]
    assert digikey.call("GET", "/search/categories")
    assert sum(1 for url, _, _ in api["sent"] if url == digikey.TOKEN_URL) == tokens + 1
    with pytest.raises(LensError, match="3 or more characters"):
        digikey.offers("FA", exact=False)
    api["products"] = {"FAKE-FET-1": _product("FAKE-FET-1")}
    assert digikey.offers("FAKE-FET-1")[0].prices[0] == (1, "12.5 USD")
    assert main(["dist", "--currency", "CZK"]) == 0
    assert digikey.offers("FAKE-FET-1")[0].prices[0] == (1, "12.5 CZK")  # not the cached USD answer
    with pytest.raises(LensError, match="offset: 0 or more"):
        digikey.search("100", [], offset=-5)
    assert main(["pick", "digikey", "100", "-P", "vdss >= 20 V"]) == 2


def test_digikey_key_but_not_enabled(api, store_home):
    from datasheet_lens.setup import save_config

    save_config({"distributors": ["mouser"]})
    with pytest.raises(LensError, match="DigiKey has a key but is not enabled") as e:
        distributors.pick_lines("digikey", "100", [])
    assert '"datasheet-lens dist mouser digikey"' in str(e.value) and "get a key" not in str(e.value)


def test_each_key_is_looked_up_once(api, store_home, monkeypatch):
    """A locked key store waits for each lookup: one lookup of each key."""
    from datasheet_lens import keys

    seen = []
    lookup = keys._lookup
    monkeypatch.setattr(keys, "_lookup", lambda key: seen.append(key) or lookup(key))
    distributors.status_lines()
    assert sorted(seen) == ["digikey-id", "digikey-secret", "mouser"]
    seen.clear()
    distributors.keys_of()
    assert sorted(seen) == ["digikey-id", "digikey-secret", "mouser"]


def test_key_tells_when_the_distributor_is_not_in_the_list(store_home, monkeypatch, capsys):
    """A distributor list that the user set before does not get DigiKey: the
    command key tells how to add it."""
    import io

    from datasheet_lens.setup import save_config

    save_config({"distributors": ["mouser"]})
    monkeypatch.setattr("sys.stdin", io.StringIO("id-1\nsecret-1\n"))
    assert main(["key", "digikey"]) == 0
    assert 'run "datasheet-lens dist mouser digikey" to use it' in capsys.readouterr().out
    save_config({})  # no list: the distributors with a key, DigiKey too
    monkeypatch.setattr("sys.stdin", io.StringIO("id-1\nsecret-1\n"))
    assert main(["key", "digikey"]) == 0
    assert "not in the distributor list" not in capsys.readouterr().out


def test_search_marks_parts_that_are_not_orderable(api):
    """The parametric search gives the same marks as search_distributors."""
    product = dict(PRODUCT, ProductStatus={"Status": "Obsolete"})
    line = digikey.product_line(product, [], "USD")
    assert "state: Obsolete [NOT ORDERABLE: Obsolete]" in line
    line = digikey.product_line(dict(PRODUCT, ProductStatus={"Status": "Discontinued at Digi-Key"}), [], "USD")
    assert "[DigiKey does not sell it now" in line and "NOT ORDERABLE" not in line


def _second_source(mpn, maker, number, stock):
    """A product of the MPN from a different manufacturer (a second source)."""
    return dict(_product(mpn, stock=stock), Manufacturer={"Id": sum(map(ord, maker)), "Name": maker},
                ProductUrl=f"https://www.digikey.com/p/{number}",
                ProductVariations=[{"DigiKeyProductNumber": number, "PackageType": {"Name": "Cut Tape (CT)"}}])


def test_an_mpn_of_more_manufacturers(api, store_home, tmp_path):
    """DigiKey gives HTTP 404 in productdetails for an MPN of more products: the
    keyword search gives one offer for each manufacturer (a live call,
    2026-11-07: an MPN of four manufacturers was "not at DigiKey")."""
    from datasheet_lens import adopt
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    api["products"] = {"DUP-1": [_second_source("DUP-1", "Maker One", "1-DUP-1-CT-ND", 70),
                                 _second_source("DUP-1", "Maker Two", "2-DUP-1-CT-ND", 5),
                                 _second_source("DUP-1Y", "Maker One", "1-DUP-1Y-CT-ND", 9)]}
    assert [(o.manufacturer, o.distributor_pn) for o in digikey.offers("dup-1")] == [
        ("Maker One", "1-DUP-1-CT-ND"), ("Maker Two", "2-DUP-1-CT-ND")]
    assert [(o.mpn, o.manufacturer) for o in digikey.offers("DUP", exact=False)] == [
        ("DUP-1", "Maker One"), ("DUP-1", "Maker Two"), ("DUP-1Y", "Maker One")]
    # The number of the distributor in each line: adopt_part takes it.
    text = "\n".join(distributors.search_lines("DUP-1", exact=True))
    assert "DUP-1 (Maker Two); DigiKey 2-DUP-1-CT-ND; state: Active" in text
    # The direct links of each manufacturer, with its name.
    lines = distributors.link_lines("DUP-1")
    assert "Direct links (Source: DigiKey, Maker One):" in lines and "Direct links (Source: DigiKey, Maker Two):" in lines
    assert any(url.endswith("/search/2-DUP-1-CT-ND/media") for url, _, _ in api["sent"])
    # The availability of the manufacturer of the project part.
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "DUP-1", manufacturer="Maker Two")
        lines = distributors.availability(prj.load(project.root), store)
    assert lines[1] == "DUP-1: Active, stock 5"
    # adopt: the MPN of more manufacturers asks for the number; an MPN is not a number.
    with pytest.raises(LensError, match="DigiKey has more parts for DUP-1. Give the number of one: 1-DUP-1-CT-ND "
                                        r"\(DUP-1, Maker One\); 2-DUP-1-CT-ND \(DUP-1, Maker Two\)"):
        adopt._part("digikey", "", "DUP-1")
    with pytest.raises(LensError, match="'DUP-1' is not a DigiKey number of a product: give an MPN as mpn"):
        adopt._part("digikey", "DUP-1", "")
    with pytest.raises(LensError, match="DigiKey has no part 9-NO-ND"):
        adopt._part("digikey", "9-NO-ND", "")
    part = adopt._part("digikey", "2-dup-1-ct-nd", "")
    assert (part["mpn"], part["manufacturer"], part["number"]) == ("DUP-1", "Maker Two", "2-DUP-1-CT-ND")
    assert adopt._part("digikey", "", "DUP-1Y")["number"] == "1-DUP-1Y-CT-ND"


def test_two_products_of_one_manufacturer(api, store_home, tmp_path, monkeypatch):
    """DigiKey can have two products of one MPN and one manufacturer (a product
    of a marketplace seller): two offers, each with its DigiKey number and its
    stock. The direct links come from one product of the manufacturer."""
    from datasheet_lens import adopt
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    api["products"] = {"TWO-1": [_second_source("TWO-1", "Maker One", "1-TWO-1-CT-ND", 70),
                                 _second_source("TWO-1", "Maker One", "2156-TWO-1-ND", 30)]}
    assert [o.distributor_pn for o in digikey.offers("TWO-1")] == ["1-TWO-1-CT-ND", "2156-TWO-1-ND"]
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "TWO-1", manufacturer="Maker One")
        assert distributors.availability(prj.load(project.root), store)[1] == "TWO-1: Active, stock 100"
    with pytest.raises(LensError, match="DigiKey has more parts for TWO-1"):
        adopt._part("digikey", "", "TWO-1")
    lines = distributors.link_lines("TWO-1")
    assert "Direct links (Source: DigiKey):" in lines
    assert sum(1 for url, _, _ in api["sent"] if url.endswith("/media")) == 1
    # An error of one request: the links of the manufacturers before it stay.
    api["products"] = {"DUP-2": [_second_source("DUP-2", "Maker One", "1-DUP-2-CT-ND", 7),
                                 _second_source("DUP-2", "Maker Two", "2-DUP-2-CT-ND", 5)]}
    real = digikey.call

    def call(method, path, *args, **kwargs):
        if path.endswith("/2-DUP-2-CT-ND/media"):
            raise Unavailable("DigiKey API: HTTP 503 Service Unavailable")
        return real(method, path, *args, **kwargs)

    monkeypatch.setattr(digikey, "call", call)
    lines = digikey.media_lines("DUP-2")
    assert lines[0] == "Direct links (Source: DigiKey, Maker One):" and len(lines) == 4
    assert lines[3] == "Direct links (Source: DigiKey, Maker Two): not checked: DigiKey API: HTTP 503 Service Unavailable"


def test_a_filter_that_digikey_does_not_apply(api, monkeypatch):
    """DigiKey gives no part for a filter on some parameters (their value IDs
    are counts), although it gives parts for their values (a live call,
    2026-11-07). A search with no part checks each filter alone, and searches
    again without the filters that DigiKey does not apply."""
    channels = dict(_filter(12, "Unidirectional Channels", [("1", "1"), ("2", "2")]))
    channels["FilterValues"] = [dict(v, ProductCount=n) for v, n in zip(channels["FilterValues"], (40, 3))]
    monkeypatch.setitem(globals(), "FILTERS", {"ProductsCount": 25, "FilterOptions": {
        "ParametricFilters": FILTERS["FilterOptions"]["ParametricFilters"] + [channels]}})
    api["ignored"] = {12}
    result = digikey.search("100", ["unidirectional channels = 1", "vdss >= 20 V"])
    assert result.count == 1 and [r.status for r in result.filters] == ["not_applied", "server"]
    searches = _searches(api)
    assert [[f["ParameterId"] for f in s["FilterOptionsRequest"]["ParameterFilterRequest"]["ParameterFilters"]]
            for s in searches] == [[12, 6], [12], [6]]  # the search, the check, the search again
    # (The values of Vdss have no number of parts here: no check.)
    text = "\n".join(digikey.search_lines(result))
    assert ('filter "unidirectional channels = 1": NOT APPLIED. DigiKey gives no part for a filter on "Unidirectional '
            'Channels", although it gives 40 parts with its values (a fault of the DigiKey API).') in text
    assert "1 parts agree with the applied filters" in text
    # The value of the part does not agree with the filter (PRODUCT has no such value).
    assert text.count('[NOT AGREE: filter "unidirectional channels = 1": no value]') == 1
    result.products = [dict(PRODUCT, Parameters=PRODUCT["Parameters"] + [
        {"ParameterText": "Unidirectional Channels", "ValueText": "1"}])]
    text = "\n".join(digikey.search_lines(result))
    assert "Unidirectional Channels: 1" in text and "NOT AGREE" not in text
    # The next search knows it: no search with no part, no check.
    sent = len(_searches(api))
    assert digikey.search("100", ["unidirectional channels = 1"]).count == 1
    assert len(_searches(api)) == sent + 1
    # A filter that DigiKey applies, and that gives no part with the others,
    # stays. The check of it stays in the cache too: no second check.
    api["ignored"], api["no_part_for_more"] = set(), True
    digikey._not_applied_cache.clear()
    for _ in range(2):
        result = digikey.search("100", ["unidirectional channels = 2", "vdss >= 20 V"])
        assert result.count == 0 and [r.status for r in result.filters] == ["server", "server"]
    searches = _searches(api)[-3:]
    assert [[f["ParameterId"] for f in s["FilterOptionsRequest"]["ParameterFilterRequest"]["ParameterFilters"]]
            for s in searches] == [[12, 6], [12], [12, 6]]


def test_availability_of_the_digikey_purchase_source(api, store_home, tmp_path):
    """A part with a DigiKey purchase source: the product of its number."""
    from datasheet_lens import project as prj
    from datasheet_lens.store import Store

    api["products"] = {"DUP-1": [_second_source("DUP-1", "Maker One", "1-DUP-1-CT-ND", 70),
                                 _second_source("DUP-1", "Maker Two", "2-DUP-1-CT-ND", 5)]}
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "DUP-1")
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "digikey", "2-dup-1-ct-nd")
    lines = distributors.availability(prj.load(project.root))
    assert lines[1] == "DUP-1 (2-dup-1-ct-nd, the purchase source): Active, stock 5"


def test_offer_of_one_packaging():
    """The offer of a DigiKey number of a purchase source: the prices, the
    minimum and the stock of its packaging."""
    product = dict(PRODUCT, ProductVariations=[
        {"DigiKeyProductNumber": "1-TR-ND", "PackageType": {"Name": "Tape & Reel (TR)"}, "MinimumOrderQuantity": 3000,
         "QuantityAvailableforPackageType": 0, "StandardPricing": [{"BreakQuantity": 3000, "UnitPrice": 0.1}]},
        {"DigiKeyProductNumber": "1-CT-ND", "PackageType": {"Name": "Cut Tape (CT)"}, "MinimumOrderQuantity": 1,
         "QuantityAvailableforPackageType": 900, "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.5}]}])
    reel = digikey.offer_of(product, "USD", "1-tr-nd")
    assert (reel.distributor_pn, reel.stock, reel.minimum, reel.prices) == ("1-TR-ND", 0, 3000, [(3000, "0.1 USD")])
    assert digikey.offer_of(product, "USD").distributor_pn == "1-CT-ND"
    with pytest.raises(LensError, match="has no packaging 9-ND"):
        digikey.offer_of(product, "USD", "9-ND")
