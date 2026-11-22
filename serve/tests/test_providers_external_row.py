"""Models > Providers: the line "External requests through the OpenAI and Anthropic interfaces" sits right under the table of functions, before the council,
and says in both languages that only the loaded local model answers them (never the primary model or a chosen provider). Static checks of the page's own files."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
KEYS = ("pv.external", "pv.external.who", "pv.external.help")


class ExternalRequestsRowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "providers.js").read_text(encoding="utf-8")
        cls.i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    def test_the_row_comes_right_after_the_functions_and_before_the_council(self):
        call = re.search(r"primarySection\(\), functionsTable\(\), externalRequests\(\), councilSection\(\)", self.js)
        self.assertTrue(call, "the page must draw functionsTable(), externalRequests(), councilSection() in that order")

    def test_the_row_uses_its_three_texts(self):
        body = re.search(r"function externalRequests\(\) \{(.*?)\n  \}", self.js, re.S).group(1)
        for key in KEYS:
            self.assertIn(f'T("{key}")', body)

    def test_every_text_exists_in_english_and_in_italian(self):
        for key in KEYS:
            hits = re.findall(rf'"{re.escape(key)}":\s*"((?:[^"\\]|\\.)*)"', self.i18n)
            self.assertEqual(len(hits), 2, f"{key} must be in both languages")
            self.assertNotEqual(hits[0], hits[1], f"{key}: the two languages are the same text")

    def test_the_texts_say_who_answers(self):
        who = re.findall(r'"pv\.external\.who":\s*"((?:[^"\\]|\\.)*)"', self.i18n)
        self.assertIn("local engine", who[0])
        self.assertIn("Primary model", who[0])
        self.assertIn("motore locale", who[1])
        self.assertIn("Modello primario", who[1])


if __name__ == "__main__":
    unittest.main()
