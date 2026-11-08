"""The page side of making a skill from a plan: a button on a plan that ended well, the window shared with the one for a conversation, and the words in both languages."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
PLANS = (WEB / "plans.js").read_text(encoding="utf-8")
SKILLS = (WEB / "pskills.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")


class PlanSkillPageTest(unittest.TestCase):
    def test_the_button_shows_only_for_a_plan_whose_steps_all_ended_well(self):
        self.assertIn('id: "pl-skill"', PLANS)
        self.assertIn('nodes.every(n => n.status === "completed" || n.status === "skipped") && nodes.some(n => n.status === "completed")', PLANS)
        self.assertIn("window.pskills.makePlan(S.id)", PLANS)
        self.assertIn('title: T("pl.skill.tip")', PLANS)                                          # every button has a tooltip

    def test_the_window_is_the_one_of_a_conversation_and_not_a_copy_of_it(self):
        self.assertEqual(len(re.findall(r"async function makeFrom\(", SKILLS)), 1)
        self.assertIn("return makeFrom({", SKILLS)
        self.assertEqual(len(re.findall(r'const write = button\(T\("psk.make.write"\)', SKILLS)), 1)  # one writer of the script, for both
        self.assertIn("api/agent/plan-skill/propose", SKILLS)
        self.assertIn("api/agent/plan-skill/script", SKILLS)
        self.assertIn("api/agent/plan-skill/fill", SKILLS)
        self.assertIn("api/agent/plan-skill/chat", SKILLS)
        self.assertIn("makePlan,", SKILLS)

    def test_a_step_with_no_record_must_be_decided_before_the_script_is_written(self):
        self.assertIn("extra.check()", SKILLS)
        self.assertIn('T("psk.plan.need"', SKILLS)
        self.assertIn("decided[row.id] = []", SKILLS)                                             # left to the model
        self.assertIn("approved,", SKILLS.replace("approved }", "approved,"))

    def test_the_parameters_can_be_changed_by_the_form_and_by_talking(self):
        self.assertIn("setParams: fill", SKILLS)
        self.assertIn("ctx.setParams(r.json.params)", SKILLS)
        self.assertNotIn("innerHTML", SKILLS)

    def test_every_word_exists_in_both_languages(self):
        keys = set(re.findall(r'T\("((?:psk\.plan|pl\.skill)[\w.]*)"', SKILLS + PLANS)) | {f"psk.plan.how.{h}" for h in ("recorded", "words", "asks", "plan", "missing")}
        keys = {k for k in keys if not k.endswith(".")}
        self.assertGreaterEqual(len(keys), 15)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for cls in (".psk-plan", ".psk-chip.missing", ".psk-check"):
            self.assertIn(cls, CSS)
        self.assertRegex(CSS, r"\.psk-check \{[^}]*min-height: 44px")


if __name__ == "__main__":
    unittest.main()
