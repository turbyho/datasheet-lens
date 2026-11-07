"""The JLCPCB parts library: the download of the split zip, the search, the
offers and the availability. The data is made up: the jlcparts data has no
license statement."""

import json
import os
import sqlite3
import struct
import threading
import time
import zlib

import pytest

from datasheet_lens import distributors, jlcpcb
from datasheet_lens import project as prj
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.store import Store

COLUMNS = ("lcsc", "present", "category", "subcategory", "mfr", "package", "manufacturer", "library_type",
           "preferred", "description", "datasheet", "stock", "price", "attributes")
PARTS = [
    # lcsc, category, subcategory, mfr, package, manufacturer, library, preferred, stock, price, attributes
    (1001, "Transistors/Thyristors", "MOSFETs", "FET-A", "DFN3x3-8", "Maker A", "expand", 0, 500, "1-9:0.5,10-:0.4",
     {"Type": "N-Channel", "Drain to Source Voltage": "30V", "RDS(on)": "4mΩ@4.5V, 6mΩ@2.5V",
      "Collector Current (Ic)": "1A"}),  # a parameter of one part (of an IGBT in this category)
    (1002, "Transistors/Thyristors", "MOSFETs", "FET-B", "SOT-23", "Maker B", "base", 0, 9000, "1-49:0.05",
     {"type": "N-Channel", "Drain to Source Voltage": "30V", "RDS(on)": "30mΩ@4.5V"}),
    (1003, "Transistors/Thyristors", "MOSFETs", "FET-C", "DFN3x3-8", "Maker C", "expand", 0, 0, "1-9:0.3",
     {"Type": "N-Channel", "Drain to Source Voltage": "25V", "RDS(on)": "5mΩ@2.5V"}),
    (1004, "Transistors", "MOSFET", "FET-D", "SOT-23", "Maker D", "expand", 1, 70, "1-9:0.2",
     {"Type": "P-Channel", "Drain to Source Voltage": "20V", "RDS(on)": "50mΩ@4.5V,2A"}),
    (1005, "Transistors/Thyristors", "Bipolar (BJT)", "BJT-E", "SOT-23", "Maker E", "base", 0, 100, "1-9:0.01", {}),
]


def make_db(path) -> bytes:
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE jlc_components ({', '.join(COLUMNS)})")
    for lcsc, cat, sub, mfr, pkg, maker, lib, pref, stock, price, attrs in PARTS:
        db.execute(f"INSERT INTO jlc_components VALUES ({', '.join('?' * len(COLUMNS))})",
                   (lcsc, 1, cat, sub, mfr, pkg, maker, lib, pref, f"{mfr} description", f"https://x/{mfr}.pdf",
                    stock, price, json.dumps(attrs)))
    db.commit()
    db.close()
    return path.read_bytes()


