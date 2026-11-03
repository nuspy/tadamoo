"""riskConfirm (serve/web/risk.js) answers yes by itself, showing nothing, while the chat's permission mode is Automatic."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

RISK = Path(__file__).resolve().parent.parent / "web" / "risk.js"
NODE = shutil.which("node")

SCRIPT = """
const vm = require("vm"), fs = require("fs");
let shown = 0;
const doc = { createElement() { shown++; throw new Error("a dialog was built"); }, createTextNode() { return {}; }, body: { append() { shown++; } } };
const win = { T: k => k };
const ctx = vm.createContext({ window: win, document: doc, T: win.T, console });
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
(async () => {
  const out = {};
  win.tadamooAutoMode = () => true;
  out.auto = await win.riskConfirm(["x"]);
  win.tadamooAutoMode = () => false;
  try { await win.riskConfirm(["x"]); out.other = "no dialog"; } catch (e) { out.other = "dialog built"; }
  out.shown = shown;
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(NODE, "node is needed to run the page's script")
class RiskConfirmTest(unittest.TestCase):
    def test_automatic_answers_yes_without_a_dialog_and_another_mode_still_asks(self):
        out = subprocess.run([NODE, "-e", SCRIPT, str(RISK)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertTrue(got["auto"])
        self.assertEqual(got["other"], "dialog built")


if __name__ == "__main__":
    unittest.main()
