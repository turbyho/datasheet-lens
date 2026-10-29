import pytest

from datasheet_lens import agents
from datasheet_lens.cli import main


def test_block_in_agent_files(tmp_path, capsys):
    root = tmp_path / "board"
    root.mkdir()
    # No instruction file: project init makes none.
    assert main(["project", "init", "-p", str(root)]) == 0
    assert not (root / "AGENTS.md").exists() and not (root / "CLAUDE.md").exists()

    (root / "AGENTS.md").write_text("# Board\n\nUse SI units.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_bytes(b"# Claude\r\n\r\nAnswer in Czech.\r\n")  # CRLF line ends
    capsys.readouterr()
    assert main(["project", "init", "-p", str(root)]) == 0  # the project exists: no error
    out = capsys.readouterr().out
    assert "it exists: not changed" in out
    assert "AGENTS.md: datasheet-lens block added" in out and "CLAUDE.md: datasheet-lens block added" in out
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Board\n\nUse SI units.\n\n<!-- datasheet-lens -->\n## datasheet-lens")
    assert "`datasheets/`" in text and text.endswith("<!-- /datasheet-lens -->\n")
    claude = (root / "CLAUDE.md").read_bytes()
    assert b"\r\n<!-- /datasheet-lens -->\r\n" in claude and b"\n<!--" not in claude.replace(b"\r\n", b"")

    # A second run changes nothing; an old block is replaced, the rest stays.
    assert main(["project", "init", "-p", str(root)]) == 0
    assert "AGENTS.md: datasheet-lens block current" in capsys.readouterr().out
    old = text.replace("Do not change the copies by hand.", "An old text.") + "\n## After\n"
    (root / "AGENTS.md").write_text(old, encoding="utf-8")
    assert agents.update(root, "datasheets")[0] == ("AGENTS.md", "updated")
    new = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "An old text." not in new and new.count(agents.BEGIN) == 1 and new.endswith("\n## After\n")


def test_claude_md_that_includes_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    result = dict(agents.update(tmp_path, "datasheets"))
    assert result["AGENTS.md"] == "added" and result["CLAUDE.md"].startswith("not changed: it includes")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_block_with_no_end_marker(tmp_path):
    (tmp_path / "AGENTS.md").write_text("<!-- datasheet-lens -->\nhalf a block\n", encoding="utf-8")
    assert agents.update(tmp_path, "datasheets")[0][1].startswith("not changed")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "<!-- datasheet-lens -->\nhalf a block\n"


def test_review_cases(tmp_path, capsys):
    import os
    import stat

    root = tmp_path / "board"
    sub = root / "firmware"
    sub.mkdir(parents=True)
    # A symbolic link: AGENTS.md -> CLAUDE.md. One block, and the link stays.
    (root / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    try:
        os.symlink("CLAUDE.md", root / "AGENTS.md")
    except OSError:  # Windows with no right to make symbolic links
        pytest.skip("no symbolic links")
    os.chmod(root / "CLAUDE.md", 0o644)
    assert main(["project", "init", "-p", str(root)]) == 0
    assert (root / "AGENTS.md").is_symlink()
    assert (root / "CLAUDE.md").read_text(encoding="utf-8").count(agents.BEGIN) == 1
    assert stat.S_IMODE((root / "CLAUDE.md").stat().st_mode) == 0o644  # the permissions stay
    assert stat.S_IMODE((root / "datasheet-lens.toml").stat().st_mode) == 0o644
    capsys.readouterr()

    # From a subfolder: the existing project, not a new one; an option with no effect is told.
    cwd = os.getcwd()
    os.chdir(sub)
    try:
        assert main(["project", "init", "--folder", "docs"]) == 0
    finally:
        os.chdir(cwd)
    out = capsys.readouterr().out
    assert "it exists: not changed" in out and "--folder 'docs' has no effect" in out
    assert not (sub / "datasheet-lens.toml").exists()


def test_markers_on_own_lines_and_other_files(tmp_path):
    text = "Doc: the tool writes `<!-- datasheet-lens -->` here.\n\nKeep this.\n"
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe not UTF-8")
    result = dict(agents.update(tmp_path, "datasheets"))
    assert result["AGENTS.md"] == "added" and result["CLAUDE.md"].startswith("not changed")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").startswith(text.rstrip("\n") + "\n\n<!--")
    # "@./AGENTS.md" counts as an include; one in a code block does not.
    (tmp_path / "CLAUDE.md").write_text("See @./AGENTS.md\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"].startswith("not changed: it includes")
    (tmp_path / "CLAUDE.md").write_text("```\n@AGENTS.md\n```\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == "added"


def test_block_names_the_project_tools():
    """The block for AGENTS.md and CLAUDE.md names each project tool of the MCP
    server: a new tool must also go into the block."""
    import asyncio

    from datasheet_lens.agents import TEXT
    from datasheet_lens.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    wanted = {n for n in names if n.startswith("project_") and n not in ("project_remove_document",)}
    wanted |= {"get_ordering_codes", "find_facts", "save_fact"}
    assert not {n for n in wanted if n not in TEXT}, wanted
