"""Long answer stays on for a conversation until the person turns it off: the logic runs in node on a stand-in page, the wiring is read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
LONG_JS = (WEB / "longform.js").read_text(encoding="utf-8")
CHAT_JS = (WEB / "agentchat.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the control")
class StickyLongAnswerTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "longform_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 4)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class StickyWiringTest(unittest.TestCase):
    def test_nothing_turns_the_control_off_by_itself(self):
        self.assertNotIn("disarm", LONG_JS)
        self.assertNotIn("next message only", I18N.split('"la.help"', 1)[1][:600])

    def test_the_chat_tells_the_control_when_the_conversation_changes(self):
        self.assertEqual(len(re.findall(r"conversationChanged\(", CHAT_JS)), 5)
        self.assertIn("conversationChanged(true)", CHAT_JS)

    def test_the_new_box_and_the_strings_exist_in_both_languages(self):
        self.assertIn('id="la-default"', HTML)
        self.assertRegex(HTML, r'<label for="la-default" data-t="la.default"')
        for key in ("la.default", "la.on", "la.on.tip", "la.help"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
