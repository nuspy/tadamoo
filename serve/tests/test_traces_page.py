"""The session traces section of the LOGS view: wired into the page, uses only the traces routes, writes no markup, and every string exists in both languages."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "traces.js").read_text(encoding="utf-8")
CSS = (WEB / "traces.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class TracesPageTest(unittest.TestCase):
    def test_the_section_sits_in_the_logs_view_and_the_files_are_linked(self):
        logs = HTML[HTML.index('id="view-logs"'):HTML.index("</main>")]
        for needle in ('id="tr-switch"', 'id="tr"', 'id="tr-sel"', 'id="tr-replay"', 'id="tr-export"', 'id="tr-verify"', 'id="rl"'):
            self.assertIn(needle, logs)
        self.assertIn("web/traces.css", HTML)
        self.assertIn("web/traces.js", HTML)

    def test_each_action_goes_to_its_route(self):
        for call in ('"api/agent/traces"', "api/agent/traces/${encodeURIComponent(sel.value)}/replay", "api/agent/traces/${encodeURIComponent(sel.value)}/export",
                     "api/agent/traces/${encodeURIComponent(sel.value)}/audit"):
            self.assertIn(call, JS)

    def test_the_script_writes_no_markup_and_sends_the_key(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        self.assertIn("authHeaders()", JS)

    def test_every_text_exists_in_english_and_italian_and_a_state_is_never_only_a_colour(self):
        keys = set(re.findall(r'T\("(tr\.[a-z_.]+)"', JS)) | set(re.findall(r'data-t(?:-aria)?="(tr\.[a-z_.]+)"', HTML))
        keys |= {f"tr.kind.{k}" for k in ("tool", "arguments", "decision", "result", "missing", "extra", "model")}
        keys |= {f"tr.out.{k}" for k in ("ok", "error", "refused", "cancelled", "started")} | {f"tr.state.{k}" for k in ("ok", "bad", "note")}
        keys |= {"tr.mode.default", "tr.mode.read_only", "tr.mode.auto"}
        keys = {k for k in keys if not k.endswith(".")}                                      # prefixes completed at run time are listed above
        self.assertGreater(len(keys), 30)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertIn("T(\"tr.state.\" + state)", JS)                                      # the word sits next to the colour

    def test_the_targets_are_big_enough_and_the_layout_folds_on_a_phone(self):
        self.assertGreaterEqual(CSS.count("min-height: 44px"), 3)
        self.assertIn("@media (max-width: 640px)", CSS)
        self.assertNotIn(":hover", CSS)


if __name__ == "__main__":
    unittest.main()
