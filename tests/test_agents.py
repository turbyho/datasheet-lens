import pytest

from datasheet_lens import agents
from datasheet_lens.cli import main

INCLUDED = "not changed: Claude Code also reads AGENTS.md through the import in {}, which has the block"


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
    out = capsys.readouterr().out
    assert "AGENTS.md: datasheet-lens block current" in out and "CLAUDE.md: datasheet-lens block current" in out
    assert (root / "CLAUDE.md").read_bytes() == claude
    old = text.replace("Do not change the copies by hand.", "An old text.") + "\n## After\n"
    (root / "AGENTS.md").write_text(old, encoding="utf-8")
    assert agents.update(root, "datasheets")[0] == ("AGENTS.md", "updated")
    new = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "An old text." not in new and new.count(agents.BEGIN) == 1 and new.endswith("\n## After\n")


def test_claude_md_that_includes_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    result = dict(agents.update(tmp_path, "datasheets"))
    assert result["AGENTS.md"] == "added" and result["CLAUDE.md"] == INCLUDED.format("CLAUDE.md")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_block_in_a_file_with_crlf(tmp_path):
    """A file with CRLF line ends: the markers are found, thus a second update finds
    the block current, and an update replaces the block and keeps the CRLF."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# Board\r\n")
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "added")]
    first = path.read_bytes()
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    assert path.read_bytes() == first
    path.write_bytes(first.replace(b"Do not change the copies by hand.", b"An old text."))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "updated")]
    assert path.read_bytes() == first
    half = b"<!-- datasheet-lens -->\r\nhalf a block\r\n"
    path.write_bytes(half)
    assert agents.update(tmp_path, "datasheets")[0][1].startswith("not changed")
    assert path.read_bytes() == half


@pytest.mark.parametrize("head, newline", [
    (b"\xef\xbb\xbf# Board\r\n", b"\r\n"),  # a byte order mark, as an editor of Windows writes it
    (b"# Board\r# Notes\r", b"\r"),  # CR line ends
    (b"# Board", b"\n"),  # no line end
])
def test_block_in_files_of_other_editors(tmp_path, head, newline):
    """The block gets the line ends of the file, the byte order mark stays at the
    start, and a second update finds the block current."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(head)
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "added")]
    first = path.read_bytes()
    assert first.startswith(head.rstrip(b"\r\n")) and first.count(b"<!-- datasheet-lens -->") == 1
    assert first.endswith(b"<!-- /datasheet-lens -->" + newline)
    rest = first.replace(newline, b"")
    assert b"\r" not in rest and b"\n" not in rest  # only the line end of the file
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    assert path.read_bytes() == first


def test_marker_lines_that_are_not_markers(tmp_path):
    """A marker line in a fenced code block is an example, not a marker. Spaces after
    a marker do not make the block different. The line ends outside the block stay."""
    path = tmp_path / "AGENTS.md"
    example = ("```\n<!-- datasheet-lens -->\n```\n"
               "~~~\n<!-- datasheet-lens -->\nexample\n<!-- /datasheet-lens -->\n~~~\n")
    path.write_bytes(example.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "added")]
    text = path.read_bytes().decode("utf-8")
    assert text.startswith(example) and text.count("<!-- datasheet-lens -->") == 3
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    # Spaces after the markers, CRLF in the block, and LF on the first line.
    block = agents.block("datasheets").replace("\n", "\r\n").replace("-->\r\n", "--> \t\r\n", 1)
    path.write_bytes(b"# a\n# b\r\n" + block.encode("utf-8") + b" \r\n")
    before = path.read_bytes()
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    assert path.read_bytes() == before
    path.write_bytes(before.replace(b"Do not change the copies by hand.", b"An old text."))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "updated")]
    # A new block with no spaces after the markers. The text outside the block
    # stays: the LF of "# a".
    clean = agents.block("datasheets").replace("\n", "\r\n").encode("utf-8")
    assert path.read_bytes() == b"# a\n# b\r\n" + clean + b"\r\n"


@pytest.mark.parametrize("example", [
    "````\n```\n<!-- datasheet-lens -->\n```\n````\n",  # a longer fence holds a shorter one
    "  ```\n<!-- datasheet-lens -->\n  ```\n",  # a fence with up to 3 spaces
    "~~~ text\n<!-- datasheet-lens -->\n~~~\n",  # tildes and an info string
])
def test_markers_in_fences_of_commonmark(tmp_path, example):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(example.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "added")]
    assert path.read_bytes().decode("utf-8").startswith(example)


