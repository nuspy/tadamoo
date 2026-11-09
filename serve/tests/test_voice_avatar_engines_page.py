"""The Avatar engines card: the markup that loads it, the two requests it makes are routes the server answers, both languages have every string, the page
puts nothing the server says into HTML, and the styles are touch-sized and mobile first. The layout itself was checked in a headless browser against a
faked API at 360x640 and 1280x900. The page is static here."""
import re
import unittest
from pathlib import Path

from serve import voice_avatar

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voiceengines.js").read_text(encoding="utf-8")
CSS = (WEB / "voiceengines.css").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
AVATAR_JS = (WEB / "voiceavatar.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


def dictionary(lang):
    text = I18N.split("\n  it: {")[0] if lang == "en" else I18N.split("\n  it: {")[1]
    return dict(re.findall(r'"(ve\.[\w.]+)": "((?:[^"\\]|\\.)*)"', text))


class EnginesPage(unittest.TestCase):
    def test_the_styles_and_the_script_load_before_the_tab_that_mounts_them(self):
        self.assertIn('<link rel="stylesheet" href="web/voiceengines.css">', HTML)
        self.assertEqual(HTML.count("web/voiceengines.js"), 1)
        self.assertLess(HTML.index("web/voiceengines.js"), HTML.index("web/voicetab.js"))
        self.assertIn("window.VoiceEngines = { settingsCard, refresh, webLine }", JS)
        self.assertIn("window.VoiceEngines.settingsCard(api)", TAB)
        self.assertLess(TAB.index("window.VoiceAvatar.settingsCard(api)"), TAB.index("window.VoiceEngines.settingsCard(api)"))

    def test_the_two_requests_are_routes_the_server_answers(self):
        calls = re.findall(r'api\(\s*"(GET|POST)"\s*,\s*"([^"]+)"', JS)
        self.assertEqual(sorted(calls), [("GET", "api/voice/avatar/engines"), ("POST", "api/voice/avatar/engines/select")])
        self.assertEqual("/" + calls[0][1], voice_avatar.ENGINES)
        self.assertEqual("/" + calls[1][1], voice_avatar.SELECT)
        self.assertIn("{ target, engine }", JS)
        self.assertIn("body.resolution = resolution", JS)
        self.assertNotRegex(JS, r'fetch\(')

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, JS)

    def test_both_languages_have_every_string_with_the_same_placeholders(self):
        en, it = dictionary("en"), dictionary("it")
        self.assertEqual(sorted(en), sorted(it))
        self.assertGreater(len(en), 30)
        for key in en:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)
        used = {k for k in re.findall(r'\bT\(\s*"(ve\.[\w.]+)"', JS + AVATAR_JS) if not k.endswith(".")}      # "ve.t." + target is built from a name
        self.assertFalse(used - set(en), used - set(en))
        for target in ("pc", "web", "android", "esp32"):
            self.assertIn(f"ve.t.{target}", en)
            self.assertIn(f"ve.t.{target}.help", it)
        for key in ("ve.kind.generic", "ve.kind.personalized", "ve.lic.nc", "ve.lic.verify", "ve.weight.nm", "ve.res.help"):
            self.assertIn(key, en)
        self.assertIn("lower is lighter", en["ve.res.help"])

    def test_each_row_shows_what_the_person_needs_to_choose(self):
        for part in ("i.name", "weightText(i)", "words(i.performance)", "words(lic.note)", "lic.noncommercial", "lic.verify", "st.available",
                     'input.type = "radio"', "i.needs", "ve-res-"):
            self.assertIn(part, JS)
        self.assertIn("st.reason_it", JS)                                       # the reason is shown in the language of the page

    def test_the_avatar_card_names_the_engine_the_web_preview_uses(self):
        self.assertIn("window.VoiceEngines.webLine(ctx.api)", AVATAR_JS)
        self.assertIn('"tadamoo:avatar-engines"', AVATAR_JS)
        self.assertIn('"tadamoo:avatar-engines"', JS)

    def test_no_emoji(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertGreaterEqual(CSS.count("min-height: 44px"), 2)
        self.assertIn("@media (min-width: 1000px)", CSS)
        self.assertNotIn("@media (max-width", CSS)                              # one column is the base; wider screens add to it
        self.assertNotIn(":hover", CSS)
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")
        self.assertIn("width: 24px", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)


if __name__ == "__main__":
    unittest.main()
