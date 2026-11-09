"""The Set up panel of the PC engines: the markup that loads it, the requests it makes are routes the proxy or the server answers, both languages have every string with
the same placeholders, the page puts nothing the server says into HTML, nothing is asked until the panel is opened, and the styles are touch-sized. The JSON proxy
accepts the setup routes of the three engines only. The layout is static here."""
import re
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_proxy

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicesetup.js").read_text(encoding="utf-8")
CSS = (WEB / "voicesetup.css").read_text(encoding="utf-8")
ENGINES_JS = (WEB / "voiceengines.js").read_text(encoding="utf-8")
TRAIN_JS = (WEB / "voicetrain.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


def dictionary(lang):
    text = I18N.split("\n  it: {")[0] if lang == "en" else I18N.split("\n  it: {")[1]
    return dict(re.findall(r'"((?:vse|vtr\.phase|vtr\.engine\.er_nerf)[\w.]*)": "((?:[^"\\]|\\.)*)"', text))


class SetupPage(unittest.TestCase):
    def test_the_script_and_styles_load_before_the_card_that_mounts_them(self):
        self.assertIn('<link rel="stylesheet" href="web/voicesetup.css">', HTML)
        self.assertEqual(HTML.count("web/voicesetup.js"), 1)
        self.assertLess(HTML.index("web/voicesetup.js"), HTML.index("web/voiceengines.js"))
        self.assertIn("window.VoiceEngineSetup = { panel }", JS)
        self.assertIn("window.VoiceEngineSetup.panel(i.id, api)", ENGINES_JS)

    def test_the_panel_is_for_the_three_pc_engines_and_asks_nothing_until_opened(self):
        self.assertIn('const ENGINES = ["wav2lip_v288", "sadtalker", "er_nerf"];', JS)
        self.assertIn('box.addEventListener("toggle"', JS)
        self.assertIn("if (box.open && !opened)", JS)
        head = JS.split("function panel(")[1].split('box.addEventListener("toggle"')[0]
        self.assertNotRegex(JS.split("function panel(")[1].split("let setup")[0], r"api\(|svc\(")     # nothing runs when the node is made
        self.assertIn("async function load()", head)

    def test_the_requests_are_routes_the_proxy_and_the_server_answer(self):
        service = set(re.findall(r'svc\(api, "(GET|POST)", [`"]([^`"]+)[`"]', JS))
        self.assertEqual(service, {("GET", "/avatar/engines/${engine}/setup"), ("POST", "/avatar/engines/python"), ("POST", "/avatar/engines/${engine}/check")})
        direct = set(re.findall(r'api\("(GET|POST)", "([^"]+)"', JS))
        self.assertEqual(direct, {("GET", "api/voice/models"), ("GET", "api/voice/download/plan?id="), ("POST", "api/voice/download"), ("GET", "api/voice/download/status"),
                          ("POST", "api/voice/service/call")})            # the service routes above go through the proxy's call
        self.assertNotRegex(JS, r"fetch\(")

    def test_nothing_the_server_says_enters_the_page_as_html(self):
        self.assertNotRegex(JS, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write")
        self.assertIn("textContent", JS)

    def test_both_languages_have_every_string_with_the_same_placeholders(self):
        en, it = dictionary("en"), dictionary("it")
        used = {k for k in re.findall(r'T\("((?:vse)\.[\w.]+)"', JS) if not k.endswith(".")} | {f"vse.python.src.{k}" for k in ("env", "stored", "none")}
        self.assertTrue(used)
        for key in used:
            self.assertIn(key, en, key)
            self.assertIn(key, it, key)
        for key in en:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it.get(key, ""))), key)
        for phase in ("check", "prepare", "head", "torso", "done"):
            self.assertIn(f"vtr.phase.{phase}", en)
            self.assertIn(f"vtr.phase.{phase}", it)
        self.assertIn("vtr.engine.er_nerf.help", en)
        self.assertIn("fragile", en["vtr.engine.er_nerf.help"])

    def test_er_nerf_is_trained_from_the_same_section_without_epochs(self):
        self.assertIn('const ENGINES = ["ultralight", "feathertalk", "er_nerf"];', TRAIN_JS)
        self.assertIn('engine === "er_nerf" ? { avatar, engine } : { avatar, engine, epochs, fresh }', TRAIN_JS)

    def test_the_styles_are_touch_sized_and_do_not_scroll_sideways(self):
        self.assertRegex(CSS, r"\.vse-sum \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.vse \.btn \{[^}]*min-height: 44px")
        self.assertIn("min-width: 0", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn(":focus-visible", CSS)


class SetupProxy(unittest.TestCase):
    def setUp(self):
        self.calls = []
        rec = lambda method, route, body=None, timeout=None: (self.calls.append((method, route, body)) or (200, {"ok": True}))
        patcher = mock.patch.object(voice_proxy.voice_service, "call", rec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_setup_check_and_python_routes_of_the_three_engines_are_allowed(self):
        for engine in ("wav2lip_v288", "sadtalker", "er_nerf"):
            self.assertEqual(voice_proxy.call({"method": "GET", "path": f"/avatar/engines/{engine}/setup"})[0], 200)
            self.assertEqual(voice_proxy.call({"method": "POST", "path": f"/avatar/engines/{engine}/check"})[0], 200)
        self.assertEqual(voice_proxy.call({"method": "POST", "path": "/avatar/engines/python", "body": {"path": "C:\\env\\python.exe"}})[0], 200)
        self.assertEqual(self.calls[-1], ("POST", "/avatar/engines/python", {"path": "C:\\env\\python.exe"}))

    def test_anything_near_them_is_refused_and_nothing_reaches_the_service(self):
        for method, path, body in (("GET", "/avatar/engines/visemes/setup", None), ("POST", "/avatar/engines/musetalk_15/check", None), ("POST", "/avatar/engines/er_nerf/check", {"x": 1}),
                                   ("DELETE", "/avatar/engines/er_nerf/setup", None), ("GET", "/avatar/engines/er_nerf/setup/../x", None),
                                   ("POST", "/avatar/engines/python", {}), ("POST", "/avatar/engines/python", {"path": 3}), ("POST", "/avatar/engines/python", {"path": "x", "y": 1}),
                                   ("POST", "/avatar/engines/python", {"path": "a" * 600})):
            with self.assertRaises(voice_proxy.Refused, msg=(method, path, body)):
                voice_proxy.call({"method": method, "path": path, **({"body": body} if body is not None else {})})
        self.assertEqual(self.calls, [])

    def test_er_nerf_can_be_planned_for_training_through_the_proxy(self):
        self.assertEqual(voice_proxy.call({"method": "POST", "path": "/avatar/train/plan", "body": {"avatar": "ada", "engine": "er_nerf"}})[0], 200)
        with self.assertRaises(voice_proxy.Refused):
            voice_proxy.call({"method": "POST", "path": "/avatar/train/plan", "body": {"avatar": "ada", "engine": "sadtalker"}})


if __name__ == "__main__":
    unittest.main()
