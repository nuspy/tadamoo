"""A page reaches the model as lines of text, so that the article is there and the menus are not."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.context import TokenJuiceCompactor
from harness.tools.search import format_page, readable

ARTICLE = "\n".join(f"Paragraph {i}: " + "the army reported movements near the town of Kupiansk. " * 6 for i in range(120))
MENU = "\n".join(f"-\n[{name}]" for name in ("Home", "News", "Sport", "Business", "Live")) + "\n\n- []\n\n- []\n"


def page(text, truncated=False):
    return {"final_url": "https://news.example/", "title": "News", "status": 200, "truncated": truncated, "text": text}


def test_a_page_reaches_the_model_as_lines_with_its_menus_taken_out():
    headline = "[Kyiv targets Russian-occupied Crimea as UK and France hold joint military exercise](https://news.example/a1)"
    shown = format_page(page(MENU + headline + "\n\n" + ARTICLE + "\n\n-\n[Contact]\n[Help]"))
    assert shown.startswith("URL: https://news.example/\nTitle: News\nStatus: 200\n\n")
    assert "[Home]" not in shown and "[Contact]" not in shown and "- []" not in shown
    assert headline in shown and "Paragraph 119" in shown
    assert max(len(line) for line in shown.split("\n")) < 2000                      # no line is long enough to be cut in the middle


def test_blank_runs_collapse_and_a_cut_page_says_so():
    assert readable("a\n\n\n\nb") == "a\n\nb"
    assert "the rest was cut" in format_page(page("x", truncated=True))


def test_what_the_context_keeps_of_a_long_page_is_its_text_and_not_its_menu(tmp_path):
    kept = TokenJuiceCompactor(tmp_path).compress_output(format_page(page(MENU + ARTICLE)), "browse_url")
    assert "Paragraph 0" in kept and "[Home]" not in kept
