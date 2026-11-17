"""The Test button of the database block: its route, its strings in both languages, and that it never writes markup."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "ragdb.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagDbTestButtonTest(unittest.TestCase):
    def test_the_button_calls_the_test_route_with_the_form_and_shows_each_step(self):
        self.assertIn('api("POST", "api/rag/db/test"', JS)
        self.assertIn("{ project: st.project, config: cleanConfig()", JS)
        self.assertIn("function testBlock()", JS)
        self.assertIn('btn(st.testing ? T("rd.running") : T("rd.test"), "", runTest)', JS)

    def test_every_string_exists_in_both_languages(self):
        keys = {"rd.test", "rd.test.tip", "rd.test.title", "rd.test.ok", "rd.test.bad"} | {f"rd.test.step.{s}" for s in ("package", "connect", "tables", "read", "write")}
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        used = {k for k in re.findall(r'T\("(rd\.test[a-z.]*)"', JS) if not k.endswith(".")}
        self.assertTrue(used <= keys, used - keys)

    def test_what_the_server_says_enters_the_page_as_text(self):
        block = JS[JS.index("async function runTest()"):JS.index("function cleanConfig()")]
        for banned in ("innerHTML", "insertAdjacentHTML", "eval("):
            self.assertNotIn(banned, block)


if __name__ == "__main__":
    unittest.main()