def test_lines_that_do_not_make_a_fence(tmp_path):
    """Four spaces make an indented code block, and backticks in the info string of
    a backtick line make inline code: no fence, thus the marker lines are markers."""
    for head in ("    ```\n", "``` a`b\n"):
        (tmp_path / "AGENTS.md").write_bytes((head + "<!-- datasheet-lens -->\nhalf\n").encode("utf-8"))
        assert agents.update(tmp_path, "datasheets")[0][1] == "not changed: <!-- datasheet-lens --> has no " \
            "<!-- /datasheet-lens --> line after it (outside a code block)"


def test_file_that_ends_in_an_open_fence(tmp_path):
    """A fence with no end runs to the end of the file: a block after it is code."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# Board\n```\ncode\n")
    assert agents.update(tmp_path, "datasheets") == [
        ("AGENTS.md", "not changed: the end of the file is in a code block or an HTML block with no end")]
    assert path.read_bytes() == b"# Board\n```\ncode\n"


def test_half_block_before_a_full_block(tmp_path):
    """A BEGIN with no END before an other block: no change, the text of the user stays."""
    path = tmp_path / "AGENTS.md"
    text = "<!-- datasheet-lens -->\nnotes of the user\n\n" + agents.block("datasheets") + "\n"
    path.write_bytes(text.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets")[0][1].startswith("not changed")
    assert path.read_bytes() == text.encode("utf-8")


def test_line_end_of_the_text_of_the_user(tmp_path):
    """The line end comes from the text outside the block: an LF block in a CRLF file
    gets CRLF, also when the block has more lines than the text of the user."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# Board\r\n\r\n" + agents.block("datasheets").encode("utf-8") + b"\r\n")
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "updated")]
    assert path.read_bytes() == (b"# Board\r\n\r\n" + agents.block("datasheets").replace(
        "\n", "\r\n").encode("utf-8") + b"\r\n")
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    # A file with only the block keeps the line end of the block.
    path.write_bytes(agents.block("datasheets").encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]


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
    os.chmod(root / "CLAUDE.md", 0o644)  # on Windows: only "not read-only"
    mode = stat.S_IMODE((root / "CLAUDE.md").stat().st_mode)
    # The mode of a new file in this folder (umask on Linux and macOS), not the
    # 0o600 of a temporary file.
    (tmp_path / "new.txt").write_text("", encoding="utf-8")
    new_file = stat.S_IMODE((tmp_path / "new.txt").stat().st_mode)
    assert main(["project", "init", "-p", str(root)]) == 0
    assert (root / "AGENTS.md").is_symlink()
    assert (root / "CLAUDE.md").read_text(encoding="utf-8").count(agents.BEGIN) == 1
    assert stat.S_IMODE((root / "CLAUDE.md").stat().st_mode) == mode  # the permissions stay
    assert stat.S_IMODE((root / "datasheet-lens.toml").stat().st_mode) == new_file
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
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == INCLUDED.format("CLAUDE.md")
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


def test_fences_in_list_items(tmp_path):
    """A fenced code block in a list item ends in the list item: its lines do not
    hide the block, and the block is found one time."""
    path = tmp_path / "AGENTS.md"
    text = ("1. ```\n   x\n   ```\n\n" + agents.block("datasheets") + "\n\n- ```\n  y\n  ```\n"
            "   ```\n    <!-- datasheet-lens -->\n    ```\n")
    path.write_bytes(text.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "current")]
    path.write_bytes(text.replace("Do not change the copies by hand.", "An old text.").encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "updated")]
    assert path.read_bytes() == text.encode("utf-8")


