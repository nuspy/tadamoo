"""The Cron Jobs tab: the routines of the server with a list, an editor, their latest results, and a small chat that only proposes."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "cron.js").read_text(encoding="utf-8")
CSS = (WEB / "cron.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class CronPageTest(unittest.TestCase):
    def test_the_tab_follows_plans_and_the_page_is_wired(self):
        self.assertRegex(HTML, r'id="tab-plans"[^\n]*\n\s*<button[^>]*id="tab-cron" data-view="cron"')
        self.assertIn('id="view-cron"', HTML)
        self.assertIn('"cron"', re.search(r"const VIEWS = \[([^\]]*)\]", (WEB / "app.js").read_text(encoding="utf-8")).group(1))
        self.assertIn("web/cron.css", HTML)
        self.assertIn("web/cron.js", HTML)

    def test_every_job_can_be_created_changed_turned_on_and_off_run_and_deleted_through_the_routines_routes(self):
        for call in ('"api/routines"', "api/routines/${existing.id}/update", "api/routines/${j.id}/${j.enabled ? \"disable\" : \"enable\"}", "api/routines/${j.id}/run", "api/routines/${j.id}/remove",
                     "api/routines/runs?id="):
            self.assertIn(call, JS)
        self.assertIn("S.armed !== j.id", JS)                                            # a delete asks twice

    def test_a_job_has_no_mode_starts_on_and_its_limits_are_optional(self):
        for gone in ("riskConfirm", "confirm_auto", "steps.max", "MODES", "modeNow", "cj.risk", "cj.allow", "ag.mode."):
            self.assertNotIn(gone, JS)
        self.assertIn("cj.new.on", JS)
        self.assertNotIn("cj.new.off", JS)
        self.assertIn('max_turns: f.steps.value.trim() === "" ? null : Number(f.steps.value)', JS)       # empty means no limit
        self.assertIn('max_minutes: f.minutes.value.trim() === "" ? null : Number(f.minutes.value)', JS)
        self.assertNotIn('"60"', JS)                                                                   # no default time to give way

    def test_requests_that_wait_for_the_person_are_listed_and_can_be_dismissed(self):
        self.assertIn('id="cj-pending"', HTML)
        self.assertIn("api/routines/${p.id}/dismiss", JS)
        self.assertIn("S.pending = r.data.pending || []", JS)
        self.assertIn("cj-pend", CSS)

    def test_the_chat_only_proposes_and_the_proposal_fills_the_editor(self):
        self.assertIn("```job", JS)
        self.assertIn("function use(p)", JS)
        use = JS[JS.index("function use(p)"):JS.index("function paintChat()")]
        self.assertNotIn("api(", use)                                                    # using a proposal saves nothing
        self.assertIn('"v1/chat/completions"', JS)
        self.assertIn("Never say that a job was saved or is running.", JS)

    def test_the_script_writes_no_markup_and_sends_the_key_and_the_texts_exist_in_both_languages(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        self.assertGreaterEqual(JS.count("authHeaders()"), 2)
        keys = {k for k in re.findall(r'T\("(cj\.[a-z.]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t(?:-title|-aria|-ph)?="(cj\.[a-z.]+)"', HTML)) | {"tab.cron"}
        self.assertGreater(len(keys), 40)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertIn("@media (max-width: 1100px)", CSS)


    def test_a_job_can_run_a_personal_skill_with_values_that_are_read_at_each_run(self):
        for needle in ('api("GET", "api/personal-skills")', "window.pskills.valueField(p,", 'skill: skill ? f.skillSel.value : ""', "values: skill ? f.valuesNow() : {}", "cj.values.help"):
            self.assertIn(needle, JS)
        self.assertIn("valueField,", (WEB / "pskills.js").read_text(encoding="utf-8"))
        for key in ("cj.kind", "cj.kind.task", "cj.kind.skill", "cj.skill", "cj.skillrow", "cj.values.none"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
