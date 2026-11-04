"""The Markdown chunker (tadamoo/markdown_chunks.py): pieces within the limit, no cut inside a code block, table or list item when avoidable."""
import random
import re

import pytest

from tadamoo.markdown_chunks import split_markdown

FENCE_LINE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$", re.M)


def fences_balanced(piece):
    """Every fence opened in the piece is closed in it (the closer as long as the opener)."""
    open_marker = None
    for line in piece.split("\n"):
        s = line.strip()
        if open_marker is None:
            m = re.match(r"^(`{3,}|~{3,})", s)
            if m:
                open_marker = m.group(1)
        elif s and set(s) == {open_marker[0]} and len(s) >= len(open_marker):
            open_marker = None
    return open_marker is None


def without_fence_lines(text):
    return FENCE_LINE.sub("", text).replace("\n", "")


def check(text, limit):
    pieces = split_markdown(text, limit)
    assert all(len(p) <= limit for p in pieces), [len(p) for p in pieces]
    return pieces


def test_short_and_empty_text():
    assert split_markdown("", 10) == []
    assert split_markdown("short", 10) == ["short"]
    with pytest.raises(ValueError):
        split_markdown("x", 0)


def test_paragraphs_are_the_preferred_boundary_and_the_text_comes_back():
    text = "First paragraph, a line.\nIts second line.\n\nSecond paragraph here.\n\nThird one, also short.\n"
    pieces = check(text, 45)
    assert "".join(pieces) == text
    assert pieces[0].startswith("First paragraph") and "Second paragraph" in pieces[1]
    assert not any("Its second line." in p and "First paragraph" not in p for p in pieces)       # the lines of one paragraph stay together


def test_a_long_paragraph_breaks_at_lines_then_at_sentences_then_anywhere():
    lines = "\n".join(f"line number {i} of the paragraph" for i in range(10))
    pieces = check(lines, 80)
    assert "".join(pieces) == lines and all(p.endswith("\n") for p in pieces[:-1])
    sentences = " ".join(f"Sentence {i} says something." for i in range(12))
    pieces = check(sentences, 70)
    assert "".join(pieces) == sentences and all(p.rstrip().endswith(".") for p in pieces)


def test_a_very_long_single_line_is_cut_and_comes_back():
    word = "abcdefghij" * 50
    pieces = check(word, 64)
    assert "".join(pieces) == word and len(pieces) == 8
    spaced = ("word " * 100).strip()
    pieces = check(spaced, 33)
    assert "".join(pieces) == spaced and all(p.endswith(" ") for p in pieces[:-1])               # cut after a space when there is one


def test_a_code_block_that_fits_is_never_cut():
    code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(8)) + "\n```\n"
    text = "Intro paragraph that is fairly long to fill the room.\n\n" + code + "\nAfter the code.\n"
    pieces = check(text, len(code) + 5)
    assert "".join(pieces) == text
    assert sum(code.rstrip() in p for p in pieces) == 1 and all(fences_balanced(p) for p in pieces)


def test_an_oversize_code_block_is_closed_and_reopened_with_its_language():
    body = [f"value_{i} = compute({i})\n" for i in range(30)]
    text = "```python\n" + "".join(body) + "```\n"
    pieces = check(text, 120)
    assert len(pieces) > 2 and all(fences_balanced(p) for p in pieces)
    assert all(p.startswith("```python\n") for p in pieces)
    # without the added fence lines the code comes back exactly
    code = ""
    for n, p in enumerate(pieces):
        inner = p.split("\n")[1:]                                   # the opener is the first line of every piece
        if n < len(pieces) - 1:
            assert inner[-1] == "```"                               # the added closing fence
            inner = inner[:-1]
        else:
            inner = inner[:-2]                                      # the real closing fence and the newline after it
        code += "\n".join(inner) + "\n"
    assert code == "".join(body)
    assert "".join(pieces).endswith("```\n")


def test_nested_fences_keep_the_outer_marker_and_the_inner_ones_stay_content():
    inner = "\n".join(f"line {i}" for i in range(20))
    text = "````markdown\nExample:\n\n```python\nprint(1)\n```\n\n" + inner + "\n````\n"
    pieces = check(text, 90)
    assert len(pieces) > 1 and all(p.startswith("````markdown\n") for p in pieces)
    assert all(p.rstrip().endswith("````") for p in pieces)
    assert all(fences_balanced(p) for p in pieces)


