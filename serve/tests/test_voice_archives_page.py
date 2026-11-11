"""The archive checklist and the consent dialog of the voice functions: the logic is run by node on a stand-in DOM (voicearchives_dom_check.js); the wiring, the texts in
both languages and the touch sizes are read from the files. No browser is needed."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve.tests.test_web_static import strings

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
JS = (WEB / "voicearchives.js").read_text(encoding="utf-8")
CSS = (WEB / "voicearchives.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
PAGES = {name: (WEB / name).read_text(encoding="utf-8") for name in ("voicetab.js", "voicemeet.js", "voice.js", "voiceplan.js")}


@unittest.skipUnless(NODE, "node is needed to run the page logic")
class ArchivePageTest(unittest.TestCase):
    def test_every_case_of_the_page_logic_passes(self):
        out = subprocess.run([NODE, str(HERE / "voicearchives_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 5)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_the_scripts_parse_and_are_loaded_before_the_pages_that_use_them(self):
        for name in ("voicearchives.js", *PAGES):
            out = subprocess.run([NODE, "-e", f"new Function(require('fs').readFileSync({json.dumps(str(WEB / name))},'utf8'))"], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, name + ": " + out.stderr)
        self.assertIn('href="web/voicearchives.css"', HTML)
        self.assertLess(HTML.index("web/voicearchives.js"), HTML.index("web/voicemeet.js"))
        self.assertIn("VoiceArchives.picker", PAGES["voicetab.js"] + PAGES["voicemeet.js"] + PAGES["voice.js"] + PAGES["voiceplan.js"])
        for name, text in PAGES.items():
            self.assertIn("VoiceArchives", text, name)

    def test_every_text_exists_in_english_and_italian(self):
        keys = set(re.findall(r'T\("(va\.[a-z0-9.]+)"', JS)) | {f"va.q.what.{h}" for h in ("spoken", "written", "shown")}
        for page in PAGES.values():
            keys |= set(re.findall(r'T\("(va\.[a-z0-9.]+|vm\.set\.private[a-z.]*)"', page))
        keys = {k for k in keys if not k.endswith(".")}
        keys |= {"va.brain.label", "va.brain.help", "va.meet.label", "va.meet.help", "va.cop.label", "va.cop.help", "va.calls.label", "va.calls.help", "va.plan.label", "va.plan.inherit",
                 "vm.set.private", "vm.set.private.help"}
        langs = strings()
        for lang in ("en", "it"):
            missing = sorted(k for k in keys if not langs[lang].get(k))
            self.assertEqual(missing, [], lang)
        for k in ("va.q.title", "va.gone"):
            self.assertIn("{0}", langs["en"][k])
            self.assertIn("{0}", langs["it"][k])

    def test_targets_are_at_least_44_pixels_and_the_dialog_cannot_be_dismissed_by_escape(self):
        self.assertIn(".va-row { display: flex; align-items: center; gap: 10px; min-height: 44px", CSS)
        self.assertIn(".va-btn { min-height: 44px; }", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn('addEventListener("cancel", e => e.preventDefault())', JS)


if __name__ == "__main__":
    unittest.main()
