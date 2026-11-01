"""Where the Markdown chunker (harness/markdown_chunks.py) is wired: a very long text read in parts (digest), shown to the model from an
attachment (compose_message, read_range), and a long answer delivered and joined in pieces (serve/longform.py). Plain code, no model."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import attachments as at
from harness import digest
from harness.markdown_chunks import joiner, safe_line_end, whole_blocks
from serve import longform as lf


def open_fence(piece):
    """Whether a piece ends inside a fenced code block."""
    marker = None
    for line in piece.split("\n"):
        s = line.strip()
        if marker is None:
            m = re.match(r"^(`{3,}|~{3,})", s)
            marker = m.group(1) if m else None
        elif s and set(s) == {marker[0]} and len(s) >= len(marker):
            marker = None
    return marker is not None


def book(chapters=30):
    """A long Markdown text: prose, a code block with blank lines in it, a table and a nested list in every chapter."""
    out = []
    for c in range(chapters):
        out.append(f"## Chapter {c}\n")
        out.append(" ".join(f"Sentence {c}.{i} says something about the river." for i in range(12)) + "\n")
        out.append("```python\ndef f():\n    x = 1\n\n    return x\n\n\nprint(f())\n```\n")
        out.append("| name | value |\n|---|---|\n" + "".join(f"| item{c}-{i} | {i} |\n" for i in range(6)))
        out.append("- first point\n  with a second line\n- second point\n  - nested one\n  - nested two\n- third point\n")
    return "\n".join(out)


# -- the exact pieces ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [150, 400, 900, 3000])
def test_whole_blocks_give_the_text_back_and_never_end_inside_a_block(limit):
    text = book(8)
    pieces = whole_blocks(text, limit)
    assert "".join(pieces) == text
    assert not any(open_fence(p) for p in pieces)
    for p in pieces[:-1]:
        last = p.rstrip().splitlines()[-1]
        assert not last.startswith("|") or p.rstrip().endswith("|")             # a table row is whole
    nxt = [p for p in pieces[1:] if p.startswith(("  with a second line", "  - nested", "| item"))]
    assert not nxt                                                              # no piece starts in the middle of a list item or a table


def test_a_block_longer_than_the_limit_is_a_piece_of_its_own_and_is_not_cut():
    code = "```\n" + "line of code\n" * 50 + "```\n"
    pieces = whole_blocks("before\n\n" + code + "\nafter\n", 100)
    assert "".join(pieces) == "before\n\n" + code + "\nafter\n"
    assert any(p.startswith("```") and p.rstrip().endswith("```") and len(p) > 100 for p in pieces)


def test_the_lines_a_window_can_show_stop_between_blocks():
    lines = ["intro", "", "```", "a", "", "b", "```", "", "| h | h |", "|---|---|", "| 1 | 2 |", "| 3 | 4 |", "", "- one", "  more", "- two"]
    assert safe_line_end(lines, 5) == 1                  # 5 would cut the code block: only "intro" is shown
    assert safe_line_end(lines, 11) == 7                 # the table would be cut after its first row: the code block ends the window
    assert safe_line_end(lines, 12) == 12                # the table ends exactly there
    assert safe_line_end(lines, 14) == 12                # "- one" without its continuation line would be half an item
    assert safe_line_end(lines, 15) == 15                # the whole item
    assert safe_line_end(["x", "y"], 5) == 2             # all of it fits
    assert safe_line_end(["```", "a", "b"], 2) == 2      # one block longer than the window: nothing better exists


def test_two_pieces_written_apart_are_joined_by_what_they_continue():
    assert joiner("Some paragraph.", "Another one.") == "\n\n"
    assert joiner("- one\n- two", "- three\n- four") == "\n"
    assert joiner("1. one\n2. two", "3. three") == "\n"
    assert joiner("- one", "A paragraph.") == "\n\n"
    assert joiner("| a | b |\n|---|---|\n| 1 | 2 |", "| 3 | 4 |\n| 5 | 6 |") == "\n"
    assert joiner("| a | b |\n|---|---|\n| 1 | 2 |", "| c | d |\n|---|---|\n| 3 | 4 |") == "\n\n"       # a new table has its own header
    assert lf.join_parts(["A.", "- x", "- y", "- z", "End."]) == "A.\n\n- x\n- y\n- z\n\nEnd."
    assert lf.join_parts([]) == "" and lf.join_parts(["only"]) == "only"


def test_a_part_is_delivered_in_pieces_that_joined_are_the_part():
    part = book(6)
    pieces = lf.delivery("\n\n", part)
    assert len(pieces) > 1 and "".join(pieces) == "\n\n" + part
    assert not any(open_fence(p) for p in pieces)
    assert lf.delivery("", "short") == ["short"]


# -- a long text read in parts ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [700, 1500, 4000])
def test_the_parts_of_a_text_read_in_parts_do_not_end_inside_a_code_block_table_or_list_item(limit):
    text = book(20)
    parts = digest.split(text, limit)
    assert all(len(p) <= limit for p in parts) and "".join(parts) == text
    assert not any(open_fence(p) for p in parts)
    for p in parts[1:]:
        assert not p.startswith(("  with a second line", "  - nested", "| item", "    ", "print(f())"))


def test_a_code_block_longer_than_a_part_is_cut_at_lines_and_each_part_closes_and_reopens_it():
    text = "```python\n" + "".join(f"value_{i} = compute({i})\n" for i in range(300)) + "```\n"
    parts = digest.split(text, 1000)
    assert len(parts) > 1 and all(len(p) <= 1000 and p.startswith("```python\n") and p.rstrip().endswith("```") for p in parts)
    assert sum(p.count("compute(") for p in parts) == 300


def test_a_limit_too_small_for_a_fence_falls_back_to_cutting_the_plain_way():
    text = "```\n" + "x = 1\n" * 40 + "```\n"
    assert "".join(digest.split(text, 6)) == text


# -- an attachment shown to the model ------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return at.AttachmentStore(tmp_path / "att")


def put(store, name, data):
    tmp = store.dir / "up.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    return store.add("s1", name, tmp)


def shown_text(message):
    return re.search(r"<document_content>(.*?)</document_content>", message, re.S).group(1)


def test_the_start_of_a_big_document_shown_in_the_message_ends_between_blocks(store):
    text = book(60)
    a = put(store, "libro.md", text.encode())
    msg, _ = at.compose_message(store, "s1", [a["id"]], "riassumi", 4096)
    assert "<shown>lines 1-" in msg and "## Chapter 59" not in msg
    cut = int(msg.split("<shown>lines 1-")[1].split(" of ")[0])
    lines = text.split("\n")
    assert 0 < cut < len(lines)
    assert not open_fence("\n".join(lines[:cut]))                              # no code block left open
    assert lines[cut].strip() == ""                                            # the cut is at a gap between blocks, not inside a table or an item
    assert "\n".join(lines[:cut]) in shown_text(msg)


def test_a_document_with_no_block_boundary_inside_the_room_is_cut_at_a_line_as_before(store):
    big = "\n".join(f"riga {i} con del testo di riempimento" for i in range(5000)).encode()
    a = put(store, "grande.txt", big)
    msg, _ = at.compose_message(store, "s1", [a["id"]], "cerca", 8192)
    assert "<shown>lines 1-" in msg and "of 5000</shown>" in msg


def test_a_read_of_an_attachment_stops_between_blocks_and_says_where_to_go_on():
    text = book(10)
    out = at.read_range(text, 1, None, 1200)
    shown = [ln.split(": ", 1)[1] if ": " in ln else ln for ln in out.split("\n")[1:]]
    assert not open_fence("\n".join(shown))
    nxt = int(re.search(r"continue from line (\d+)", out).group(1))
    following = text.split("\n")[nxt - 1]
    assert not following.startswith(("  with a second line", "  - nested", "| item", "    ", "print(f())"))
    again = at.read_range(text, nxt, None, 1200)
    assert again.startswith(f"Lines {nxt}-")


def test_a_read_with_an_end_the_caller_asked_for_is_not_moved():
    text = book(10)
    out = at.read_range(text, 1, 13)
    assert out.startswith("Lines 1-13 of ") and len(out.split("\n")) == 14


class Parts:
    """A client that writes the parts given, one per call."""

    def __init__(self, parts):
        self.parts, self.calls = parts, []

    def chat_completion(self, messages, max_tokens=None, extra=None, timeout=0, on_delta=None, **_):
        self.calls.append(messages)
        text = self.parts[len(self.calls) - 1]
        return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}}


def test_a_long_answer_is_delivered_in_pieces_between_blocks_and_joined_as_one_text():
    first = book(10) + "\n- last point of part one\n- another point"
    second = "- the list goes on in part two\n- and on\n\n" + book(3)
    outline = [{"heading": "One", "goal": "a", "target_words": 700, "key_points": []}, {"heading": "Two", "goal": "b", "target_words": 700, "key_points": []}]
    spec = lf.Spec.from_request({"mode": "write", "task": "Write about rivers", "target_words": 1400, "segment_words": 700, "outline": outline, "retries": 0}, "")
    run = lf.LongAnswer(Parts([first, second]), spec)
    deltas = [e["text"] for e in run.run() if e and e["event"] == "delta"]
    assert len(deltas) > 2
    assert "".join(deltas) == run.text() == first.strip() + "\n" + second.strip()    # the list goes on across the seam: a line end, not a blank line
    assert not any(open_fence(d) for d in deltas)
    assert all(len(d) < lf.DELIVERY_CHARS * 1.6 for d in deltas)
    for d in deltas[1:]:
        assert not d.lstrip("\n").startswith(("  with a second line", "  - nested", "| item", "print(f())"))     # no piece starts inside a block


def test_the_brief_asks_each_part_to_end_after_a_whole_block():
    spec = lf.Spec.from_request({"mode": "write", "task": "t", "target_words": 1000}, "")
    assert "never inside one" in lf.brief(spec, [lf.Segment(0, words=1000)])


def test_a_source_with_a_code_block_that_is_not_protected_is_not_cut_inside_it():
    code = "```python\ndef f():\n    x = 1\n\n    return x\n```"
    source = "\n\n".join(["Intro paragraph. " * 150, code, "Closing paragraph. " * 150])
    spec = lf.Spec.from_request({"mode": "translate", "target_language": "Italian", "source": source, "segment_words": 200, "protect": False}, "")
    plan = lf.source_plan(spec, source, lf.Protected())
    assert len(plan) > 1 and not any(open_fence(s.source) for s in plan)
    assert sum(s.source.count("return x") for s in plan) == 1


def test_a_text_with_no_blank_line_is_read_as_before():
    text = "\n".join(f"riga {i}" for i in range(500))
    out = at.read_range(text, 1, None, 12000)
    assert out.startswith("Lines 1-200 of 500 (continue from line 201)")
