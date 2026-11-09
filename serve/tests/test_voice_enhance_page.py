"""The Avatar acceleration card: the markup that loads it, every request it makes is one the server allows, both languages have every string, what the
server says enters the page as text, an unusable choice is shown disabled with its reason and never left out, and the styles are touch-sized and
mobile first. The layout was checked in a headless browser against a faked API at 360x640, 390x844 and 1280x900; the page is static here."""
import re
import unittest
from pathlib import Path

from serve import voice_avatar, voice_proxy

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voiceenhance.js").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
CSS = (WEB / "voiceenhance.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


def dictionary(lang):
    text = I18N.split("\n  it: {")[0] if lang == "en" else I18N.split("\n  it: {")[1]
    return dict(re.findall(r'"(ae\.[\w.]+)": "((?:[^"\\]|\\.)*)"', text))


class EnhancePage(unittest.TestCase):
    def test_the_styles_and_the_script_load_before_the_tab_that_mounts_them(self):
        self.assertEqual(HTML.count('<link rel="stylesheet" href="web/voiceenhance.css">'), 1)
        self.assertEqual(HTML.count("web/voiceenhance.js"), 1)
        self.assertLess(HTML.index("web/voiceenhance.js"), HTML.index("web/voicetab.js"))
        self.assertIn("window.VoiceEnhance = { settingsCard };", JS)
        self.assertIn("window.VoiceEnhance.settingsCard(api)", TAB)

    def test_the_card_is_under_the_avatar_output_card_in_settings_voice(self):
        self.assertLess(TAB.index("window.VoiceAvatar.settingsCard(api)"), TAB.index("window.VoiceEnhance.settingsCard(api)"))

    def test_every_request_is_one_the_server_allows(self):
        calls = re.findall(r'api\(\s*"(GET|POST)"\s*,\s*"([^"]+)"', JS)
        self.assertEqual(sorted(set(calls)), [("GET", "api/voice/avatar"), ("POST", "api/voice/avatar"), ("POST", "api/voice/service/call")])
        self.assertEqual(voice_avatar.BASE, "/api/voice/avatar")
        self.assertIn('{ method: "GET", path: "/avatar/enhance" }', JS)
        self.assertIn(("GET", "/avatar/enhance"), voice_proxy.ROUTES)
        self.assertIn('{ enhance: body }', JS)
        self.assertNotRegex(JS, r'fetch\(')                                              # only the voice tab's own api function, which carries the key

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, JS)

    def test_an_unusable_choice_is_disabled_with_its_reason_and_never_left_out(self):
        self.assertIn("input.disabled = !usable(stage, name);", JS)
        self.assertIn('if (input.disabled) set.append(Object.assign(el("p", "vt-help ae-why", T("ae.na", why(stage, name)))', JS)
        self.assertIn("const UP = [\"none\", \"basic\", \"rtx_video\"], IN = [\"none\", \"basic\"]", JS)       # every provider is listed whatever the PC can do
        self.assertIn("p.reason", JS)

    def test_the_choices_are_the_ones_the_server_validates(self):
        from serve import voice_settings as vs
        self.assertEqual(tuple(re.findall(r'"(\w+)"', re.search(r"const UP = \[(.*?)\]", JS).group(1))), vs.ENHANCE_UPSCALE)
        self.assertEqual(tuple(re.findall(r'"(\w+)"', re.search(r"IN = \[(.*?)\]", JS).group(1))), vs.ENHANCE_INTERPOLATE)
        self.assertEqual(tuple(int(x) for x in re.search(r"FACTORS = \[(.*?)\]", JS).group(1).split(",")), vs.ENHANCE_FACTORS)
        self.assertEqual(tuple(float(x) for x in re.search(r"ALL_SCALES = \[(.*?)\]", JS).group(1).split(",")), vs.ENHANCE_SCALES)

    def test_both_languages_have_every_string_with_the_same_placeholders(self):
        en, it = dictionary("en"), dictionary("it")
        self.assertGreaterEqual(len(en), 30)
        self.assertEqual(sorted(en), sorted(it))
        for key in en:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)
        used = {k for k in re.findall(r'\bT\(\s*"(ae\.[\w.]+)"', JS)}
        used |= {f"ae.{kind}.{name}" for kind in ("up", "in") for name in ("none", "basic", "rtx_video") if not (kind == "in" and name == "rtx_video")}
        self.assertFalse(used - set(en), used - set(en))
        self.assertIn("ae.cost.over", en)
        self.assertNotIn("Object.assign(STRINGS", JS)

    def test_the_basic_providers_are_called_not_neural_in_both_languages(self):
        en, it = dictionary("en"), dictionary("it")
        self.assertIn("not neural", en["ae.up.basic"] + en["ae.in.basic"])
        self.assertIn("non neurale", it["ae.up.basic"])

    def test_no_emoji(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))

    def test_the_styles_are_mobile_first_with_touch_sized_targets_and_no_hover(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertNotIn(":hover", CSS)
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn("focus-visible", CSS)

    def test_the_polling_stops_when_the_card_leaves_the_page_and_while_the_tab_is_hidden(self):
        self.assertIn("if (!box.isConnected) { clearInterval(timer); timer = null; return; }", JS)
        self.assertIn("if (document.hidden) return;", JS)

    def test_the_number_fields_are_typed_text_with_a_numeric_keypad_not_a_slider(self):
        self.assertIn('input.inputMode = "numeric"', JS)
        self.assertIn("worker_fps_cap", JS)
        self.assertIn("worker_height_cap", JS)


if __name__ == "__main__":
    unittest.main()
