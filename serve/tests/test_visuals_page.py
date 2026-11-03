"""Pictures and diagrams in the page: only pictures the agent fetched are shown, diagrams are drawn by a vendored library loaded on demand,
and each of the two has its own switch and level."""
import re
import urllib.error
import urllib.request
from pathlib import Path

from serve.tests import test_api_plans as base

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
LEVELS = ("very_low", "low", "normal", "high", "very_high", "highest")


class VisualsPageTest(base.PlansApiBase):
    def fetch(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read(64)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), b""

    def test_the_vendored_library_is_served_from_its_own_folder_and_nothing_else_leaks_out_of_it(self):
        code, ctype, head = self.fetch("/web/vendor/mermaid.min.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)
        for path in ("/web/vendor/LICENSE-mermaid.txt", "/web/vendor/../index.html", "/web/vendor/%2e%2e/app.js", "/web/vendor/a/b.js", "/web/vendor/missing.js"):
            self.assertEqual(self.fetch(path)[0], 404, path)

    def test_a_picture_in_an_answer_comes_only_from_the_conversations_own_files(self):
        self.assertIn("tadamoo-file:([a-f0-9]{12})", CHAT)
        self.assertEqual(len(re.findall(r"!\\\[", CHAT)), 1)                  # the only picture rule: none loads a picture from an address
        self.assertEqual(set(re.findall(r"\(basalt-(\w+):", CHAT)), {"file", "path"})
        self.assertIn("api/outputs/${encodeURIComponent(sid)}/${id}/file", CHAT)
        self.assertIn("headers: authHeaders()", CHAT[CHAT.index("async function loadPictures"):CHAT.index("const drawn = new Map()")])
        self.assertIn("window.chatUi.sessionId = () => sid;", AGENT)

    def test_a_diagram_is_drawn_with_the_strict_setting_once_its_block_is_closed_and_kept_for_the_next_redraw(self):
        self.assertIn('securityLevel: "strict"', CHAT)
        self.assertIn('data-ready="${end ? 1 : 0}"', CHAT)
        self.assertIn("if (drawn.has(code))", CHAT)
        self.assertIn('s.src = "web/vendor/mermaid.min.js";', CHAT)
        self.assertNotIn("mermaid.min.js", HTML)                                   # loaded only when an answer holds a diagram

    def test_each_has_its_own_switch_and_six_levels_and_the_conversation_is_made_with_them(self):
        for box in ("cs-img-on", "cs-chart-on", "cs-img-level", "cs-chart-level"):
            self.assertIn(f'id="{box}"', HTML)
        for level in LEVELS:
            self.assertEqual(HTML.count(f'data-t="vis.level.{level}"'), 2, level)
            self.assertEqual(I18N.count(f'"vis.level.{level}":'), 2, level)
        self.assertIn("worktree: copyMode(), ...visuals(),", AGENT)
        self.assertIn('images: vis.imgOn.checked ? vis.imgLevel.value : ""', AGENT)
        for key in set(re.findall(r'data-t="(vis\.[a-z.]+)"', HTML)) | {"mmd.source", "mmd.error"}:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
