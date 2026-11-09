"""The Voice tab and the voice card of Settings > Voice: the markup, the scripts and styles, the requests the page makes (all on the proxy's list), the
translations and the touch rules. The page is static here; the layout is checked in a browser."""
import re
import unittest
from pathlib import Path

from serve import voice_proxy

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicetab.js").read_text(encoding="utf-8")
CSS = (WEB / "voicetab.css").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")

# keys the script builds from a state or a name
DYNAMIC = {"vt.st.": ("loaded", "unloaded", "loading", "error", "unknown"), "vt.fn.": ("stt", "tts", "avatar"), "vt.phase.": ("idle", "listening", "reasoning", "speaking", "hidden")}


class VoiceTabPageTest(unittest.TestCase):
    def test_the_tab_the_panel_and_the_settings_card_are_in_the_page(self):
        self.assertEqual(HTML.count('id="tab-voice"'), 1)
        self.assertRegex(HTML, r'<button class="tab" role="tab" id="tab-voice" data-view="voice" aria-controls="view-voice" aria-selected="false" tabindex="-1" data-t="tab.voice">Voice</button>')
        self.assertRegex(HTML, r'<div class="view vt" id="view-voice" role="tabpanel" aria-labelledby="tab-voice" hidden>')
        self.assertIn('id="vt-root"', HTML)
        # the Settings and Models sub-tabs keep their own ids: the new ones are view-voice and tab-voice
        self.assertEqual(HTML.count('id="view-voice"'), 1)
        self.assertIn('id="vb-section"', HTML)
        self.assertRegex(HTML.split('id="vb-section"')[0].rsplit("<section", 1)[1], r'data-sub="voice"')
        self.assertIn('<link rel="stylesheet" href="web/voicetab.css">', HTML)
        self.assertIn('<script defer src="web/voicetab.js"></script>', HTML)

    def test_ids_are_unique(self):
        for i in ("tab-voice", "view-voice", "vt-root", "vt-h", "vb-section", "vb-root", "vb-h"):
            self.assertEqual(HTML.count(f'id="{i}"'), 1, i)

    def test_the_view_is_known_to_the_shell(self):
        self.assertRegex(APP, r'const VIEWS = \[[^\]]*"voice"')
        self.assertNotIn("voice:", APP.split("const TAB_OF")[1].split("\n")[0])              # it is a tab of its own, not a page under another

    def test_every_request_the_page_makes_exists(self):
        for route in ("api/voice/brain", "api/voice/service/call", "api/personas"):
            self.assertIn(f'"{route}"', JS, route)
        for route in ("/api/voice/brain", "/api/voice/service/call"):
            self.assertIn(f'"{route}"', HANDLER, route)
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_every_call_to_the_service_is_on_the_proxys_list(self):
        calls = re.findall(r'svc\(\s*"(GET|POST)"\s*,\s*"([^"]+)"', JS)
        self.assertGreaterEqual(len(calls), 5)
        for method, path in calls:
            self.assertIn((method, path), voice_proxy.ROUTES)
        self.assertIn('{ mode: "half" }', JS)                                           # the only body of a session the page sends
        self.assertNotIn("avatar", re.findall(r'svc\("POST", "/session/start"[^)]*\)', JS)[0])

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, JS)

    def test_the_voice_level_is_the_servers_setting_with_the_chats_words(self):
        self.assertNotIn('"tadamoo.chat.perm"', JS)                                   # the browser holds no level of its own
        self.assertIn('api("POST", "api/permission-level", { channel: "voice", level: m })', JS)
        self.assertIn('api("GET", "api/permission-level")', JS)
        self.assertNotIn("permission:", JS)                                           # never sent with the brain's settings
        self.assertIn('T("lvl.where.voice"', JS)
        self.assertIn('T("lvl.risk")', JS)
        self.assertRegex(JS, r'PERMS = \["plan", "read_only", "default", "auto"\]')
        for mode in ("plan", "read_only", "default", "auto"):
            self.assertEqual(I18N.count(f'"ag.mode.{mode}":'), 2, mode)                 # the Chat's words, not new ones
            self.assertEqual(I18N.count(f'"ag.mode.{mode}.d":'), 2, mode)
        self.assertEqual(JS.count('permission("v'), 2)                                 # drawn in the tab and in Settings > Voice
        self.assertIn('T("ag.auto.risk")', JS)

    def test_the_talk_buttons_work_only_with_the_functions_loaded_and_say_why(self):
        self.assertIn('hold.disabled = !!reason', JS)
        self.assertIn('go.disabled = !!reason', JS)
        self.assertIn('"vt.off.service"', JS)
        self.assertIn('"vt.off.fns"', JS)
        self.assertIn('["stt", "tts"].filter', JS)
        self.assertIn('state === "loaded"', JS)

    def test_hold_to_talk_works_with_touch_pointer_and_keyboard_and_lets_go_when_the_page_leaves(self):
        for token in ("onpointerdown", "onpointerup", "onpointercancel", "onkeydown", "onkeyup", "setPointerCapture", "aria-pressed", "oncontextmenu",
                      'window.addEventListener("blur"', 'document.hidden) release()'):
            self.assertIn(token, JS, token)
        self.assertIn("touch-action: none", CSS)

    def test_the_hold_button_is_not_replaced_under_a_finger_or_while_typing(self):
        self.assertIn("!typing && !holding", JS)

    def test_the_text_keys_exist_twice_in_the_translations(self):
        keys = set(re.findall(r'\bT\(\s*"([\w.]+)"', JS)) | set(re.findall(r'data-t="((?:vt|tab)\.[\w.]+)"', HTML))
        keys |= set(re.findall(r'"(vt\.[\w.]+)"', JS))                                   # also the ones named in a list
        keys = {k for k in keys if (k.startswith("vt.") or k == "tab.voice" or k.startswith("ag.")) and not k.endswith(".")}
        for prefix, names in DYNAMIC.items():
            keys |= {prefix + n for n in names}
        keys |= {f"vt.mode.{m}" for m in ("direct", "harness")} | {f"vt.mode.{m}.d" for m in ("direct", "harness")}
        self.assertGreater(len(keys), 45)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        defined = set(re.findall(r'"(vt\.[\w.]+)":', I18N))
        self.assertEqual(sorted(k for k in defined if k not in keys), [])               # nothing is translated that no page uses

    def test_the_two_languages_have_the_same_placeholders(self):
        en, it = I18N.split("\n  it: {")[0], I18N.split("\n  it: {")[1]
        for key in sorted(set(re.findall(r'"(vt\.[\w.]+)":', I18N))):
            e = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', en)
            i = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', it)
            self.assertTrue(e and i, key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", e.group(1))), sorted(re.findall(r"\{\d\}", i.group(1))), key)

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertIn(".vt-root .btn, .vb-root .btn { min-height: 44px; min-width: 44px;", CSS)
        self.assertIn(".vt-opt { display: flex; align-items: flex-start; gap: 12px; min-height: 44px;", CSS)
        self.assertIn(".vt-field input[type=\"text\"], .vt-field select { width: 100%; min-height: 44px;", CSS)
        self.assertIn(".vt-check { display: flex; align-items: center; gap: 12px; min-height: 44px;", CSS)
        self.assertIn("min-height: 72px", CSS)
        self.assertIn("@media (min-width: 1000px)", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotIn(":hover", CSS)                                                 # no hover-only anything
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")
        self.assertIn(":focus-visible", CSS)

    def test_no_emoji_icons(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))
        for key in re.findall(r'"(vt\.[\w.]+)": "([^"]*)"', I18N):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", key[1]), key[0])

    def test_the_avatar_card_is_drawn_by_its_own_script(self):
        self.assertIn("window.VoiceAvatar.mount(parts.avatar.s, avatarCtx)", JS)
        self.assertIn("window.VoiceAvatar.update()", JS)
        self.assertNotIn("vt.avatar.off", JS)


if __name__ == "__main__":
    unittest.main()
