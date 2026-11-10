"""Diagrams the drawing library refuses are put right by rule before the model is asked: the rules run in node, the wiring is read from the files."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the rules")
class DiagramRulesTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "diagramfix_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 6)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class DiagramWiringTest(unittest.TestCase):
    def test_the_rules_load_before_the_chat_and_are_tried_before_the_model_is_asked(self):
        self.assertLess(HTML.index("web/diagramfix.js"), HTML.index("web/chat.js"))
        self.assertLess(CHAT.index("window.diagramFix.candidates(code)"), CHAT.index("const next = await repair(code, why)"))
