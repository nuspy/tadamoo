"""The Knowledge tab: the markup, the scripts that use the routes, the texts in both languages, and a layout made for a phone first."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "rag.js").read_text(encoding="utf-8")
CSS = (WEB / "rag.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿]")


class RagPageTest(unittest.TestCase):
    def test_the_tab_the_view_the_script_and_the_style_are_in_the_page(self):
        self.assertRegex(HTML, r'<button class="tab" role="tab" id="tab-rag" data-view="rag" aria-controls="view-rag" aria-selected="false" tabindex="-1" data-t="tab\.rag">')
        self.assertRegex(HTML, r'<div class="view rg" id="view-rag" role="tabpanel" aria-labelledby="tab-rag" hidden>')
        self.assertIn('id="rg-root"', HTML)
        self.assertIn('<script defer src="web/rag.js"></script>', HTML)
        self.assertIn('href="web/rag.css"', HTML)
        views = re.search(r"const VIEWS = \[([^\]]*)\]", APP).group(1)
        self.assertIn('"rag"', views)
        self.assertNotIn("rag", re.search(r"const TAB_OF = \{([^}]*)\}", APP).group(1))              # a top-level tab, not a page under another

    def test_it_is_a_top_level_tab_before_canary_and_settings(self):
        tabs = re.findall(r'<button class="tab" role="tab" id="tab-(\w+)" data-view=', HTML)
        self.assertLess(tabs.index("loader"), tabs.index("rag"))
        self.assertLess(tabs.index("rag"), tabs.index("canary"))

    def test_every_route_of_the_server_is_used(self):
        for route in ('"api/rag/projects"', "`api/rag?project=${encodeURIComponent(project)}`", '"api/rag/recall"', "`api/rag/archives/${encodeURIComponent(chosen)}`", '"api/rag/archives"', '"api/rag/search"',
                      "`api/rag/archives/${encodeURIComponent(chosen)}/update`", "`api/rag/archives/${encodeURIComponent(chosen)}/delete`",
                      "`api/rag/archives/${encodeURIComponent(chosen)}/documents`", "`api/rag/archives/${encodeURIComponent(chosen)}/reindex`",
                      "/documents/${encodeURIComponent(doc.id)}/remove`",
                      "/upload?name=${encodeURIComponent(f.name)}`"):
            self.assertIn(route, JS)
        self.assertIn("authHeaders()", JS)
        self.assertIn("{ confirm: true }", JS)
        self.assertEqual(JS.count("{ confirm: true }"), 1)
        self.assertNotIn("/link", JS)                                                                 # an archive is not linked to a personality

    def test_the_tab_has_a_project_picker_and_makes_archives_in_the_picked_project(self):
        self.assertIn('const sel = el("select");', JS)
        self.assertIn('field("rg-project", T("rg.proj.label")', JS)
        self.assertIn('api("POST", "api/rag/archives", { name, description, project })', JS)
        self.assertIn("window.agentChat.record()", JS)                                                # the default is the open conversation's folder
        self.assertIn("projects.find(p => p.chat)", JS)                                               # else the Chat's own folder
        for word in ("personalit", "persona"):
            self.assertNotIn(word, JS.replace("whatever personality speaks", ""))

    def test_the_model_only_chat_adds_the_projects_passages_where_it_adds_the_personality(self):
        self.assertIn("window.ragUi = { recall };", JS)
        self.assertIn("await window.ragUi.recall(text)", CHAT)
        self.assertLess(CHAT.index("await window.personaUi.system(text)"), CHAT.index("await window.ragUi.recall(text)"))
        self.assertIn("personaText = [personaText, found]", CHAT)

    @unittest.skipUnless(NODE, "node is needed to run the page on the stand-in DOM")
    def test_the_picker_and_the_recall_work_on_a_stand_in_dom(self):
        out = subprocess.run([NODE, str(HERE / "rag_project_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 6)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)
        self.assertIn("el(\"p\", \"rg-hit-text\", hit.text)", JS)
        self.assertIn("e.textContent = text", JS)

    def test_deleting_an_archive_or_a_document_needs_two_presses(self):
        self.assertIn('if (!sure) arm("delete"); else remove();', JS)
        self.assertIn("if (!sure) arm(key); else removeDoc(d);", JS)
        self.assertIn("ARM_MS = 4000", JS)

    def test_the_banner_says_text_only_until_an_embedding_model_is_there(self):
        self.assertIn("S.embedding && S.embedding.available", JS)
        self.assertIn('on ? T("rg.hybrid", S.embedding.model) : T("rg.textonly")', JS)
        self.assertIn("Text search only: no embedding model installed.", I18N)
        self.assertIn("Solo ricerca testuale: nessun modello di embedding installato.", I18N)

    def test_the_formats_and_limits_come_from_the_server(self):
        self.assertIn("pick.accept = S.formats.join(\",\")", JS)
        self.assertIn("S.limits.document_bytes", JS)
        self.assertIn("!S.pdf", JS)

    def test_the_texts_exist_in_both_languages_and_have_no_emoji(self):
        keys = set(re.findall(r'\bT\(\s*"(rg\.[\w.]+)"', JS)) | set(re.findall(r'data-t(?:-title)?="(rg\.[\w.]+)"', HTML)) | {"tab.rag"}
        keys |= set(re.findall(r'T\(linked \? "(rg\.[\w.]+)" : "(rg\.[\w.]+)"', JS)[0]) if re.findall(r'T\(linked \? "(rg\.[\w.]+)" : "(rg\.[\w.]+)"', JS) else set()
        keys |= set(re.findall(r'T\(\w+ \? "(rg\.[\w.]+)" : "(rg\.[\w.]+)"', JS)[0:0])
        self.assertGreater(len(keys), 40)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        own = {m for m in re.findall(r'"(rg\.[\w.]+)":', I18N)}
        for key in own:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for text in (JS, CSS, "\n".join(l for l in I18N.splitlines() if '"rg.' in l or '"tab.rag"' in l)):
            self.assertIsNone(EMOJI.search(text))

    def test_every_key_the_script_asks_for_exists(self):
        asked = set(re.findall(r'"(rg\.[\w.]+)"', JS))
        known = set(re.findall(r'"(rg\.[\w.]+)":', I18N))
        self.assertEqual(sorted(asked - known), [])

    def test_the_style_is_made_for_a_phone_first(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("prefers-reduced-motion: reduce", CSS)
        self.assertIn("@media (min-width: 900px)", CSS)                                              # one column first, two on a wide window
        self.assertIn("width: 24px; height: 24px", CSS)                                               # the tick boxes, inside rows 44px high
        self.assertRegex(CSS, r"\.rg-field select \{[^}]*min-height: 44px")                              # the project picker
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertNotIn("hover", CSS)                                                                # nothing that works only with a pointer


if __name__ == "__main__":
    unittest.main()
