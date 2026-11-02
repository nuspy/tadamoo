"""Joining a reply the output limit cut with the reply that goes on from it: one table of cases, run through the Python functions
(harness/continuation.py) and through the Chat page's copy of them (serve/web/cuttext.js, in node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.continuation import cut_point, join_continuation

WEB = Path(__file__).resolve().parents[1] / "serve" / "web"
NODE = shutil.which("node")
LONG = "Rome was not built in a day, and the people who built it knew that every wall needed patience, so they began with the foundations and went up"

# (cut reply, new reply, kept up to, joined text)
JOINS = [
    ("The international rel", "relations of states are old.", "The international ", "The international relations of states are old."),
    ("The international relations", "relations of states are old.", "The international ", "The international relations of states are old."),
    ("The international rel", "international relations of states", "The international ", "The international relations of states"),
    ("The first half of the", "the answer, and the rest.", "The first half of ", "The first half of the answer, and the rest."),
    ("One two three four ", "five six.", "One two three four ", "One two three four five six."),
    ("One two three four ", " five six.", "One two three four ", "One two three four five six."),                     # no doubled space
    ("One two three four\n\n", "\n\nFive.", "One two three four\n\n", "One two three four\n\nFive."),                    # no second break
    ("One two three four\n", "\n\n\nFive.", "One two three four\n", "One two three four\n\nFive."),
    ("One two three four\n\nFive", "Five six.", "One two three four\n\n", "One two three four\n\nFive six."),
    (LONG, "went up one course at a time.", LONG[:-2], LONG + " one course at a time."),                                # the model began a little early
    (LONG, "up one course at a time.", LONG[:-2], LONG + " one course at a time."),
    ("A very long word:\nhttps://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "/bbb and more.",
     "A very long word:\nhttps://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "A very long word:\nhttps://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbb and more."),
    ("Single", "Single word text goes on.", "Single", "Single word text goes on."),
    ("Text so far.", "   ", "Text so ", "Text so far."),                                                              # nothing came: nothing changes
    ("", "Starts here.", "", "Starts here."),
    ("a | b |\n| 1 | 2", "| 1 | 2 |\n| 3 | 4 |", "a | b |\n| 1 | ", "a | b |\n| 1 | 2 |\n| 3 | 4 |"),
]


@pytest.mark.parametrize("cut,new,kept,joined", JOINS)
def test_the_cut_point_and_the_join(cut, new, kept, joined):
    assert cut[:cut_point(cut)] == kept
    assert join_continuation(cut, new) == joined


def test_a_new_reply_that_shares_a_few_characters_with_the_end_is_not_cut_short():
    assert join_continuation("He went up the hill and ", "up was where he stayed.") == "He went up the hill and up was where he stayed."
    assert join_continuation("So we said that ", "that was all.") == "So we said that that was all."               # under the repeat minimum


@pytest.mark.skipif(not NODE, reason="node is needed to run the page's script")
def test_the_page_script_gives_the_same_answers():
    script = ("const vm=require('vm'),fs=require('fs');const ctx={};vm.createContext(ctx);"
              f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'cuttext.js'))},'utf8')+';this.C=cutText;',ctx);"
              f"const rows={json.dumps([[c, n] for c, n, _, _ in JOINS])};"
              "console.log(JSON.stringify(rows.map(([c,n])=>[ctx.C.cutPoint(c),ctx.C.joinCut(c,n)])))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    for (cut, new, kept, joined), (point, text) in zip(JOINS, json.loads(out.stdout)):
        assert cut[:point] == kept, cut
        assert text == joined, (cut, new)