def test_include_after_a_fence_in_a_list_item(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(b"- ```\n  a\n  ```\n\n@AGENTS.md\n\n- ```\n  b\n  ```\n")
    result = dict(agents.update(tmp_path, "datasheets"))
    assert result["AGENTS.md"] == "added" and result["CLAUDE.md"] == INCLUDED.format("CLAUDE.md")


def test_two_blocks(tmp_path):
    """Two blocks (for example after a merge): no change, and the result tells it."""
    path = tmp_path / "AGENTS.md"
    text = agents.block("datasheets") + "\n\n" + agents.block("datasheets") + "\n"
    path.write_bytes(text.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [
        ("AGENTS.md", "not changed: 2 <!-- datasheet-lens --> lines: keep one block")]
    assert path.read_bytes() == text.encode("utf-8")


def test_code_block_with_no_end_in_the_block(tmp_path):
    """A code block with no end in the block makes code of the END line: the result
    tells that the END line is not outside a code block."""
    path = tmp_path / "AGENTS.md"
    text = "<!-- datasheet-lens -->\n```\nold\n<!-- /datasheet-lens -->\n"
    path.write_bytes(text.encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [("AGENTS.md", "not changed: <!-- datasheet-lens --> has no "
                                                     "<!-- /datasheet-lens --> line after it (outside a code block)")]
    assert path.read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("end", ["<!-- note\n", "<pre>\ncode\n"])
def test_file_that_ends_in_an_open_html_block(tmp_path, end):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(("# Board\n" + end).encode("utf-8"))
    assert agents.update(tmp_path, "datasheets") == [
        ("AGENTS.md", "not changed: the end of the file is in a code block or an HTML block with no end")]
    assert path.read_bytes() == ("# Board\n" + end).encode("utf-8")


@pytest.mark.parametrize("wrap", [
    "<details>\n{block}\n</details>\n",  # an HTML block holds the markers
    "<div>\n{block}\n</div>\n",
    "- item\n\n  {block}\n",  # a block in a list item
])
def test_block_that_is_not_at_the_top_level(tmp_path, wrap):
    """The tool does not add a second block when the old block is not at the top
    level: the result tells it, and the file does not change."""
    path = tmp_path / "AGENTS.md"
    text = wrap.format(block=agents.block("datasheets").replace("\n", "\n  " if wrap.startswith("-") else "\n"))
    path.write_bytes(text.encode("utf-8"))
    result = agents.update(tmp_path, "datasheets")[0][1]
    assert result.startswith("not changed: a <!-- datasheet-lens --> or <!-- /datasheet-lens --> line in an HTML")
    assert path.read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("claude", [
    "<!-- @AGENTS.md (disabled) -->\n",  # Claude Code removes the HTML comments
    "Write `@AGENTS.md` to import it.\n",  # an import in inline code is not an import
])
def test_text_that_does_not_import_agents_md(tmp_path, claude):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(claude.encode("utf-8"))
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == "added"


def test_import_in_a_sentence(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(b"# Claude\n\nRead *the rules*: @./AGENTS.md\n")
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == INCLUDED.format("CLAUDE.md")


def test_claude_md_in_the_claude_folder(tmp_path, capsys):
    """Claude Code reads .claude/CLAUDE.md as CLAUDE.md: project init puts the block
    into it when it is the only instruction file."""
    (tmp_path / ".claude").mkdir()
    path = tmp_path / ".claude" / "CLAUDE.md"
    path.write_text("# Claude\n", encoding="utf-8")
    assert main(["project", "init", "-p", str(tmp_path)]) == 0
    assert ".claude/CLAUDE.md: datasheet-lens block added" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8").count(agents.BEGIN) == 1
    assert agents.update(tmp_path, "datasheets") == [(".claude/CLAUDE.md", "current")]


def test_two_claude_md_files_get_one_block(tmp_path):
    """Claude Code reads CLAUDE.md and .claude/CLAUDE.md together: one block only.
    A file that has the block keeps it, also when it is .claude/CLAUDE.md."""
    (tmp_path / ".claude").mkdir()
    root_file, folder_file = tmp_path / "CLAUDE.md", tmp_path / ".claude" / "CLAUDE.md"
    root_file.write_text("# Root\n", encoding="utf-8")
    folder_file.write_text("# Folder\n", encoding="utf-8")
    also = "not changed: Claude Code also reads {}, which has the block"
    assert agents.update(tmp_path, "datasheets") == [
        ("CLAUDE.md", "added"), (".claude/CLAUDE.md", also.format("CLAUDE.md"))]
    assert agents.BEGIN not in folder_file.read_text(encoding="utf-8")
    assert agents.update(tmp_path, "datasheets") == [
        ("CLAUDE.md", "current"), (".claude/CLAUDE.md", also.format("CLAUDE.md"))]

    root_file.write_text("# Root\n", encoding="utf-8")
    folder_file.write_text("# Folder\n\n" + agents.block("old") + "\n", encoding="utf-8")
    assert agents.update(tmp_path, "datasheets") == [
        ("CLAUDE.md", also.format(".claude/CLAUDE.md")), (".claude/CLAUDE.md", "updated")]
    assert root_file.read_text(encoding="utf-8") == "# Root\n"


def test_import_is_relative_to_the_file(tmp_path):
    """A relative import starts in the folder of the file: "@AGENTS.md" in
    .claude/CLAUDE.md is .claude/AGENTS.md, and "@../AGENTS.md" is the root file."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    path = tmp_path / ".claude" / "CLAUDE.md"
    path.write_text("@AGENTS.md\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))[".claude/CLAUDE.md"] == "added"
    path.write_text("@../AGENTS.md\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))[".claude/CLAUDE.md"] == INCLUDED.format(".claude/CLAUDE.md")
    # An absolute path. Claude Code reads a backslash as the end of the path: "/" also on Windows.
    path.write_text(f"@{(tmp_path / 'AGENTS.md').as_posix()}\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))[".claude/CLAUDE.md"] == INCLUDED.format(".claude/CLAUDE.md")


@pytest.mark.parametrize("claude, included", [
    ("@AGENTS.md#rules\n", True),  # the text after "#" is not part of the path
    ("<!-- note --> @AGENTS.md\n", True),  # Claude Code reads the HTML block with no comment
    ("- <!-- note --> @AGENTS.md\n", True),  # also in a list item
    ("<div>\n@AGENTS.md\n</div>\n", False),  # an other HTML block: no import
    ("Read @AGENTS.md, then work.\n", False),  # the path is "AGENTS.md," (no such file)
])
def test_import_rules_of_claude_code(tmp_path, claude, included):
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(claude, encoding="utf-8")
    wanted = INCLUDED.format("CLAUDE.md") if included else "added"
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == wanted


def test_two_claude_md_files_that_are_one_file(tmp_path):
    """.claude/CLAUDE.md that is a symbolic link to CLAUDE.md: one block, and the
    result tells that the two names are one file."""
    import os

    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    try:
        # The separator of the system: Windows does not follow a relative link with "/".
        os.symlink(os.path.join("..", "CLAUDE.md"), tmp_path / ".claude" / "CLAUDE.md")
    except OSError:  # Windows with no right to make symbolic links
        pytest.skip("no symbolic links")
    assert agents.update(tmp_path, "datasheets") == [
        ("CLAUDE.md", "added"), (".claude/CLAUDE.md", "added (the same file as CLAUDE.md)")]
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").count(agents.BEGIN) == 1


def test_two_claude_md_files_with_a_block(tmp_path):
    """Two blocks in the context of Claude Code: the tool updates the two and tells
    the user to keep one."""
    (tmp_path / ".claude").mkdir()
    for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
        (tmp_path / name).write_text(agents.block("datasheets") + "\n", encoding="utf-8")
    assert agents.update(tmp_path, "datasheets") == [
        ("CLAUDE.md", "current"),
        (".claude/CLAUDE.md", "current (Claude Code also reads the block in CLAUDE.md: keep one of the two blocks)")]


def test_import_of_agents_md_and_a_block(tmp_path):
    """CLAUDE.md that imports AGENTS.md and has its own block: the two blocks stay,
    and the result tells the user to keep one. A broken block in AGENTS.md also
    counts: CLAUDE.md gets no second block."""
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n\n" + agents.block("datasheets") + "\n", encoding="utf-8")
    assert agents.update(tmp_path, "datasheets") == [
        ("AGENTS.md", "added"),
        ("CLAUDE.md", "current (Claude Code also reads the block in AGENTS.md through the import in CLAUDE.md: "
                      "keep one of the two blocks)")]
    (tmp_path / "AGENTS.md").write_text("<!-- datasheet-lens -->\nhalf a block\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    assert dict(agents.update(tmp_path, "datasheets"))["CLAUDE.md"] == (
        "not changed: Claude Code also reads AGENTS.md through the import in CLAUDE.md, which has a block that "
        "the tool cannot change")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_claude_md_that_is_agents_md_and_a_second_block(tmp_path):
    """CLAUDE.md is a symbolic link to AGENTS.md, and .claude/CLAUDE.md has its own
    block: the result tells it on .claude/CLAUDE.md, not on AGENTS.md."""
    import os

    (tmp_path / ".claude").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    try:
        os.symlink("AGENTS.md", tmp_path / "CLAUDE.md")
    except OSError:  # Windows with no right to make symbolic links
        pytest.skip("no symbolic links")
    (tmp_path / ".claude" / "CLAUDE.md").write_text(agents.block("datasheets") + "\n", encoding="utf-8")
    assert agents.update(tmp_path, "datasheets") == [
        ("AGENTS.md", "added"), ("CLAUDE.md", "added (the same file as AGENTS.md)"),
        (".claude/CLAUDE.md", "current (Claude Code also reads the block in CLAUDE.md: keep one of the two blocks)")]