def test_an_unclosed_fence_runs_to_the_end_and_each_piece_of_it_but_the_last_is_closed():
    lines = [f"var a{i} = {i};" for i in range(25)]
    text = "Before.\n\n```js\n" + "\n".join(lines)
    pieces = check(text, 100)
    fenced = [p for p in pieces if "```js" in p]
    assert pieces[0].startswith("Before.") and len(fenced) > 2
    assert all(p.startswith("```js\n") and p.endswith("```") for p in fenced[:-1])
    assert fenced[-1].startswith("```js\n") and not fenced[-1].endswith("```")
    assert without_fence_lines("".join(pieces)) == without_fence_lines(text)


def test_a_long_code_line_is_cut_inside_a_closed_fence():
    text = "```\n" + "x" * 300 + "\nshort\n```\n"
    pieces = check(text, 80)
    assert all(fences_balanced(p) for p in pieces) and len(pieces) >= 4
    assert without_fence_lines("".join(pieces)) == "x" * 300 + "short"


def test_a_table_that_fits_stays_whole_and_one_that_does_not_repeats_its_header():
    header = "| name | value |\n| --- | ---: |\n"
    table = header + "".join(f"| key{i} | {i} |\n" for i in range(20))
    text = "Intro line that fills some room here.\n\n" + table + "\nAfter.\n"
    whole = check(text, len(table) + 60)
    assert "".join(whole) == text and sum("| name | value |" in p for p in whole) == 1
    pieces = check(text, 120)
    tables = [p for p in pieces if "| key" in p]
    assert len(tables) > 1 and all(p[p.index("| name"):].startswith(header) for p in tables)
    assert "".join(pieces).count("| name | value |") == len(tables)
    rows = []
    for p in tables:
        rows += re.findall(r"\| key\d+ \| \d+ \|", p)
    assert rows == [f"| key{i} | {i} |" for i in range(20)]            # every row once, in order


def test_list_items_are_not_cut_while_a_boundary_between_items_exists():
    items = ["- first item with a continued\n  second line\n", "- second item\n  - nested a\n  - nested b\n", "- third item\n\n  with its own paragraph\n", "1. numbered item\n"]
    text = "".join(items)
    pieces = check(text, max(len(i) for i in items) + 10)
    assert "".join(pieces) == text
    for item in items:
        assert sum(item.rstrip() in p for p in pieces) == 1


def test_a_fence_inside_a_list_item_is_kept_with_its_item():
    item = "- step one:\n\n  ```sh\n\n  make\n\n  ```\n\n  done\n"
    text = "Lead paragraph, quite long, to push the item over.\n\n" + item + "- next\n"
    pieces = check(text, len(item) + 5)
    assert "".join(pieces) == text and sum("make" in p for p in pieces) == 1
    assert all(fences_balanced(p) for p in pieces)


def test_a_limit_too_small_for_the_fence_lines_is_an_error():
    with pytest.raises(ValueError):
        split_markdown("```python\n" + "a = 1\n" * 10 + "```\n", 12)


def test_windows_line_endings_are_kept():
    text = "```py\r\n" + "".join(f"a{i} = {i}\r\n" for i in range(20)) + "```\r\n\r\nAfter.\r\n"
    pieces = check(text, 90)
    assert all(fences_balanced(p) for p in pieces)
    assert without_fence_lines("".join(pieces)).replace("\r", "") == without_fence_lines(text).replace("\r", "")


FRAGMENTS = [
    "A plain paragraph with some words. And another sentence! Is this one a question? Yes.\n",
    "# A heading\n",
    "- an item\n  continued here\n",
    "1. numbered\n   - nested\n",
    "```python\nx = 1\ny = 2\n```\n",
    "| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |\n",
    "> a quote line\n> and more\n",
    "A long line without any stop " + "word " * 30 + "\n",
    "\n",
]


@pytest.mark.parametrize("seed", range(40))
def test_the_pieces_fit_and_give_back_the_text(seed):
    rnd = random.Random(seed)
    text = "".join(rnd.choice(FRAGMENTS) + rnd.choice(["", "\n"]) for _ in range(rnd.randint(1, 14)))
    limit = rnd.choice([60, 90, 150, 300, 800])
    pieces = split_markdown(text, limit)
    assert all(len(p) <= limit for p in pieces)
    assert all(fences_balanced(p) for p in pieces)

    def plain(t):                                                   # the fence lines and a table's repeated header are the only additions
        for h in ("| a | b |", "| - | - |"):
            t = t.replace(h, "")
        return without_fence_lines(t)
    assert plain("".join(pieces)) == plain(text)
