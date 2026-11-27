"""The file tools work on pieces of a file by their place: the map, a section or a range of words read as a block, edits of a block by its lines or
its section, and a text copied from an earlier reply or a file instead of typed again."""
import re

import pytest

from tadamoo.tools import pieces
from tadamoo.tools.filesystem import WorkspaceSandbox

BOOK = ("# Il libro\n\nIntroduzione di poche parole.\n\n## Capitolo 1\n\nUno due tre quattro.\n\nCinque sei.\n\n"
        "## Capitolo 2\n\nSette otto nove.\n\n### Dettaglio\n\nDieci undici.\n\n## Capitolo 3\n\nDodici.\n")


@pytest.fixture
def sb(tmp_path):
    box = WorkspaceSandbox(tmp_path / "ws")
    box.write_file("book.md", BOOK)
    return box


def text(sb, name="book.md"):
    return (sb.root / name).read_bytes().decode("utf-8")


def version_of(answer):
    return re.search(r"version ([0-9a-f]{8})", answer).group(1)


def test_the_map_lists_the_sections_with_their_lines_words_and_version(sb):
    out = sb.read_file("book.md", outline=True)
    assert out.splitlines()[0].startswith("21 lines, ") and "words, version" in out.splitlines()[0]
    assert "5-10\t" in out and "11-18\t" in out and "## Capitolo 1" in out and "  ### Dettaglio" in out
    assert re.search(r"1-21\t30w\t# Il libro", out)
    assert version_of(out) == pieces.version(BOOK)


def test_a_heading_inside_a_code_fence_is_not_a_section(sb):
    sb.write_file("code.md", "# A\n\n```\n# not a heading\n```\n\n## B\n")
    out = sb.read_file("code.md", outline=True)
    assert "not a heading" not in out and "## B" in out


def test_a_text_without_headings_says_so_and_points_to_the_other_ways_to_read(sb):
    sb.write_file("plain.txt", "uno due\n\ntre quattro cinque\n")
    out = sb.read_file("plain.txt", outline=True)
    assert "No headings" in out and "words=[from, to]" in out


def test_a_section_is_read_by_its_title_with_what_is_under_it(sb):
    out = sb.read_file("book.md", section="capitolo 2")
    assert out.startswith("## Capitolo 2\n") and "Dettaglio" in out and "Dodici" not in out
    assert "section 'Capitolo 2': lines 11-18 of 21" in out and version_of(out) == pieces.version(BOOK)
    assert sb.read_file("book.md", section="dett").startswith("### Dettaglio")             # the start of a title


def test_a_title_that_fits_several_sections_or_none_is_refused_with_the_candidates(sb):
    with pytest.raises(ValueError, match="fits 3 sections"):
        sb.read_file("book.md", section="Capitolo")
    with pytest.raises(ValueError, match="No section is called"):
        sb.read_file("book.md", section="Epilogo")


def test_a_range_of_words_is_read_as_the_whole_lines_that_hold_it(sb):
    out = sb.read_file("book.md", words=[0, 5])
    assert out.startswith("# Il libro\n\nIntroduzione di poche parole.") and "words 0-" in out and "lines 1-3 of 21" in out
    inner = sb.read_file("book.md", words=[10, 11])                                        # words 10 and 11 are in "Uno due tre quattro."
    assert "Uno due tre quattro." in inner and "Cinque" not in inner
    with pytest.raises(ValueError, match="past the end"):
        sb.read_file("book.md", words=[9000, 9010])
    with pytest.raises(ValueError, match="two whole numbers"):
        sb.read_file("book.md", words=[1])


def test_a_section_is_replaced_by_its_title_and_the_old_text_is_not_typed(sb):
    out = sb.edit_file("book.md", section="Capitolo 3", text="## Capitolo 3\n\nTredici quattordici.")
    assert out["action"] == "replace" and out["lines"] == [19, 21] and out["total_lines"] == 21
    assert text(sb).endswith("## Capitolo 3\n\nTredici quattordici.\n")
    assert "Dodici" not in text(sb) and out["version"] == pieces.version(text(sb).replace("\r\n", "\n"))


def test_lines_need_the_version_they_were_read_from(sb):
    with pytest.raises(ValueError, match="good for the version"):
        sb.edit_file("book.md", lines=[3, 3], text="Nuova introduzione.")
    first = sb.read_file("book.md", outline=True)
    ver = version_of(first)
    out = sb.edit_file("book.md", lines=[3, 3], text="Nuova introduzione, più lunga.", version=ver)
    assert out["lines"] == [3, 3] and "Nuova introduzione, più lunga." in text(sb)
    with pytest.raises(ValueError) as stale:                                              # the number was good for the version before the edit
        sb.edit_file("book.md", lines=[7, 7], text="X", version=ver)
    assert "now version " + out["version"] in str(stale.value) and "## Capitolo 1" in str(stale.value)
    sb.edit_file("book.md", lines=[7, 7], text="Uno due tre.", version=out["version"])      # the version the answer gave works
    assert "Uno due tre.\n" in text(sb) and "quattro" not in text(sb)


