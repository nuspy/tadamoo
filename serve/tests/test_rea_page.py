"""The REA box in the Agent tab: wired into the page, every text in English and Italian, server strings only as text."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from serve import handler, rea_api

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "rea.js").read_text(encoding="utf-8")
CSS = (WEB / "rea.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
EN, IT = I18N.index("  en: {"), I18N.index("  it: {")


class Wiring(unittest.TestCase):
    def test_the_box_the_script_and_the_style_are_in_the_page(self):
        for needle in ('id="rea-box"', 'id="rea-on"', 'type="checkbox"', "web/rea.js", "web/rea.css", 'data-t="rea.title"', 'data-t="rea.hint"'):
            self.assertIn(needle, HTML)
        self.assertNotIn('id="rea-on" checked', HTML)            # off by default

    def test_the_box_is_shown_only_in_the_agent_tab(self):
        self.assertRegex(CSS, r'body\[data-view="agent"\] \.rea \{ display: block')
        self.assertRegex(CSS, r"\.rea \{ display: none")

    def test_touch_targets_are_large(self):
        self.assertIn("min-height: 44px", CSS)

    def test_the_routes_are_in_the_handler(self):
        source = Path(handler.__file__).read_text(encoding="utf-8")
        for needle in ('"/api/rea"', 'path.startswith("/api/rea/")', "rea_api.start_if_enabled", "rea_api.route_post"):
            self.assertIn(needle, source)

    def test_no_server_string_becomes_markup(self):
        self.assertNotIn("eval(", JS)
        self.assertNotIn("innerHTML", JS)


class Texts(unittest.TestCase):
    def test_every_text_is_in_both_languages(self):
        keys = set(re.findall(r'\bT\("(rea\.[A-Za-z0-9_.]+)"', JS)) | set(re.findall(r'data-t="(rea\.[A-Za-z0-9_.]+)"', HTML))
        keys |= {f"rea.t.{t['id']}" for t in rea_api.TOOLS} | {f"rea.tw.{t['id']}" for t in rea_api.TOOLS}
        keys |= {f"rea.{k}.{v}" for k in ("mcpstate", "skillstate") for v in ("on", "off")}
        keys = {k for k in keys if not k.endswith(".")}
        self.assertGreater(len(keys), 30)
        for key in sorted(keys):
            self.assertIn(f'"{key}":', I18N[EN:IT], key)
            self.assertIn(f'"{key}":', I18N[IT:], key)


if __name__ == "__main__":
    unittest.main()
