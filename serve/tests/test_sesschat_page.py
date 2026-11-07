"""Changing a plan or a job by an instruction in its tab: the bubbles logic runs in node, the wiring of both tabs and the strings are read from the files."""
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
CHAT = (WEB / "sesschat.js").read_text(encoding="utf-8")
CRON = (WEB / "cron.js").read_text(encoding="utf-8")
PLANS = (WEB / "plans.js").read_text(encoding="utf-8")
CSS = (WEB / "sesschat.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the bubbles logic")
class BubblesTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "sesschat_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 4)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class WiringTest(unittest.TestCase):
    def test_both_tabs_mount_the_chat_with_what_they_have_selected(self):
        self.assertIn('kind: "cron"', CRON)
        self.assertIn('id: "cj-instr"', CRON)
        self.assertIn("job(S.selected)", CRON)
        self.assertIn('kind: "plan"', PLANS)
        self.assertIn('id: "pl-instr"', PLANS)
        self.assertIn('id="cj-instr-root"', HTML)
        self.assertIn("web/sesschat.css", HTML)
        self.assertLess(HTML.index("web/sesschat.js"), HTML.index("web/plans.js"))
        self.assertLess(HTML.index("web/sesschat.js"), HTML.index("web/cron.js"))

    def test_an_answer_refreshes_the_tab(self):
        self.assertIn('new CustomEvent("tadamoo:edited")', CRON)
        self.assertIn('document.addEventListener("tadamoo:edited", () => { if (S.ready) load(); })', CRON)
        self.assertIn('document.addEventListener("tadamoo:edited", () => { if (S.id) refreshSoon(); })', PLANS)

    def test_it_talks_to_the_editor_route_and_the_conversation_routes(self):
        for call in ('"api/agent/editor"', "api/agent/sessions/${encodeURIComponent(S.sid)}/history", "/status`", "/send`"):
            self.assertIn(call, CHAT)

    def test_the_box_is_labelled_and_the_button_is_big_enough(self):
        self.assertIn('label.htmlFor = opts.id + "-input"', CHAT)
        self.assertRegex(CSS, r"\.sc-send \{[^}]*min-height: 44px")
        self.assertNotRegex(CSS, r"min-width:\s*\d{4,}px")

    def test_every_string_exists_in_both_languages(self):
        used = set(re.findall(r'T\("(sc\.[\w.]+)"', CHAT + PLANS)) | set(re.findall(r'data-t="(sc\.[\w.]+)"', HTML))
        used |= set(re.findall(r'(?:emptyKey|placeholderKey|labelKey): "(sc\.[\w.]+)"', CRON + PLANS))
        used |= {"sc.pick.plan", "sc.pick.cron", "sc.empty", "sc.label", "sc.ph"}
        self.assertGreaterEqual(len(used), 12)
        for key in sorted(used):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


class CreateWiringTest(unittest.TestCase):
    def test_only_the_cron_tab_may_send_with_nothing_chosen(self):
        self.assertIn("canCreate: true", CRON)
        self.assertNotIn("canCreate", PLANS)
        self.assertIn("opts.kind && !opts.canCreate && !String(opts.target()", CHAT)

    def test_the_cron_texts_say_that_a_new_job_can_be_made_in_both_languages(self):
        for key in ("sc.title.cron", "sc.help.cron", "sc.empty.cron", "sc.ph.cron"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertEqual(I18N.lower().count("with no job selected"), 2)
        self.assertEqual(I18N.lower().count("senza job scelto"), 2)
