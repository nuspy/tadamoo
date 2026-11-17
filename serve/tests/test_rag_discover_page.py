"""The block of the Knowledge tab that adds from a request in words: the page runs on a stand-in DOM in node, the strings, the wiring and the styles are read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
JS = (WEB / "ragdiscover.js").read_text(encoding="utf-8")
CSS = (WEB / "rag.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
RAG = (WEB / "rag.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagDiscoverPageTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is needed to run the page on the stand-in DOM")
    def test_the_block_works_on_a_stand_in_dom(self):
        out = subprocess.run([NODE, str(HERE / "ragdiscover_dom_check.js")], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 7)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_every_string_exists_in_both_languages(self):
        keys = set(re.findall(r'T\("(rdc\.[a-z0-9.]+)"', JS)) | {"rdc.search.running", "rdc.widen.running", "rdc.import.running"}
        self.assertGreater(len(keys), 15)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_what_the_server_and_the_web_say_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_it_is_wired_into_the_page_and_the_archive_view(self):
        self.assertLess(HTML.index("web/ragdiscover.js"), HTML.index("web/rag.js"))
        self.assertIn("window.ragDiscover.block(a, { reload: reloadAll })", RAG)

    def test_a_phone_gets_big_targets_and_nothing_scrolls_sideways(self):
        self.assertRegex(CSS, r"\.rd-pick \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.rd-pick input \{[^}]*width: 24px")
        self.assertIn("overflow-wrap: anywhere", CSS[CSS.index(".rd-title"):CSS.index(".rd-title") + 120])


if __name__ == "__main__":
    unittest.main()