def split_zip(payload: bytes, part_size: int, name: str = "cache.sqlite3") -> dict[str, bytes]:
    """The files of a split zip (zip -s) with one deflated file: cache.z01 ... and cache.zip."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = deflate.compress(payload) + deflate.flush()
    crc = zlib.crc32(payload)
    local = struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 8, 0, 0, crc, len(data), len(payload), len(name),
                        0) + name.encode()
    body = b"PK\x07\x08" + local + data  # the split signature, then the file from offset 4
    chunks = [body[i:i + part_size] for i in range(0, len(body), part_size)]
    others, last = chunks[:-1], chunks[-1]
    cd = struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 798, 20, 0, 8, 0, 0, crc, len(data), len(payload),
                     len(name), 0, 0, 0, 0, 0, 4) + name.encode()
    end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", len(others), len(others), 1, 1, len(cd), len(last), 0)
    files = {f"cache.z{i + 1:02d}": c for i, c in enumerate(others)}
    files["cache.zip"] = last + cd + end
    return files


@pytest.fixture
def jlc(store_home, tmp_path, monkeypatch):
    """A JLCPCB database from the made-up parts, through the download code."""
    jlcpcb._job.clear()
    jlcpcb._job["state"] = "idle"
    payload = make_db(tmp_path / "source.sqlite3")
    files = split_zip(payload, part_size=150)
    assert len(files) > 2  # the data goes over more parts
    monkeypatch.setattr(jlcpcb, "_get", lambda url: files[url.rsplit("/", 1)[1]])

    def opener(url):
        data = files[url.rsplit("/", 1)[1]]
        for i in range(0, len(data), 64):
            yield data[i:i + 64]

    lines = []
    state = jlcpcb.update(progress=lines.append, opener=opener)
    jlcpcb._categories.clear()
    return {"files": files, "opener": opener, "state": state, "lines": lines, "payload": payload}


def test_update_unpacks_the_split_zip(jlc):
    # The size of the unpacked file is the size of the source; the indexes come after the check.
    assert jlc["state"]["size"] == len(jlc["payload"]) and "add the indexes" in jlc["lines"]
    db = sqlite3.connect(jlcpcb.db_path())
    assert db.execute("SELECT COUNT(*) FROM jlc_components").fetchone()[0] == len(PARTS)
    indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"dl_mfr", "dl_subcategory"} <= indexes
    db.close()
    assert not list(jlcpcb.folder().glob("*.tmp"))
    assert jlcpcb.state()["age_days"] < 0.01 and jlcpcb.age_note() == ""


def test_a_wrong_download_keeps_the_old_database(jlc, monkeypatch):
    before = jlcpcb.db_path().read_bytes()
    files = dict(jlc["files"])
    broken = bytearray(files["cache.z01"])
    broken[-1] ^= 0xFF  # one byte of the compressed data
    files["cache.z01"] = bytes(broken)
    monkeypatch.setattr(jlcpcb, "_get", lambda url: files[url.rsplit("/", 1)[1]])
    with pytest.raises((LensError, zlib.error)):
        jlcpcb.update(progress=lambda _: None, opener=lambda url: iter([files[url.rsplit("/", 1)[1]]]))
    assert jlcpcb.db_path().read_bytes() == before
    assert not list(jlcpcb.folder().glob("*.tmp"))


def test_search_separates_stock_and_library(jlc):
    result = jlcpcb.search("MOSFETs", ["channel = N-Channel", "rds_on <= 10 mOhm at Vgs<=2.5V"])
    assert [r["mfr"] for r in result["rows"]] == ["FET-A"]  # FET-C has no stock
    text = "\n".join(jlcpcb.search_lines(result))
    assert "Category: Transistors/Thyristors > MOSFETs." in text
    assert "Other categories with a name that holds the text" not in text  # "MOSFET" does not hold "mosfets"
    assert "(1 parts give no value at Vgs<=2.5V (read it in their datasheet))" in text  # FET-B: 4.5 V only
    assert ("C1001 FET-A (Maker A); JLCPCB stock: 500; library: Extended; price 1-9: 0.5 USD; Type: N-Channel; "
            "RDS(on): 4mΩ@4.5V, 6mΩ@2.5V; Package: DFN3x3-8") in text
    assert "https://jlcpcb.com/partdetail/C1001" in text and "no license statement" in text
    # With the parts that have no stock: the mark NOT IN STOCK. The type "type" counts as "Type".
    result = jlcpcb.search("MOSFETs", ["channel = N-Channel"], in_stock=False, sort="price")
    assert [r["mfr"] for r in result["rows"]] == ["FET-B", "FET-C", "FET-A"]
    text = "\n".join(jlcpcb.search_lines(result))
    assert "C1003 FET-C (Maker C); JLCPCB stock: 0 (NOT IN STOCK: order needed); library: Extended" in text
    assert "library: Basic" in text
    result = jlcpcb.search("MOSFET|MOSFETs", ["library = Basic|Preferred"])
    assert sorted(r["mfr"] for r in result["rows"]) == ["FET-B", "FET-D"]


def test_categories(jlc):
    result = jlcpcb.search("mosfet", [])  # the exact subcategory name "MOSFET"
    assert [c["subcategory"] for c in result["categories"]] == ["MOSFET"]
    assert "Transistors/Thyristors > MOSFETs (2 in stock of 3)" in "\n".join(jlcpcb.search_lines(result))
    with pytest.raises(LensError, match="fits more JLCPCB categories") as e:
        jlcpcb.search("transistors", [])
    assert '  "MOSFETs": 2 in stock of 3 (in Transistors/Thyristors)' in e.value.lines
    assert e.value.lines[-1].startswith('For example: datasheet-lens pick jlcpcb "MOSFETs" -P')
    with pytest.raises(LensError, match="no JLCPCB category 'capacitor'"):
        jlcpcb.search("capacitor", [])
    with pytest.raises(LensError, match="the condition needs a number"):
        jlcpcb.search("MOSFETs", ["rds_on <= 10 mOhm at low voltage"])
    lines = jlcpcb.parameter_lines("MOSFETs")
    assert "Library (short name library): 2 parts, 2 values: Basic | Extended" in lines
    assert any(line.startswith("RDS(on) (short name rds_on): 2 parts, 2 values") for line in lines)
    assert lines[-1] == "Collector Current (Ic): 1 parts, 1 values: 1A"  # the parameters of most parts first


def test_offers_search_lines_and_availability(jlc, tmp_path):
    [offer] = jlcpcb.offers("fet-a")
    assert (offer.source, offer.stock, offer.library, offer.distributor_pn) == ("JLCPCB", 500, "Extended", "C1001")
    assert offer.prices == [(1, "0.5 USD"), (10, "0.4 USD")]
    assert [o.mpn for o in jlcpcb.offers("FET", exact=False)] == ["FET-B", "FET-A", "FET-D", "FET-C"]
    with pytest.raises(LensError, match="3 or more characters"):
        jlcpcb.offers("FE", exact=False)
    text = "\n".join(distributors.search_lines("FET-C", exact=True))
    assert "Source: JLCPCB parts library (1 parts)" in text
    assert "stock: 0 (NOT IN STOCK at JLCPCB: the assembly needs an order of the part)" in text
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("FET-A", "FET-C", "NO-PART"):
            prj.add_part(project, store, mpn)
    lines = distributors.availability(prj.load(project.root))
    assert "FET-A: orderable (JLCPCB). JLCPCB: stock 500, library Extended (C1001)" in lines
    assert lines[lines.index("FET-C: WARNING: no stock at any source") + 1] == (
        "  JLCPCB: stock 0, library Extended (C1003). WARNING: NOT IN STOCK at JLCPCB: the assembly needs an order "
        "of the part")
    assert lines[lines.index("NO-PART: WARNING: no source has this MPN (a base part, or a wrong MPN?)") + 1] == (
        "  JLCPCB: does not have this MPN")


def test_source_is_required_and_cli(jlc, capsys):
    """The search always names its source: the categories and the stock differ."""
    assert distributors.pick_lines("jlcpcb", "MOSFETs", [])[0].startswith("Source: JLCPCB parts library")
    with pytest.raises(LensError, match="give the source of the search: digikey or jlcpcb"):
        distributors.pick_lines("", "MOSFETs", [])
    with pytest.raises(LensError, match="datasheet-lens key digikey"):  # no DigiKey key
        distributors.pick_lines("digikey", "MOSFETs", [])
    with pytest.raises(SystemExit):  # argparse: the source is a required argument
        main(["pick", "MOSFETs", "vdss >= 25 V"])
    assert main(["pick", "jlcpcb", "MOSFETs", "vdss >= 25 V", "-a"]) == 0
    assert "FET-C" in capsys.readouterr().out
    assert main(["pick", "jlcpcb", "MOSFETs", "-P"]) == 0
    assert "short name rds_on" in capsys.readouterr().out
    assert main(["jlc"]) == 0 and "days old" in capsys.readouterr().out
    assert "jlcpcb: the local database is 0.0 days old" in "\n".join(distributors.status_lines())
    assert main(["jlc", "remove"]) == 0 and "removed" in capsys.readouterr().out
    with pytest.raises(LensError, match="not on this machine"):
        distributors.pick_lines("jlcpcb", "MOSFETs", [])
    assert main(["jlc"]) == 0 and "no JLCPCB database" in capsys.readouterr().out


def test_age_note(jlc, monkeypatch):
    state = json.loads((jlcpcb.folder() / jlcpcb.STATE_NAME).read_text())
    state["downloaded"] = time.time() - 3 * 86400
    (jlcpcb.folder() / jlcpcb.STATE_NAME).write_text(json.dumps(state))
    assert "is 3.0 days old" in jlcpcb.age_note()
    assert jlcpcb.search_lines(jlcpcb.search("MOSFETs", []))[-1].startswith("Note: the JLCPCB database is 3.0")


def test_review_fixes(jlc, tmp_path, monkeypatch):
    """A central directory in an earlier part, an error of the database, the
    manufacturer of a project part, the name jlcpcb in the distributor list,
    and the search links with no distributor key."""
    last = bytearray(jlc["files"]["cache.zip"])
    end = last.rfind(b"PK\x05\x06")
    last[end + 6:end + 8] = struct.pack("<H", 0)  # the disk of the central directory
    with pytest.raises(LensError, match="starts in an earlier part"):
        jlcpcb._entry(bytes(last))
    with pytest.raises(LensError, match="not a distributor of this list"):
        distributors.check_names(["mouser", "jlcpcb"])
    lines = distributors.search_lines("FET-A", exact=True)
    assert "Source: JLCPCB parts library (1 parts)" in lines and "Search links:" in lines

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    # A second source of FET-A: the MPN has parts of two manufacturers, none is the maker of the project part.
    db = sqlite3.connect(jlcpcb.db_path())
    db.execute("INSERT INTO jlc_components (lcsc, present, category, subcategory, mfr, package, manufacturer, "
               "library_type, preferred, description, datasheet, stock, price, attributes) VALUES (1006, 1, "
               "'Transistors/Thyristors', 'MOSFETs', 'FET-A', 'SOT-23', 'Maker Z', 'expand', 0, '', '', 5, '', '{}')")
    db.commit()
    db.close()
    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        prj.add_part(project, store, "FET-A", manufacturer="Other Maker")
        lines = distributors.availability(prj.load(project.root), store)
    # The offers of other manufacturers are not offers of the part.
    assert lines[:2] == ["FET-A: WARNING: no offer of the manufacturer of the part (JLCPCB)",
                         "  JLCPCB: stock 500, library Extended (C1001). WARNING: the JLCPCB library has no part from "
                         "Other Maker: the data is of Maker A, Maker Z"]
    monkeypatch.setattr(jlcpcb, "offers", broken)
    assert "Source: JLCPCB parts library. Not checked: database disk image is malformed" in \
        distributors.search_lines("FET-A", exact=True)
    assert distributors.availability(prj.load(project.root))[:2] == [
        "FET-A: not checked", "  JLCPCB: not checked: database disk image is malformed"]


def _wait(seconds: float = 10.0) -> dict:
    deadline = time.time() + seconds
    while jlcpcb.job()["state"] == "running":
        assert time.time() < deadline, f"the update job still runs after {seconds} s"
        time.sleep(0.02)
    return jlcpcb.job()


def test_update_in_the_background_with_progress(jlc, monkeypatch):
    """The MCP tool starts the update and answers at once; status gives the progress and the result."""
    from datasheet_lens import server

    files = jlc["files"]
    gate = threading.Event()

    def slow_opener(url):
        gate.wait(5)  # the job stays "running" until the test opens the gate
        yield files[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(jlcpcb, "_stream", slow_opener)
    try:
        out = server.jlcpcb_library("update")
        assert "no license statement" in out and "update: running for" in out
        assert "update: running for" in server.jlcpcb_library("update")  # no second job
    finally:
        gate.set()  # never leave the job thread waiting into a later test
    assert _wait()["state"] == "done"
    assert "update: done in" in server.jlcpcb_library("status")

    def failing(url):
        raise LensError("JLCPCB database: HTTP 404 Not Found")
        yield b""

    monkeypatch.setattr(jlcpcb, "_stream", failing)
    server.jlcpcb_library("update")
    assert _wait()["state"] == "failed"
    out = server.jlcpcb_library("status")
    assert "update: FAILED: LensError: JLCPCB database: HTTP 404" in out and "days old" in out  # the old one stays
    with pytest.raises(Exception, match="action: status or update"):
        server.jlcpcb_library("download")


def test_set_price_locale(store_home):
    from datasheet_lens import digikey, server

    assert "global: site CZ, currency CZK" in server.set_price_locale("cz", "czk")
    assert digikey.locale() == ("CZ", "CZK")
    assert "currency EUR" in server.set_price_locale(currency="eur") and digikey.locale() == ("CZ", "EUR")
    with pytest.raises(Exception, match="a country code of 2 letters"):
        server.set_price_locale("CZE")
    with pytest.raises(Exception, match="give the site"):
        server.set_price_locale()



def test_temporary_files_of_other_updates(jlc):
    """Each update writes its own temporary file. A file of a running update of
    a different process stays; a file of a stopped update (older than one hour)
    goes at the next update."""
    folder = jlcpcb.folder()
    running = folder / "jlcpcb.sqlite3.999.1.tmp"
    running.write_bytes(b"x")
    assert "an update of a different process (the CLI or a second MCP server) runs now" in \
        "\n".join(jlcpcb.status_lines())
    stopped = folder / "jlcpcb.sqlite3.998.1.tmp"
    stopped.write_bytes(b"x")
    old = time.time() - 2 * jlcpcb.STALE_SECONDS
    os.utime(stopped, (old, old))
    jlcpcb.update(progress=lambda _: None, opener=jlc["opener"])
    assert running.exists() and not stopped.exists()
    os.utime(running, (old, old))
    assert "an update that stopped (the next update deletes them)" in "\n".join(jlcpcb.status_lines())
    assert jlcpcb.remove() and not list(folder.glob("*.tmp"))


def test_availability_of_the_jlcpcb_purchase_source(jlc, tmp_path):
    """A part with a JLCPCB purchase source: the part of its C number. A C
    number of a different MPN gives a warning."""
    from datasheet_lens import project as prj

    with Store() as store:
        project = prj.init_project(tmp_path / "board")
        for mpn in ("FET-A", "FET-C"):
            prj.add_part(project, store, mpn)
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C1001", "Extended")
    prj.set_source(project, project.parts[1], "jlcpcb", "C1002", "Basic")  # the C number of FET-B
    lines = distributors.availability(prj.load(project.root))
    assert ("FET-A (C1001, the purchase source): orderable at the purchase source (JLCPCB). JLCPCB: stock 500, "
            "library Extended (C1001)") in lines
    # A C number of a different MPN: its stock is not the stock of the part.
    assert lines[lines.index("FET-C (C1002, the purchase source): WARNING: the purchase source cannot deliver "
                             "(JLCPCB: C1002 is an other MPN). No other source has stock") + 1] == (
        "  JLCPCB: stock 9000, library Basic (C1002). WARNING: C1002 is FET-B (Maker B), not FET-C")
    assert jlcpcb.offer_of_number("c1001").mpn == "FET-A" and jlcpcb.offer_of_number("C9999") is None
    with pytest.raises(LensError, match="not a JLCPCB number"):
        jlcpcb.offer_of_number("1001")


RESISTORS = "Chip Resistor - Surface Mount"
MLCC = "Multilayer Ceramic Capacitors MLCC - SMD/SMT"


def _add(db, lcsc, sub, mfr, pkg, lib, pref, stock, attrs, maker="Maker R"):
    db.execute(f"INSERT INTO jlc_components VALUES ({', '.join('?' * len(COLUMNS))})",
               (lcsc, 1, "Resistors" if sub == RESISTORS else "Capacitors", sub, mfr, pkg, maker, lib, pref,
                "", "", stock, "1-:0.01", json.dumps(attrs)))


def test_replacements(jlc, tmp_path):
    """The replacements of a part: the same MPN with an other C number, and for a
    passive part the same package and parameters (not worse). Basic and
    Preferred first. For a part with stock, only parts with no loading fee."""
    r = {"Resistance": "10kΩ", "Tolerance": "±5%", "Power(Watts)": "62.5mW", "Temperature Coefficient": "±100ppm/℃"}
    db = sqlite3.connect(jlcpcb.db_path())
    _add(db, 2001, RESISTORS, "R-EXT", "0402", "expand", 0, 5000, r)                       # the part
    _add(db, 2002, RESISTORS, "R-BASIC", "0402", "base", 0, 900, dict(r, Tolerance="±1%"))  # better: yes
    _add(db, 2003, RESISTORS, "R-PREF", "0402", "expand", 1, 99000, r)                     # Preferred: yes
    _add(db, 2004, RESISTORS, "R-WORSE", "0402", "base", 0, 99000, dict(r, Tolerance="±10%"))  # worse tolerance
    _add(db, 2005, RESISTORS, "R-0603", "0603", "base", 0, 99000, r)                       # other package
    _add(db, 2006, RESISTORS, "R-NOPWR", "0402", "base", 0, 99000,
         {k: v for k, v in r.items() if k != "Power(Watts)"})                              # cannot verify
    _add(db, 2007, RESISTORS, "R-OTHER", "0402", "base", 0, 99000, dict(r, Resistance="10Ω"))  # other value
    _add(db, 2008, RESISTORS, "R-EXT", "0402", "base", 0, 10, r, maker="Maker S")          # the same MPN
    _add(db, 2009, RESISTORS, "R-EXT2", "0402", "expand", 0, 99000, r)                     # Extended: no
    c = {"Capacitance": "100nF", "Voltage Rating": "16V", "Temperature Coefficient": "X7R", "Tolerance": "±10%"}
    _add(db, 3001, MLCC, "C-EXT", "0402", "expand", 0, 0, c)                               # no stock
    _add(db, 3002, MLCC, "C-50V", "0402", "base", 0, 10, dict(c, **{"Voltage Rating": "50V"}))   # higher voltage
    _add(db, 3003, MLCC, "C-X5R", "0402", "base", 0, 9999, dict(c, **{"Temperature Coefficient": "X5R"}))
    _add(db, 3004, MLCC, "C-10V", "0402", "base", 0, 9999, dict(c, **{"Voltage Rating": "10V"}))
    _add(db, 3005, MLCC, "C-EXT3", "0402", "expand", 0, 50, c)                             # Extended: yes (no stock)
    db.commit()
    db.close()
    found = [(o.distributor_pn, o.library, why) for o, why in jlcpcb.replacements("C2001", limit=10)]
    assert found == [("C2002", "Basic", "the same package and parameters"), ("C2008", "Basic", "the same MPN"),
                     ("C2003", "Preferred", "the same package and parameters")]
    assert [o.distributor_pn for o, _ in jlcpcb.replacements("C3001", limit=10)] == ["C3002", "C3005"]
    assert jlcpcb.replacements("C1001") == []  # a MOSFET: only the same MPN, and there is none
    assert jlcpcb.replacements("C9999") == []
    # In the availability: the replacements of a part with no stock or with a loading fee.
    from datasheet_lens import project as prj

    project = prj.init_project(tmp_path / "board")
    with Store() as store:
        prj.add_part(project, store, "C-EXT")
    lines = distributors.availability(prj.load(project.root))
    assert ("  replacement: C3002 C-50V (Maker R), library Basic, JLCPCB stock 10, 0402 (the same package and "
            "parameters): read its datasheet before you use it") in lines


def test_obsolete_part_with_jlcpcb_stock(jlc, tmp_path, monkeypatch):
    """A part that a distributor gives as obsolete gets the warning and the
    replacements also when JLCPCB has it in stock. The verdict gives the state
    of each distributor. The same MPN of the same manufacturer name is no
    replacement; the same MPN of an other name is one, with a note; an Extended
    part helps too."""
    from datasheet_lens import project as prj
    from datasheet_lens.distributors import Offer

    r = {"Resistance": "10kΩ", "Tolerance": "±5%", "Power(Watts)": "62.5mW", "Temperature Coefficient": "±100ppm/℃"}
    db = sqlite3.connect(jlcpcb.db_path())
    _add(db, 2001, RESISTORS, "R-EXT", "0402", "expand", 0, 5000, r)                       # the part
    _add(db, 2002, RESISTORS, "R-BASIC", "0402", "base", 0, 900, r)                        # a replacement
    _add(db, 2008, RESISTORS, "R-EXT", "0402", "base", 0, 10, r, maker="Maker S")          # an other name
    _add(db, 2009, RESISTORS, "R-EXT2", "0402", "expand", 0, 99000, r)                     # Extended: yes
    _add(db, 2010, RESISTORS, "R-EXT", "0402", "base", 0, 700, r)                          # the same name: no
    db.commit()
    db.close()
    found = [(o.distributor_pn, why) for o, why in jlcpcb.replacements("C2001", limit=10, obsolete=True)]
    assert found == [("C2002", "the same package and parameters"),
                     ("C2008", "the same MPN, manufacturer Maker S: its lifecycle state is not checked"),
                     ("C2009", "the same package and parameters")]
    # An other name of the manufacturer of the part (the registry knows it): no replacement.
    assert [o.distributor_pn for o, _ in jlcpcb.replacements(
        "C2001", limit=10, obsolete=True, same_maker=lambda name: name == "Maker S")] == ["C2002", "C2009"]
    # The same search with no risk: the same MPN of each name, and no Extended part (the part has stock).
    assert [o.distributor_pn for o, _ in jlcpcb.replacements("C2001", limit=10)] == ["C2002", "C2010", "C2008"]

    offers = {"mouser": Offer("Mouser", "R-EXT", "Maker R", lifecycle="End of Life", stock=0, distributor_pn="m-1"),
              "digikey": Offer("DigiKey", "R-EXT", "Maker R", lifecycle="Active", stock=0, distributor_pn="d-1")}
    monkeypatch.setattr(distributors, "keys_of", lambda project=None: ({"mouser": "k", "digikey": ("a", "b")}, []))
    monkeypatch.setattr(distributors, "offers_of", lambda name, mpn, exact, credential: [offers[name]])
    project = prj.init_project(tmp_path / "board")
    with Store() as store:
        prj.add_part(project, store, "R-EXT", manufacturer="Maker R")
    project = prj.load(project.root)
    prj.set_source(project, project.parts[0], "jlcpcb", "C2001", "Extended")
    lines = distributors.availability(prj.load(project.root))
    assert lines[:5] == [
        "R-EXT (C2001, the purchase source): WARNING: OBSOLETE at the manufacturer (Mouser: End of Life; DigiKey: "
        "Active). Orderable at the purchase source (JLCPCB). Select a replacement with the user (find_parts with the "
        "values of the datasheet)",
        "  Mouser: End of Life, stock 0. WARNING: not orderable: End of Life",
        "  DigiKey: Active, stock 0. WARNING: no stock",
        "  JLCPCB: stock 5000, library Extended (C2001)",
        "  replacement: C2002 R-BASIC (Maker R), library Basic, JLCPCB stock 900, 0402 (the same package and "
        "parameters): read its datasheet before you use it"]
    assert not any("C2010" in line for line in lines)


def test_tolerance_in_other_units():
    """A tolerance in % and a tolerance in pF do not compare."""
    assert jlcpcb._agrees("worst<=", "±5%", "±1%") and not jlcpcb._agrees("worst<=", "±5%", "±10%")
    assert not jlcpcb._agrees("worst<=", "±5%", "±0.5pF") and not jlcpcb._agrees("worst<=", "±0.5pF", "±1%")
    assert jlcpcb._agrees("worst<=", "-20%~+80%", "±20%") and not jlcpcb._agrees("worst<=", "±20%", "-20%~+80%")


def test_replacements_that_fail_are_told(jlc, tmp_path, monkeypatch):
    """An error of the replacement search is a line, not an empty list."""
    from datasheet_lens import project as prj

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jlcpcb, "replacements", fail)
    project = prj.init_project(tmp_path / "board")
    with Store() as store:
        prj.add_part(project, store, "FET-C")  # no JLCPCB stock
    lines = distributors.availability(prj.load(project.root))
    assert "  replacements of C1003: not checked: database is locked" in lines