def test_a_block_is_deleted_and_text_is_inserted_beside_it_by_place(sb):
    sb.edit_file("book.md", section="Dettaglio", action="delete")
    assert "Dettaglio" not in text(sb) and "Dieci" not in text(sb) and "Sette otto nove." in text(sb)
    sb.edit_file("book.md", section="Capitolo 1", action="insert_after", text="\n## Capitolo 1 bis\n\nTesto in più.")
    assert text(sb).index("Capitolo 1 bis") < text(sb).index("## Capitolo 2")
    sb.edit_file("book.md", section="Capitolo 2", action="insert_before", text="[nota]\n")
    assert "\n[nota]\n## Capitolo 2\n" in text(sb)
    with pytest.raises(ValueError, match="takes no text"):
        sb.edit_file("book.md", section="Capitolo 3", action="delete", text="x")


def test_a_section_is_moved_behind_another_and_a_move_into_itself_is_refused(sb):
    sb.edit_file("book.md", section="Capitolo 1", action="move_after", after={"section": "Capitolo 3"})
    body = text(sb)
    assert body.index("## Capitolo 2") < body.index("## Capitolo 3") < body.index("## Capitolo 1")
    assert "Cinque sei." in body and body.count("Uno due tre quattro.") == 1
    ver = pieces.version(body)
    with pytest.raises(ValueError, match="into itself"):
        sb.edit_file("book.md", lines=[3, 5], action="move_after", after={"line": 4}, version=ver)
    with pytest.raises(ValueError, match="into itself"):
        sb.edit_file("book.md", lines=[1, 2], action="move_after", after={"line": 0}, version=ver)       # to where it already is
    sb.edit_file("book.md", lines=[5, 6], action="move_after", after={"line": 0}, version=ver)
    assert text(sb).startswith("## Capitolo 2\n")                                                  # lines 5-6 of the moved text were that heading and its blank line
    with pytest.raises(ValueError, match="after is"):
        sb.edit_file("book.md", section="Capitolo 3", action="move_after")


def test_the_text_may_be_typed_or_copied_but_not_both_nor_neither(sb):
    with pytest.raises(ValueError, match="one of them"):
        sb.edit_file("book.md", section="Capitolo 3", text="x", source={"file": "book.md"})
    with pytest.raises(ValueError, match="one of them"):
        sb.edit_file("book.md", section="Capitolo 3")
    with pytest.raises(ValueError, match="Name the block"):
        sb.edit_file("book.md", text="x", action="replace")
    with pytest.raises(Exception, match="not a mix"):
        sb.edit_file("book.md", section="Capitolo 3", target="a", replacement="b")


def test_a_block_is_copied_from_a_file_by_section_lines_or_words(sb):
    sb.write_file("other.md", "# Altro\n\nPrima parte.\n\n## Fine\n\nUltima riga del libro.\n")
    sb.edit_file("book.md", section="Capitolo 3", text="## Capitolo 3\n\n[qui]")
    sb.edit_file("book.md", section="Capitolo 3", action="insert_after", source={"file": "other.md", "section": "Fine"})
    assert text(sb).endswith("[qui]\n## Fine\n\nUltima riga del libro.\n")
    out = sb.write_file("copy.md", source={"file": "other.md", "lines": [3, 3]})
    assert text(sb, "copy.md") == "Prima parte.\n" and out["kind"] == "added"
    sb.write_file("copy2.md", source={"file": "other.md", "words": [0, 2]})
    assert text(sb, "copy2.md").startswith("# Altro\n")


def test_a_reply_of_the_conversation_is_written_to_a_file_without_typing_it(sb):
    replies = ["Prima risposta.", "# Storia\n\n## Capitolo 1\n\nC'era una volta.\n\n## Capitolo 2\n\nE poi.\n"]
    sb.message_text = lambda which: replies[-1] if which == "last" else (replies[-which] if isinstance(which, int) and 1 <= which <= len(replies) else None)
    out = sb.write_file("storia.md", source={"message": "last"})
    assert out["kind"] == "added" and text(sb, "storia.md") == replies[1]
    sb.write_file("primo.md", source={"message": 2})
    assert text(sb, "primo.md") == "Prima risposta."
    sb.write_file("cap2.md", source={"message": "last", "section": "Capitolo 2"})
    assert text(sb, "cap2.md") == "## Capitolo 2\n\nE poi.\n"
    sb.write_file("storia.md", source={"message": "last"}, append=True)
    assert text(sb, "storia.md").count("# Storia") == 2


