"""The LOGS tab of the web page: its files are served, the tab is in the page, and every string it uses exists in
English and Italian. The browser behaviour is checked by hand against a mock-engine server. Mock engine only."""
import re
import unittest
import urllib.request
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"


class NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")

    def test_the_page_has_the_logs_tab_and_its_view(self):
        code, ctype, html = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertRegex(html, r'<button class="tab" role="tab" id="tab-logs" data-view="logs"[^>]*data-t="tab.logs">LOGS</button>')
        self.assertIn('id="view-logs" role="tabpanel" aria-labelledby="tab-logs"', html)
        self.assertIn('<script defer src="web/reqlog.js"></script>', html)
        for control in ("rl-pause", "rl-clear", "rl-save", "rl-format", "rl-q", "rl-prev", "rl-next", "rl-count", "rl-log", "rl-latest"):
            self.assertIn(f'id="{control}"', html)

    def test_the_new_script_is_served_and_the_tab_is_a_view(self):
        code, ctype, js = self.get("/web/reqlog.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)
        for call in ("api/reqlog?after=", "api/reqlog/clear", "api/reqlog/export?format="):
            self.assertIn(call, js)
        code, _, app = self.get("/web/app.js")
        self.assertRegex(app, r'const VIEWS = \[[^\]]*"logs"')
        code, ctype, css = self.get("/web/app.css")
        self.assertIn(".rl-card", css)

    def test_the_log_text_never_goes_through_innerhtml(self):
        js = (WEB / "reqlog.js").read_text(encoding="utf-8")
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(unsafe, js)
        for store in ("localStorage.setItem(", "sessionStorage"):
            for hit in re.finditer(re.escape(store), js):
                self.assertIn("FORMAT_KEY", js[hit.start():hit.start() + 40])    # only the chosen save format is kept

    def test_every_string_is_in_english_and_italian(self):
        i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        en, it = i18n.index("  en: {"), i18n.index("  it: {")
        keys = lambda part: set(re.findall(r'"((?:lg|tab)\.[a-z.]+)":', part))
        self.assertEqual(keys(i18n[en:it]) - {"tab.nav"}, keys(i18n[it:]) - {"tab.nav"})
        used = set(re.findall(r'T\("(lg\.[a-z.]+)"', (WEB / "reqlog.js").read_text(encoding="utf-8")))
        used |= set(re.findall(r'data-t(?:-aria)?="(lg\.[a-z.]+)"', (WEB / "index.html").read_text(encoding="utf-8")))
        used = {k for k in used if not k.endswith(".")}                        # the dynamic ones are listed below
        self.assertTrue(used)
        self.assertLessEqual(used, keys(i18n[en:it]))
        for state in ("live", "paused", "off", "locked", "wait"):
            self.assertIn(f'"lg.st.{state}":', i18n[en:it])
        for kind in ("request", "reasoning", "content", "attempt", "repair", "timing", "finish", "cancel", "error", "gpu",
                     "engine", "server"):
            self.assertIn(f'"lg.k.{kind}":', i18n[en:it])
        for state in ("running", "done", "error", "cancelled"):
            self.assertIn(f'"lg.s.{state}":', i18n[en:it])
        self.assertIn('"tab.logs": "LOGS"', i18n[en:it])
        self.assertIn('"tab.logs": "LOGS"', i18n[it:])


if __name__ == "__main__":
    unittest.main()
