"""The block of the Knowledge tab that keeps an archive up to date: the page runs on a stand-in DOM in node, the strings, the wiring and the styles are read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
JS = (WEB / "ragsync.js").read_text(encoding="utf-8")
CSS = (WEB / "ragsync.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
RAG = (WEB / "rag.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagSyncPageTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is needed to run the page on the stand-in DOM")
    def test_the_block_works_on_a_stand_in_dom(self):
        out = subprocess.run([NODE, str(HERE / "ragsync_dom_check.js")], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 9)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_every_string_exists_in_both_languages(self):
        keys = set(re.findall(r'T\("(rs\.[a-z0-9.]+)"', JS))
        keys |= {f"rs.stage.{s}" for s in ("checking", "waiting", "fetching", "reading", "cutting", "indexing", "saving", "comparing", "recutting")}
        keys |= {f"rs.kind.{k}" for k in ("folder", "file", "url", "mediawiki", "wikibridge")} | {f"rs.level.{k}" for k in ("error", "warn", "info")}
        keys |= {f"rs.sched.act.{k}" for k in ("check", "update")} | {"rs.sched.check", "rs.sched.update", "rs.f.file", "rs.f.folder"}
        keys |= {f"rs.state.{k}" for k in ("changed", "new", "gone", "error", "pinned", "unchanged", "updated", "added", "linked", "failed", "wouldupdate", "wouldadd")}
        keys -= {"rs.stage.", "rs.kind.", "rs.level."}
        self.assertGreater(len(keys), 100)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_documents_added_by_hand_are_not_reported_as_an_archive_with_no_source(self):
        self.assertIn('cur && cur.documents ? T("rs.sources.byhand", cur.documents) : T("rs.sources.none")', JS)
        self.assertEqual(I18N.count('"rs.sources.byhand":'), 2)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_it_is_wired_into_the_page_and_the_archive_view(self):
        self.assertLess(HTML.index("web/ragsync.js"), HTML.index("web/rag.js"))
        self.assertIn('href="web/ragsync.css"', HTML)
        self.assertIn("window.ragSync.block(a, { reload: reloadAll })", RAG)
        self.assertIn("window.ragSync.docLines(d)", RAG)

    def test_removing_anything_takes_two_presses_and_a_phone_gets_big_targets(self):
        for armed in ('armed === "g" + it.doc', 'armed === "s" + s.id', 'armed === "r" + r.id', "armed === key"):
            self.assertIn(armed, JS)
        self.assertRegex(CSS, r"@media \(max-width: 520px\) \{ \.rs-item-b \.btn \{ flex: 1 1 100%; \} \}")
        self.assertIn("overflow-wrap: anywhere", CSS)


if __name__ == "__main__":
    unittest.main()
