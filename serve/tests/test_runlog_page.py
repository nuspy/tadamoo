"""The log and results of a plan or a run as a conversation: the logic runs in node on a stand-in page, the wiring of both tabs and the strings are read from the files."""
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
LOG = (WEB / "runlog.js").read_text(encoding="utf-8")
CRON = (WEB / "cron.js").read_text(encoding="utf-8")
PLANS = (WEB / "plans.js").read_text(encoding="utf-8")
CSS = (WEB / "sesschat.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the log")
class RunLogTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "runlog_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 8)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class WiringTest(unittest.TestCase):
    def test_the_script_is_loaded_before_both_tabs(self):
        for later in ("web/plans.js", "web/cron.js"):
            self.assertLess(HTML.index("web/runlog.js"), HTML.index(later))

    def test_the_plan_shows_its_journal_and_asks_the_conversation_that_made_or_runs_it(self):
        self.assertIn("RunLog.planEntries(snap.plan.journal, snap.history", PLANS)
        self.assertIn("(snap.schedule && snap.schedule.session_ref) || snap.created_by", PLANS)
        self.assertIn("ui.chatlog", PLANS)

    def test_a_run_opens_as_a_chat_on_its_own_conversation_and_is_not_redrawn_while_written_in(self):
        self.assertIn("RunLog.mount(host", CRON)
        self.assertIn("r.session ? text => RunLog.sendToSession(r.session, text) : null", CRON)
        self.assertIn("list.contains(document.activeElement) && list.querySelector(\".cj-runchat\")", CRON)

    def test_it_talks_to_the_conversation_routes_and_puts_text_not_markup_in_the_page(self):
        self.assertIn("api/agent/sessions/${encodeURIComponent(ref)}/status", LOG)
        self.assertIn("/send`", LOG)
        self.assertNotIn("innerHTML", LOG)

    def test_the_box_is_labelled_the_button_is_big_and_a_result_stands_out(self):
        self.assertIn('label.htmlFor = o.id + "-input"', LOG)
        self.assertRegex(CSS, r"\.cj-chatbtn \{ min-height: 44px")
        self.assertRegex(CSS, r"\.sc-msg\.rl-output \{[^}]*border-left")

    def test_every_string_exists_in_both_languages(self):
        used = set(re.findall(r'T\("(rl\.[\w.]+)"', LOG + PLANS + CRON)) | set(re.findall(r'(?:emptyKey|labelKey|placeholderKey|noReplyKey): "(rl\.[\w.]+)"', LOG + PLANS + CRON))
        used |= {"rl.empty", "rl.label", "rl.ph", "rl.noreply", "rl.help.plan"}
        self.assertGreaterEqual(len(used), 15)
        for key in sorted(used):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
