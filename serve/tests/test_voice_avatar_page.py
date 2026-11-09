"""The Avatar card and the camera setting: the markup that loads them, every request they make is one the proxy or the avatar relay allows, both
languages have every string, the page puts nothing the server says into HTML, and the styles are touch-sized and mobile first. The layout itself
was checked in a headless browser against a faked API at 360x640 and 1280x900. The page is static here."""
import re
import unittest
from pathlib import Path

from serve import voice_avatar, voice_proxy

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voiceavatar.js").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
CSS = (WEB / "voiceavatar.css").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
SAMPLE = {"${wiz.draft}": "0" * 32, "${step}": "foto", "${w.draft}": "0" * 32, "${name}": "Ada", "${row.name}": "Ada", "${encodeURIComponent(name)}": "Ada",
          "${encodeURIComponent(p.slug)}": "ada-p"}


def dictionary(lang):
    """The va.* strings of one language in i18n.js ("en" comes first, "it" after the line that opens it)."""
    text = I18N.split("\n  it: {")[0] if lang == "en" else I18N.split("\n  it: {")[1]
    return dict(re.findall(r'"(va\.[\w.]+)": "((?:[^"\\]|\\.)*)"', text))


class AvatarPage(unittest.TestCase):
    def test_the_styles_and_the_script_load_before_the_tab_that_mounts_them(self):
        self.assertIn('<link rel="stylesheet" href="web/voiceavatar.css">', HTML)
        self.assertLess(HTML.index("web/voiceavatar.js"), HTML.index("web/voicetab.js"))
        self.assertEqual(HTML.count("web/voiceavatar.js"), 1)
        self.assertIn("window.VoiceAvatar = { mount, update, stop, settingsCard", JS)
        self.assertIn("window.VoiceAvatar.settingsCard(api)", TAB)

    def test_every_request_to_the_service_is_on_the_proxys_list(self):
        calls = re.findall(r'svc\(\s*"(GET|POST|DELETE)"\s*,\s*([`"][^`"]+[`"])', JS)
        self.assertGreaterEqual(len(calls), 9)
        for method, raw in calls:
            path = raw[1:-1]
            for key, value in SAMPLE.items():
                path = path.replace(key, value)
            path = path.replace("${row.name}", "Ada")
            self.assertNotIn("${", path, raw)
            listed = (method, path) in voice_proxy.ROUTES or any(m == method and rx.fullmatch(path) for m, rx, _c in voice_proxy.PATTERNS)
            self.assertTrue(listed, (method, path))

    def test_the_bodies_the_page_sends_pass_the_proxys_checks(self):
        for path, body in (("/avatar/speak", {"text": "Ciao", "lang": "it"}), ("/avatar/start", {"persona": "Ada"}), ("/avatar/prepare", {"avatar": "Ada", "clip": "walk.mp4"}),
                           ("/avatar/prepare", {"avatar": "Ada"})):
            voice_proxy.ROUTES[("POST", path)](body)
        check = next(c for m, rx, c in voice_proxy.PATTERNS if m == "POST" and rx.fullmatch("/avatar/wizard/" + "0" * 32 + "/elabora"))
        self.assertEqual(check({"name": "Zoe Test", "language": "it"}), {"name": "Zoe Test", "language": "it"})
        self.assertIn('{ avatar: row.name, clip }', JS)
        self.assertIn("{ name: w.name.trim(), language: LANG }", JS)

    def test_the_files_and_the_stream_use_the_relay_routes(self):
        self.assertIn('"api/voice/avatar/preview.mjpg"', JS)
        self.assertIn("api/voice/avatar/wizard/${wiz.draft}/${step}", JS)
        self.assertIn("api/voice/avatar/${encodeURIComponent(name)}/poster", JS)
        self.assertIn('"api/voice/avatar"', JS)
        self.assertIn('const STEPS = ["name", "foto", "voce", "video", "make"];', JS)            # the photo first, the voice and the video optional
        self.assertNotIn('"pose"', JS)
        self.assertIn("api/voice/avatar/${encodeURIComponent(name)}/sprites.png", JS)
        self.assertTrue(voice_avatar.SPRITES.fullmatch("/api/voice/avatar/Ada/sprites.png"))
        self.assertTrue(voice_avatar.UPLOAD.fullmatch("/api/voice/avatar/wizard/" + "0" * 32 + "/video"))
        self.assertTrue(voice_avatar.POSTER.fullmatch("/api/voice/avatar/Ada/poster"))
        self.assertIn("voice_avatar.get(self, path)", HANDLER)
        self.assertIn("voice_avatar.post(self, path)", HANDLER)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        self.assertIn("authHeaders()", JS)                                              # with an API key set the picture and the files carry it

    def test_the_personality_is_changed_by_the_servers_own_route_and_nothing_else(self):
        self.assertIn("`api/personas/${encodeURIComponent(p.slug)}/update`", JS)
        self.assertIn("{ avatar: row.name }", JS)
        self.assertIn("body.voice = { engine: row.voice.engine, voice: row.voice.voice }", JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, JS)

    def test_both_languages_have_every_string_with_the_same_placeholders(self):
        en, it = dictionary("en"), dictionary("it")
        self.assertEqual(sorted(en), sorted(it))
        for key in en:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)
        used = {k for k in re.findall(r'\bT\(\s*"(va\.[\w.]+)"', JS) if not k.endswith(".")}      # "va.phase." + x is built from a name
        self.assertFalse(used - set(en), used - set(en))
        for step in ("name", "foto", "voce", "video", "make"):
            self.assertIn(f"va.s.{step}", en)
        for mode in ("visemes", "wav2lip", "video"):
            self.assertIn(f"va.mode.{mode}", en)
            self.assertIn(f"va.mode.{mode}.help", it)
        self.assertNotIn("va.s.pose", en)
        for phase in ("extract", "landmark", "latents", "masks"):
            self.assertIn(f"va.phase.{phase}", en)
        self.assertNotIn("Object.assign(STRINGS", JS)                                    # the strings are in i18n.js, where the other pages keep theirs

    def test_the_result_says_which_modes_the_avatar_has_and_the_pc_video_is_prepared_only_when_asked(self):
        self.assertIn("function modesView(res)", JS)
        self.assertIn('["visemes", "wav2lip", "video"]', JS)
        self.assertIn("res.render_modes", JS)
        self.assertEqual(JS.count('"/avatar/prepare"'), 2)                              # the two prepare buttons; finishing the wizard prepares nothing
        self.assertIn(".va-sheet", CSS)

    def test_no_emoji(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("@media (min-width: 700px)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotIn(":hover", CSS)
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")
        self.assertIn("max-width: 480px", CSS)

    def test_the_preview_stops_when_the_tab_or_the_page_is_left(self):
        self.assertIn('e.detail !== "voice"', JS)
        self.assertIn("visibilitychange", JS)
        self.assertIn("removeAttribute(\"src\")", JS)

    def test_speaking_is_offered_only_when_an_avatar_is_loaded_and_the_camera_says_why_it_is_not_running(self):
        self.assertIn("go.disabled = !ready", JS)
        self.assertIn("const ready = !!(S && S.running)", JS)
        for key in ("va.cam.unsupported", "va.cam.waiting", "va.cam.running", "va.cam.off"):
            self.assertIn(f'T("{key}"', JS)

    def test_the_camera_is_a_setting_that_starts_off(self):
        self.assertIn('input.id = "va-camera"; input.checked = !!(cam && cam.virtual_camera)', JS)
        self.assertIn("{ virtual_camera: input.checked }", JS)


if __name__ == "__main__":
    unittest.main()
