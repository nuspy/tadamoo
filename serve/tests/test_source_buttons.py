"""The small button after a source of information: it opens the page in the system's browser."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")


def block(text, start, end):
    return text[text.index(start):text.index(end)]


def run_node(code):
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's script")
class SourceButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        esc = re.search(r"^  const esc = .*$", CHAT, re.M).group(0)
        markdown = block(CHAT, "  const sourceButton", "  function blocks(text)")
        cls.prefix = f'const T = k => ({{"src.btn": "source", "src.open": "Open the source"}})[k];\n{esc}\nfunction inline(s) {{}}\n' + markdown.replace("function inline(s) {}\n", "")

    def render(self, text):
        code = self.prefix + f"\nprocess.stdout.write(JSON.stringify(inline({json.dumps(text)})));"
        return run_node(code)

    def test_a_link_gets_one_button_with_the_same_address_that_opens_outside(self):
        html = self.render("see [Title](https://example.com/a?x=1&y=2) now")
        self.assertEqual(html.count('class="srcbtn"'), 1)
        self.assertEqual(html.count('href="https://example.com/a?x=1&amp;y=2"'), 2)             # the link and its button
        self.assertEqual(html.count('target="_blank" rel="noopener noreferrer"'), 2)

    def test_an_address_written_in_the_text_gets_a_button_without_its_closing_punctuation(self):
        html = self.render("source https://www.example.org/b/c. Next (https://a.example/x), done")
        self.assertIn('href="https://www.example.org/b/c"', html)
        self.assertIn('href="https://a.example/x"', html)
        self.assertEqual(html.count("srcbtn"), 2)
        self.assertRegex(html, r'↗</a>\.')

    def test_nothing_else_becomes_a_button(self):
        for text in ("javascript:alert(1)", "[x](javascript:alert(1))", "ftp://example.com/file", "a `https://in.code/x` b", "http://", "no address here"):
            self.assertNotIn("srcbtn", self.render(text), text)

    def test_an_address_cannot_close_the_attribute_or_add_markup(self):
        html = self.render('[x](https://e.com/"onmouseover=alert(1)) and https://e.com/<script>')
        self.assertNotIn("<script", html)
        self.assertNotIn('"onmouseover', html.replace("&quot;", ""))

    def test_a_search_gives_the_addresses_of_its_results_and_a_page_the_one_it_read(self):
        fn = block(AGENT, "  function sourcesOf(b)", "  function paintBlock(b, el, item)")
        code = fn + """
const out = [
  sourcesOf({ name: "search_web", args: { query: "x" }, output: '[{"title":"A","url":"https://www.news.example/a?id=1","snippet":"s"},{"title":"B","url":"http://other.example/b."},{"url":"https://www.news.example/a?id=1"},{"url":"https://user:pw@secret.example/"},{"url":"javascript:alert(1)"}]' }),
  sourcesOf({ name: "browse_url", args: { url: "https://page.example/p" }, output: "menu https://menu.example/1 https://menu.example/2" }),
  sourcesOf({ name: "shell_command", args: {}, output: "https://not.a.web.tool/" }),
  sourcesOf({ name: "search_web", args: {}, output: Array.from({ length: 12 }, (_, i) => `https://h${i}.example/`).join(" ") }).length,
];
process.stdout.write(JSON.stringify(out));"""
        found = run_node(code)
        self.assertEqual([s["host"] for s in found[0]], ["news.example", "other.example"])
        self.assertEqual(found[0][1]["url"], "http://other.example/b")
        self.assertEqual(found[1], [{"url": "https://page.example/p", "host": "page.example"}])      # not the links of the page's menus
        self.assertEqual(found[2], [])
        self.assertEqual(found[3], 6)


class SourceButtonsWiringTest(unittest.TestCase):
    css = (WEB / "app.css").read_text(encoding="utf-8")
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    def test_the_button_is_small_and_the_texts_exist_in_both_languages(self):
        self.assertRegex(self.css, r"\.srcbtn \{[^}]*font: 500 11px")
        for text in ('"src.btn": "source", "src.open": "Open the source in your browser"', '"src.btn": "fonte", "src.open": "Apri la fonte nel browser"'):
            self.assertIn(text, self.i18n)

    def test_the_tool_block_of_a_web_tool_shows_the_buttons_and_the_app_hands_new_windows_to_the_browser(self):
        self.assertIn("window.chatUi.sourceLink(s.url, s.host)", AGENT)
        app = (HERE.parents[1] / "tools" / "windows" / "desktop" / "MainForm.cs").read_text(encoding="utf-8")
        self.assertRegex(app, r"NewWindowRequested \+= \(_, e\) => \{ e\.Handled = true; Process\.Start\(new ProcessStartInfo\(e\.Uri\) \{ UseShellExecute = true \}\); \};")


if __name__ == "__main__":
    unittest.main()
