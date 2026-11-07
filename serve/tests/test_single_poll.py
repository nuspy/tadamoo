"""The Chat follows a conversation with one read at a time: two reads from the same point showed the person's message twice."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the helper")
class SingleFlightTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "singleflight_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 3)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class PollWiringTest(unittest.TestCase):
    def test_the_poll_goes_through_the_helper_and_drops_the_answer_of_another_conversation(self):
        self.assertIn("window.singleFlight(pollOnce)", CHAT)
        self.assertIn("if (mine !== sid)", CHAT)
        self.assertLess(HTML.index("web/singleflight.js"), HTML.index("web/agentchat.js"))
