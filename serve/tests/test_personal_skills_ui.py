"""The page's side of the personal skills: where the buttons are, how a skill is made from requests and what the forms ask."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
PSK = (WEB / "pskills.js").read_text(encoding="utf-8")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
SLASH = (WEB / "slash.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class PersonalSkillsPageTest(unittest.TestCase):
    def test_the_list_button_comes_right_after_branches_and_the_script_is_loaded_after_the_chat(self):
        self.assertRegex(re.sub(r'\n\s*<button[^>]*id="(?:wud-toggle|tech-toggle|priv-toggle|forget-open)"[^\n]*', '', HTML), r'id="branches-open"[^\n]*\n\s*<button[^>]*id="export-open"[^\n]*\n\s*<button[^>]*id="pskills-open"')     # Export is just left of Skills
        self.assertLess(HTML.index("web/agentchat.js"), HTML.index("web/pskills.js"))
        self.assertLess(HTML.index("web/slash.js"), HTML.index("web/pskills.js"))

    def test_the_script_draws_only_text_and_asks_the_server_with_the_page_credentials(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, PSK)
        self.assertNotRegex(PSK, r'fetch\("/')                                                         # relative addresses only
        self.assertEqual(PSK.count("authHeaders()"), 2)                                              # the helper and the export download
        for route in ("api/personal-skills", "api/personal-skills/check", "api/personal-skills/preview", "api/personal-skills/import",
                      "/skill-propose", "/skill-script", "/skill`"):
            self.assertIn(route, PSK)

    def test_every_request_has_a_button_and_a_box_when_choosing_and_the_button_on_an_earlier_one_ends_the_skill_there(self):
        self.assertIn('acts.append(copyButton(() => it.text || ""), backButton(it), skillButton(it))', CHAT)
        self.assertIn("startPick(requestsOf().indexOf(it) + 1)", CHAT)                                # the request pressed is where the skill ends
        self.assertIn("n <= pick.end", CHAT)                                                         # a box beside each request up to it
        self.assertIn("pick.picked.has(n)", CHAT)
        self.assertIn("const GAP_S = 30 * 60;", CHAT)                                                # the ones that follow each other closely are checked first
        self.assertIn("window.pskills.make(sid, turns)", CHAT)

    def test_a_recording_starts_and_stops_with_two_commands_and_the_request_text_keeps_what_was_written(self):
        for name in ("skilluse", "skills", "start-learn-skill", "stop-learn-skill"):
            self.assertIn(f'name: "{name}"', SLASH)
            self.assertIn(f'"sl.d.{name}"', I18N)
        self.assertIn("startLearn()", CHAT)
        self.assertIn("stopLearn()", CHAT)
        self.assertIn('typeof m.raw === "string" ? m.raw : text', CHAT)                               # the person sees what they wrote, with their # marks

    def test_the_run_form_reads_dates_in_words_and_offers_a_picker(self):
        self.assertIn('api("POST", "api/personal-skills/preview"', PSK)
        self.assertIn('picker.type = spec.type === "date" ? "date" : "datetime-local"', PSK)
        self.assertIn("psk.pf.dateHelp", PSK)
        self.assertRegex(PSK, r'"psk\.pf\.becomes"')

    def test_the_form_runs_a_skill_with_no_approval_step_and_no_access_mode_is_asked(self):
        self.assertNotIn("approve", PSK.split("async function makePlan(")[0])           # nothing is approved before a run; the steps of a plan are approved when the skill is made
        self.assertNotIn("riskConfirm", PSK)
        self.assertNotIn("mode.value", PSK)

    def test_a_delete_asks_again_and_the_texts_exist_in_both_languages(self):
        self.assertIn('armed !== s.slug', PSK)
        keys = {k for k in re.findall(r'T\("(psk\.[a-z.]+)"', PSK + CHAT + SLASH) if not k.endswith(".")}
        self.assertGreater(len(keys), 40)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for dynamic in ("psk.type.", "psk.item."):
            for name in (("text", "number", "date", "datetime", "choice", "list", "path", "bool") if dynamic == "psk.type." else ("number", "text", "mixed")):
                self.assertEqual(I18N.count(f'"{dynamic}{name}":'), 2, dynamic + name)


if __name__ == "__main__":
    unittest.main()
