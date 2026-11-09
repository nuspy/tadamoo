"""Settings > Voice > Import from callAPIcall: the page asks for the plan only on the button, confirms what is personal, and runs only with the confirmation."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "voice.js").read_text(encoding="utf-8")
CSS = (WEB / "voice.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")
IMPORT_JS = JS[JS.index("/* ---- import from callAPIcall"):JS.index("/* ---- download: plan, license, confirm ---- */")]


class VoiceImportPageTest(unittest.TestCase):
    def test_the_two_routes_are_used_and_the_server_has_them(self):
        self.assertIn('"api/voice/import/plan"', IMPORT_JS)
        self.assertIn('"api/voice/import/run"', IMPORT_JS)
        self.assertIn('{"/api/voice/import/plan": voice_import.plan, "/api/voice/import/run": voice_import.run}', HANDLER)
        self.assertIn("host.append(importBox());", JS)
        self.assertNotRegex(IMPORT_JS, r'fetch\(\s*["`]/')

    def test_nothing_is_read_until_the_person_presses_the_button(self):
        self.assertEqual(IMPORT_JS.count("imLook"), 2)                                    # its definition and the one button
        self.assertIn('btn(IM.busy === "look" ? T("vo.im.looking") : T("vo.im.look"), "primary", imLook,', IMPORT_JS)
        self.assertIn("const SUGGESTED", IMPORT_JS)
        self.assertIn("input.value = IM.source", IMPORT_JS)                              # the suggestion is only a value the person can change

    def test_the_run_sends_the_confirmation_and_each_personal_item_has_its_own(self):
        self.assertIn("{ source: IM.plan.source, confirm: true, items }", IMPORT_JS)
        self.assertIn("confirm_personal: !!(it.personal && imPick(it.id).personal)", IMPORT_JS)
        self.assertIn("go.disabled = !imReady() || !!IM.busy", IMPORT_JS)
        self.assertIn("IM.plan.items.every(it => !imChosen(it) || !it.personal || imPick(it.id).personal)", IMPORT_JS)
        self.assertEqual(IMPORT_JS.count('"api/voice/import/run"'), 1)

    def test_a_persona_that_exists_is_replaced_only_when_the_person_says_so(self):
        self.assertIn("it.conflict ? p.replace", IMPORT_JS)                              # an existing one is chosen only by its replace box
        self.assertIn("replace: !!it.conflict", IMPORT_JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, IMPORT_JS)

    def test_every_key_exists_twice_and_the_two_languages_have_the_same_ones(self):
        keys = set(re.findall(r'\bT\(\s*"(vo\.im\.[\w.]+)"', IMPORT_JS))
        self.assertGreater(len(keys), 25)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        english, italian = I18N.split('"vo.im.title"')[1].split('"vo.dl.')[0], I18N.rsplit('"vo.im.title"', 1)[1].split('"vo.dl.')[0]
        self.assertEqual(set(re.findall(r'"(vo\.im\.[\w.]+)":', '"vo.im.title"' + english)), set(re.findall(r'"(vo\.im\.[\w.]+)":', '"vo.im.title"' + italian)))
        for dynamic in ("vo.im.st.created", "vo.im.st.updated", "vo.im.st.skipped", "vo.im.st.needs", "vo.im.st.failed", "vo.im.why.memory", "vo.im.why.video", "vo.im.why.voice"):
            self.assertEqual(I18N.count(f'"{dynamic}":'), 2, dynamic)

    def test_the_styles_are_mobile_first_and_nothing_needs_hover(self):
        self.assertIn(".vo-im-items { display: grid;", CSS)
        self.assertIn(".vo-im-item", CSS)
        self.assertNotIn(":hover", CSS)
        self.assertNotRegex(IMPORT_JS, r"[\U0001F300-\U0001FAFF]")


if __name__ == "__main__":
    unittest.main()