def test_a_reply_that_is_not_there_is_refused_and_says_where_the_folded_ones_are(sb):
    with pytest.raises(ValueError, match="folded away"):
        sb.write_file("x.md", source={"message": "last"})
    sb.message_text = lambda which: None
    with pytest.raises(ValueError, match="artifacts"):
        sb.write_file("x.md", source={"message": 3})
    with pytest.raises(ValueError, match="one of message"):
        sb.write_file("x.md", source={"message": "last", "file": "book.md"})
    with pytest.raises(ValueError, match="one of them"):
        sb.write_file("x.md")
    with pytest.raises(ValueError, match="one of them"):
        sb.write_file("x.md", content="a", source={"file": "book.md"})


def test_a_whole_file_write_from_a_source_still_keeps_the_guards_of_a_write(sb):
    long = "# Lungo\n\n" + "\n\n".join(f"Paragrafo numero {i} con qualche parola in più." for i in range(40)) + "\n"
    sb.write_file("long.md", long)
    sb.read_file("long.md")
    sb.message_text = lambda which: "# Corto\n\nSolo questo.\n"
    out = sb.write_file("long.md", source={"message": "last"})
    assert out["status"] == "refused" and "under half" in out["message"] and text(sb, "long.md") == long
    ok = sb.write_file("long.md", source={"message": "last"}, overwrite=True)
    assert ok["kind"] == "modified" and text(sb, "long.md") == "# Corto\n\nSolo questo.\n"


def test_the_line_endings_of_the_file_are_kept_by_an_edit_of_a_block(sb):
    sb.write_file("crlf.md", "# A\r\n\r\nuno\r\n\r\n## B\r\n\r\ndue\r\n")
    sb.edit_file("crlf.md", section="B", text="## B\n\ntre")
    assert (sb.root / "crlf.md").read_bytes() == b"# A\r\n\r\nuno\r\n\r\n## B\r\n\r\ntre\r\n"


def test_a_file_that_is_binary_or_a_path_outside_is_refused_like_the_other_tools(sb):
    (sb.root / "bin.dat").write_bytes(b"\x00\x01" * 50)
    with pytest.raises(ValueError, match="binary"):
        sb.read_file("bin.dat", outline=True)
    with pytest.raises(PermissionError):
        sb.write_file("a.md", source={"file": "../outside.md"})


def test_a_replace_with_the_same_text_changes_nothing(sb):
    out = sb.edit_file("book.md", section="Capitolo 3", text="## Capitolo 3\n\nDodici.")
    assert out["changed"] is False and text(sb) == BOOK


def test_the_registered_tools_take_the_new_arguments(tmp_path):
    from tadamoo.tools import create_standard_tool_registry
    registry, box = create_standard_tool_registry(tmp_path / "ws")
    box.write_file("b.md", BOOK)
    box.message_text = lambda which: "# R\n\nrisposta\n" if which in ("last", "1") else None
    ver = version_of(registry.execute("read_file", {"path": "b.md", "outline": True}))
    registry.execute("edit_file", {"path": "b.md", "lines": [3, 3], "text": "X", "version": ver})
    registry.execute("write_file", {"path": "c.md", "source": {"message": "last"}})
    assert text(box, "c.md") == "# R\n\nrisposta\n" and "\nX\n" in text(box, "b.md")
    registry.execute("edit_file", {"path": "b.md", "section": "Capitolo 1", "action": "move_after", "after": {"section": "Capitolo 3"}})
    assert text(box, "b.md").index("## Capitolo 3") < text(box, "b.md").index("## Capitolo 1")
    schemas = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in registry.schemas()}
    assert {"outline", "section", "words"} <= set(schemas["read_file"]) and {"lines", "action", "source", "after", "version"} <= set(schemas["edit_file"])
    assert "source" in schemas["write_file"] and "content" in schemas["write_file"]


def test_the_agent_gives_the_file_tools_its_last_reply_and_not_a_step_with_a_tool_call(tmp_path):
    from types import SimpleNamespace
    from tadamoo.stack import AgentConfig, build_agent
    (tmp_path / "ws").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "ws"), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="default", max_turns=6), client=SimpleNamespace(chat_completion=lambda *a, **k: {}))
    built.agent.messages += [
        {"role": "assistant", "content": "Prima."},
        {"role": "assistant", "content": "Passo con strumento.", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "content": "ok"},
        {"role": "assistant", "content": "# Storia\n\nTesto finale.\n"}]
    ask = built.sandbox.message_text
    assert ask("last") == "# Storia\n\nTesto finale.\n" and ask("1") == ask(1) == ask("last") and ask("2") == "Prima."
    assert ask(3) is None and ask("tre") is None and ask(0) is None
