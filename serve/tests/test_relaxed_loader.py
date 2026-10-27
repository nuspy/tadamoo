"""Relaxed drafting in the Loader: the field and its checks, what the plan says when it would do nothing, and the page's rule that
switching it on sets the settings it needs and switching it off gives back what it changed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve import config, loader, settings

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
ROOMY = {"ram_total": 128 << 30, "ram_available": 120 << 30, "commit_limit": 144 << 30, "commit_used": 10 << 30,
         "vram_total": 32 << 30, "vram_free": 31 << 30}


class TheField(unittest.TestCase):
    def test_a_threshold_above_zero_up_to_one_is_taken_and_anything_else_is_refused(self):
        for given, kept in ((0.5, 0.5), ("0.3", 0.3), (1, 1.0), (True, 0.5), (" 0.2 ", 0.2)):
            self.assertEqual(settings.check_value("engine", "relaxed-drafting", given), kept, given)
        for bad in (0, "0", False, 1.5, -0.1, "", "often", None, [0.5]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.check_value("engine", "relaxed-drafting", bad)
        self.assertIn(("engine", "relaxed-drafting"), settings.LOADER_FIELDS)

    def test_it_is_written_to_the_file_as_a_number_and_removed_when_off(self):
        raw = {"engine": {"spec": 7, "relaxed-drafting": True}}
        new, changed = settings.merge(raw, {"engine.relaxed-drafting": 0.3}, settings.LOADER_FIELDS)
        self.assertEqual((new["engine"]["relaxed-drafting"], changed), (0.3, ["engine.relaxed-drafting"]))
        off, changed = settings.merge(new, {"engine.relaxed-drafting": None}, settings.LOADER_FIELDS)
        self.assertEqual(("relaxed-drafting" in off["engine"], off["engine"]["spec"], changed), (False, 7, ["engine.relaxed-drafting"]))

    def test_the_server_reads_it_from_the_file_and_the_page_gets_it_back(self):
        self.assertEqual(settings.values_of({"engine": {"relaxed-drafting": 0.5}}, settings.LOADER_FIELDS)["engine.relaxed-drafting"], 0.5)
        words = config.engine_words("relaxed-drafting", 0.3)
        self.assertEqual(words, ["--relaxed-drafting", "0.3"])


class ThePlan(unittest.TestCase):
    base = {**loader.safe(loader.DEMO_FACTS, ROOMY), "server.vision": "off"}

    def plan(self, **kw):
        return loader.plan({**self.base, **kw}, kw.pop("facts", loader.DEMO_FACTS), ROOMY)["relaxed"]

    def test_off_says_nothing(self):
        self.assertEqual(self.plan(), {"p": 0.0, "effective": True, "why": []})

    def test_on_with_the_drafter_and_one_slot_acts(self):
        self.assertEqual(self.plan(**{"engine.relaxed-drafting": 0.5}), {"p": 0.5, "effective": True, "why": []})
        self.assertEqual(self.plan(**{"engine.relaxed-drafting": True})["p"], 0.5)

    def test_it_says_why_it_would_do_nothing(self):
        self.assertEqual(self.plan(**{"engine.relaxed-drafting": 0.3, "engine.spec": 0})["why"], ["spec"])
        self.assertEqual(self.plan(**{"engine.relaxed-drafting": 0.3, "server.parallel": 2})["why"], ["parallel"])
        no_drafter = loader.plan({**self.base, "engine.relaxed-drafting": 0.3}, {**loader.DEMO_FACTS, "has_mtp": False}, ROOMY)["relaxed"]
        self.assertEqual(no_drafter["why"], ["mtp"])
        self.assertFalse(no_drafter["effective"])
        both = self.plan(**{"engine.relaxed-drafting": 0.3, "engine.spec": 1, "server.parallel": 3})
        self.assertEqual(both["why"], ["spec", "parallel"])


@unittest.skipUnless(NODE, "node is needed to run the page's rule")
class ThePageRule(unittest.TestCase):
    def run_js(self, body: str):
        script = (f"require({json.dumps(str(WEB / 'relaxed.js'))});const R=globalThis.BasaltRelaxed;" + body)
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_switching_on_sets_what_it_needs_and_remembers_what_there_was(self):
        got = self.run_js('''const cur={mtp:false,"engine.spec":"","server.parallel":"3","server.slot-full-context":true};
          console.log(JSON.stringify(R.enter(cur)))''')
        self.assertEqual(got["set"], {"mtp": True, "engine.spec": "7", "server.parallel": "1", "server.slot-full-context": False})
        self.assertEqual(got["before"], {"mtp": False, "engine.spec": "", "server.parallel": "3", "server.slot-full-context": True})

    def test_it_no_longer_touches_the_drafting_floor(self):
        got = self.run_js('''const cur={mtp:false,"engine.spec":"","server.parallel":"3","server.slot-full-context":true,"engine.spec-min-p":""};
          const u=R.enter(cur);
          console.log(JSON.stringify({names:R.NAMES,u,l:R.leave({mtp:true,"engine.spec":"7","server.parallel":"1","server.slot-full-context":false,"engine.spec-min-p":"0.5"},u)}))''')
        self.assertNotIn("engine.spec-min-p", got["names"])
        self.assertNotIn("engine.spec-min-p", got["u"]["set"])
        self.assertNotIn("engine.spec-min-p", got["u"]["before"])
        self.assertNotIn("engine.spec-min-p", got["l"]["restore"])
        self.assertNotIn("engine.spec-min-p", got["l"]["kept"])

    def test_what_already_fits_is_left_alone(self):
        got = self.run_js('''const cur={mtp:true,"engine.spec":"5","server.parallel":"","server.slot-full-context":false};
          console.log(JSON.stringify(R.enter(cur)))''')
        self.assertEqual(got, {"before": {}, "set": {}})

    def test_switching_off_gives_back_what_it_changed_and_keeps_what_was_changed_since(self):
        got = self.run_js('''const cur={mtp:false,"engine.spec":"","server.parallel":"3","server.slot-full-context":true};
          const u=R.enter(cur);
          const now={mtp:true,"engine.spec":"7","server.parallel":"1","server.slot-full-context":false};
          const a=R.leave(now,u);
          const b=R.leave({...now,"engine.spec":"6"},u);
          console.log(JSON.stringify({a,b,holds:R.holds(now,u),gone:R.holds({...now,"server.parallel":"2"},u),none:R.holds(now,null)}))''')
        self.assertEqual(got["a"]["restore"], {"mtp": False, "engine.spec": "", "server.parallel": "3", "server.slot-full-context": True})
        self.assertEqual(got["a"]["kept"], [])
        self.assertEqual(got["b"]["kept"], ["engine.spec"])
        self.assertNotIn("engine.spec", got["b"]["restore"])
        self.assertEqual(got["b"]["restore"]["server.parallel"], "3")
        self.assertEqual((got["holds"], got["gone"], got["none"]), (True, False, False))


class ThePage(unittest.TestCase):
    def test_the_page_loads_the_rule_before_the_loader_and_has_the_field(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("web/relaxed.js"), html.index("web/loader.js"))
        js = (WEB / "loader.js").read_text(encoding="utf-8")
        self.assertIn('name: "engine.relaxed-drafting", kind: "choice"', js)
        for part in ("rxChanged", "rxResync", "BasaltRelaxed.enter", "BasaltRelaxed.leave", 'id="rx-note"'):
            self.assertIn(part, js, part)
        for name in ("loader.js", "relaxed.js", "wizard.js", "i18n.js"):
            self.assertNotIn("spec-min-p", (WEB / name).read_text(encoding="utf-8"), name)

    def test_every_text_it_shows_is_in_english_and_italian(self):
        js = (WEB / "loader.js").read_text(encoding="utf-8") + (WEB / "relaxed.js").read_text(encoding="utf-8")
        i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        en, it = i18n.index("  en: {"), i18n.index("  it: {")
        keys = {k for k in re.findall(r'T\("(rx\.[a-z.-]+)"', js) if not k.endswith(".")} | {f"rx.why.{n}" for n in ("mtp", "engine.spec", "server.parallel", "server.slot-full-context")}
        keys |= {"rx.no.spec", "rx.no.mtp", "rx.no.parallel", "f.engine.relaxed-drafting", "h.engine.relaxed-drafting"}
        keys |= {f"o.engine.relaxed-drafting.{v}" for v in ("0.5", "0.3", "0.2")}
        self.assertGreater(len(keys), 15)
        for key in sorted(keys):
            self.assertIn(f'"{key}":', i18n[en:it], key)
            self.assertIn(f'"{key}":', i18n[it:], key)


if __name__ == "__main__":
    unittest.main()
