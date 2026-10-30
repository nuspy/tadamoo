"""The page side of the steps limit, the stop notes and the actions' own sentences (static checks; node reads the translations)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
SET = (WEB / "chatset.js").read_text(encoding="utf-8")
KEYS = ("cs.max_turns", "cs.max_turns.help", "cs.max_turns.bad", "cs.live", "cs.live.none", "ch.settings.steps", "ch.settings.bias", "ch.stop.max_turns",
        "ch.stop.max_turns.h", "ch.continue", "ch.continue.text", "ch.stop.budget", "ch.stop.loop", "ch.stop.error", "ch.calls.skipped", "ch.explain.said",
        "ch.act.generic", "ch.act.shell_command", "ch.act.write_file", "ch.act.read_file", "ch.act.search_web", "ch.act.browse_url", "ch.act.plan",
        "ch.act.browser", "sl.r.steps", "sl.r.steps.none", "sl.r.bias")


def strings():
    script = ("const vm=require('vm'),fs=require('fs');"
              "const ctx={document:{documentElement:{dataset:{}}},location:{search:''},URLSearchParams,localStorage:{getItem(){return null},setItem(){}}};"
              "vm.createContext(ctx);"
              f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8')+';this.S=STRINGS;',ctx);"
              "console.log(JSON.stringify(ctx.S))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


class StepsPageTest(unittest.TestCase):
    def test_the_setting_is_in_the_chat_settings_with_a_help_line_and_never_goes_to_the_model(self):
        self.assertRegex(HTML, r'<input id="cs-max_turns"[^>]*aria-describedby="cs-max_turns-h"')
        self.assertIn('id="cs-max_turns-h" data-t="cs.max_turns.help"', HTML)
        self.assertRegex(SET, r'key: "max_turns", id: "cs-max_turns", min: -1')
        self.assertIn('f.key === "retries" || f.key === "max_turns"', SET)              # the model's request body does not carry it
        self.assertIn("patterns, live }", SET)                           # and the Agent page can read it

    def test_a_new_conversation_takes_the_values_and_a_change_reaches_the_open_one(self):
        self.assertIn("...chatSettings.live()", CHAT)
        self.assertNotIn("chatSettings.body().end_turn_bias", CHAT)
        self.assertIn("`/${sid}/settings`", CHAT)
        self.assertIn('document.addEventListener("basalt:chatset-settled"', CHAT)       # the panel's own starting values are not a change by the person

    def test_the_limit_is_said_with_a_button_to_go_on_and_the_other_stops_have_a_line(self):
        self.assertIn('["max_turns", "budget", "loop", "length"].includes(kind)', CHAT)
        self.assertIn('T("ch.continue.text")', CHAT)
        self.assertIn("paintStop(it);", CHAT)
        self.assertIn(".astop .btn { min-height: 44px", (WEB / "app.css").read_text(encoding="utf-8"))

    def test_the_models_sentence_enters_the_page_as_text_and_is_cut_short(self):
        self.assertIn(".atool-d", CHAT)
        self.assertRegex(CHAT, r"head\.textContent = actionText\(b\)")
        self.assertRegex(CHAT, r"const explainOf = v => \(typeof v === \"string\" \? v\.replace\(/\\s\+/g, \" \"\)\.trim\(\)\.slice\(0, EXPLAIN_MAX\)")
        self.assertIn("EXPLAIN_MAX = 300", CHAT)
        self.assertNotRegex(CHAT, r"atool-d[^\n]*innerHTML")
        self.assertIn('T("ch.explain.said", b.explain)', CHAT)                          # the question that asks shows it as the model's own words

    @unittest.skipUnless(NODE, "node is needed to read the translations")
    def test_the_texts_exist_in_both_languages_with_the_same_placeholders(self):
        s = strings()
        for key in KEYS:
            for lang in ("en", "it"):
                self.assertIn(key, s[lang], f"{key} in {lang}")
            self.assertEqual(sorted(re.findall(r"\{\d+\}", s["en"][key])), sorted(re.findall(r"\{\d+\}", s["it"][key])), key)
            self.assertNotEqual(s["en"][key], s["it"][key], key)
        self.assertEqual(s["en"]["ch.stop.max_turns"], "Stopped after {0} steps without finishing.")
        for lang in ("en", "it"):
            self.assertIn("derail" if lang == "en" else "deragliare", s[lang]["cs.end_turn_bias.help"])


if __name__ == "__main__":
    unittest.main()
