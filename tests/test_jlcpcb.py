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
     {"Type": "N-Channel", "Drain to Source Voltage": "30V", "RDS(on)": "4mΩ@4.5V, 6mΩ@2.5V"}),
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
    assert "Library (short name library): 2 values: Basic | Extended" in lines
    assert any(line.startswith("RDS(on) (short name rds_on): 2 values") for line in lines)


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
    assert "Source: JLCPCB parts library (stock for the JLCPCB assembly)" in lines
    assert "FET-A: JLCPCB stock 500, library Extended (C1001)" in lines
    assert ("FET-C: JLCPCB stock 0, library Extended (C1003). WARNING: NOT IN STOCK at JLCPCB: the assembly "
            "needs an order of the part") in lines
    assert "NO-PART: WARNING: the JLCPCB parts library does not have this MPN" in lines


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
    assert any("WARNING: the JLCPCB library has no part from Other Maker: the data is of Maker A, Maker Z" in line
               for line in lines)
    monkeypatch.setattr(jlcpcb, "offers", broken)
    assert "Source: JLCPCB parts library. Not checked: database disk image is malformed" in \
        distributors.search_lines("FET-A", exact=True)
    assert "FET-A: not checked: database disk image is malformed" in distributors.availability(prj.load(project.root))


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
