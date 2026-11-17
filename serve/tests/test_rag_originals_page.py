"""Open and Save as on a document of the Knowledge tab: the strings in both languages, the route it calls, and nothing written as markup or run as a page."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
RAG = (WEB / "rag.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagOriginalsPageTest(unittest.TestCase):
    def test_the_strings_exist_in_both_languages(self):
        for key in ("rg.doc.open", "rg.doc.save", "rg.doc.noorig"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
            self.assertIn(f'T("{key}")', RAG)

    def test_it_asks_the_original_route_with_the_key_and_a_page_is_only_ever_shown_as_text(self):
        self.assertIn("/documents/${encodeURIComponent(d.id)}/original", RAG)
        self.assertIn("authHeaders()", RAG[RAG.index("async function getOriginal"):RAG.index("const figs")])
        self.assertRegex(RAG, r'html: "text/plain", htm: "text/plain"')                          # a page is never given the type that would run it
        self.assertIn('window.open(href, "_blank", "noopener")', RAG)

    def test_the_buttons_are_off_with_a_reason_when_no_original_was_kept(self):
        self.assertIn("x.disabled = !d.has_original", RAG)
        self.assertIn('x.title = T("rg.doc.noorig")', RAG)


if __name__ == "__main__":
    unittest.main()
