from pathlib import Path

from datasheet_lens import __version__
from datasheet_lens.cli import build_parser, main
from datasheet_lens.config import store_home


def test_info_shows_version_and_store(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("DATASHEET_LENS_HOME", str(tmp_path))
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert str(tmp_path) in out


def test_store_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("DATASHEET_LENS_HOME", raising=False)
    # Path.home() reads HOME on Linux and macOS, USERPROFILE on Windows.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert store_home() == tmp_path / ".datasheet-lens"


def test_data_folder(store_home, tmp_path, make_pdf, capsys):
    """The key "data" of config.json moves db/, blobs/ and tmp/ to a different
    folder. config.json stays in the store folder."""
    import json

    import pytest

    from datasheet_lens import version_check
    from datasheet_lens.config import config_path, data_home, data_lines
    from datasheet_lens.errors import LensError
    from datasheet_lens.setup import save_config
    from datasheet_lens.store import Store

    assert data_home() == store_home
    with Store() as store:  # a database in the store folder: the old location
        store.add_document(make_pdf())
    data = tmp_path / "raid" / "lens"
    save_config({"element_backend": "text", "data": str(data)})
    # A disk that is not mounted: no new empty store on the wrong disk.
    with pytest.raises(LensError, match="does not exist: mount its disk"):
        Store()
    assert main(["info"]) == 2 and not data.exists()
    data.mkdir(parents=True)
    assert data_home() == data
    with Store() as store:
        doc = store.add_document(make_pdf("other.pdf", pages=3))
        assert doc.path.is_relative_to(data / "blobs") and len(store.documents()) == 1
    assert (data / "db" / "lens.db").is_file() and config_path().parent == store_home
    capsys.readouterr()
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert f"data: {data}" in out and "not the database in" in out
    # The MCP server tells the agent one time.
    assert any("not the database in" in n for n in version_check.version_notes())
    assert not any("not the database in" in n for n in version_check.version_notes())
    # The same folder by a different path is not a different folder.
    save_config({"data": str(store_home / ".." / store_home.name)})
    assert data_lines() == []
    for value in ("relative/path", "", 5):
        config_path().write_text(json.dumps({"data": value}), encoding="utf-8")
        with pytest.raises(LensError, match='"data" must be'):
            data_home()
    config_path().write_text("{broken", encoding="utf-8")
    with pytest.raises(LensError, match="cannot read the configuration"):
        Store()


def test_options_between_positionals():
    """The CLI reads an optional positional after an option and an other positional on
    each Python version. Python 3.11, 3.12.0 to 3.12.6 and 3.13.0 refuse it with the
    usual parse (CPython gh-59317)."""
    parse = build_parser().parse_args
    args = parse(["pick", "digikey", "100", "-a", "vdss >= 20 V", "-n", "3"])
    assert (args.category, args.filters, args.all, args.limit) == ("100", ["vdss >= 20 V"], True, 3)
    args = parse(["adopt", "digikey", "-c", "MOSFET", "296-25646-1-ND"])
    assert (args.number, args.category) == ("296-25646-1-ND", "MOSFET")
    args = parse(["page-image", "1", "--max-px", "800", "3"])
    assert (args.page, args.max_px) == (3, 800)
    # The positionals that an option divides: one list.
    assert parse(["check", "a.csv", "--column", "MPN", "b.csv"]).bom == ["a.csv", "b.csv"]
    # A command line that the usual parse reads gives the same result.
    assert parse(["pick", "digikey", "100", "vdss >= 20 V", "-a"]).filters == ["vdss >= 20 V"]
    assert parse(["pick", "digikey", "100"]).filters == []


def test_missing_argument_message(capsys):
    """A positional with nargs "*" is not required in the error message, as on Python
    3.12.7 and later (CPython gh-72795)."""
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["pick", "digikey"])
    assert e.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        "datasheet-lens pick: error: the following arguments are required: CATEGORY")
    with pytest.raises(SystemExit):
        main(["search", "-w"])  # the missing positional first, as on Python 3.14
    assert capsys.readouterr().err.splitlines()[-1] == (
        "datasheet-lens search: error: the following arguments are required: text")


def test_parser_with_remainder_keeps_the_usual_parse():
    """The intermixed parse refuses a positional with nargs REMAINDER: such a parser
    keeps the usual parse (no TypeError for an argument that it does not read)."""
    import argparse

    from datasheet_lens.cli import _ArgumentParser

    for argv in (["a", "--bogus", "b"], ["--flag", "--bogus"], ["--flag", "a", "--bogus"]):
        results = []
        for cls in (_ArgumentParser, argparse.ArgumentParser):
            parser = cls(prog="x")
            parser.add_argument("--flag", action="store_true")
            parser.add_argument("rest", nargs=argparse.REMAINDER)
            args, extras = parser.parse_known_args(argv)
            results.append((vars(args), extras))
        assert results[0] == results[1], argv


def test_positionals_of_the_cli_have_the_rules_of_the_parser():
    """Each positional of the CLI comes from add_argument of its parser (not from a
    group): its required state follows Python 3.14, and no positional has nargs
    REMAINDER."""
    import argparse

    def parsers(parser):
        yield parser
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    yield from parsers(sub)

    for parser in parsers(build_parser()):
        for action in parser._actions:
            if action.option_strings or isinstance(action, argparse._SubParsersAction):
                continue
            assert action.nargs != argparse.REMAINDER, (parser.prog, action.dest)
            assert action.required == (action.nargs not in ("?", "*")), (parser.prog, action.dest)
