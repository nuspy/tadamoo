"""Models > the engine choice: the cards are wired in, open the tabs that exist, and every text is in English and Italian."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "enginepick.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
EN, IT = I18N.index("  en: {"), I18N.index("  it: {")
ENGINES = ("basalt", "strata", "llamacpp", "lmstudio", "vllm")


class Wiring(unittest.TestCase):
    def test_the_script_and_the_style_are_in_the_page_after_the_engine_tabs(self):
        self.assertLess(HTML.index("web/engines.js"), HTML.index("web/enginepick.js"))
        self.assertIn("engpick", (WEB / "engines.css").read_text(encoding="utf-8"))

    def test_every_card_opens_a_tab_the_page_has(self):
        for tab in ("sub-qwen", "sub-llamacpp", "sub-providers"):
            self.assertIn(f'id="{tab}"', HTML)
            self.assertIn(f'"{tab}"', JS)
        self.assertIn('"sub-eng-"', JS)

    def test_the_texts_exist_in_both_languages(self):
        keys = set(re.findall(r'"(ep\.[A-Za-z.]+)"', JS)) | {f"ep.{e}" for e in ENGINES} | {f"ep.d.{e}" for e in ENGINES}
        keys |= {"ep.title", "ep.help", "ep.s.ok", "ep.s.no", "ep.s.installed", "ep.s.notinstalled", "ep.s.connect"}
        for key in keys:
            if key.endswith("."):
                continue
            self.assertIn(f'"{key}":', I18N[EN:IT], f"{key} missing in English")
            self.assertIn(f'"{key}":', I18N[IT:], f"{key} missing in Italian")


if __name__ == "__main__":
    unittest.main()
