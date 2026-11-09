"""Settings > Voice > Meetings and the Meetings card of the Voice tab: the rules in voice.json (validated, careful defaults, the same limits as the
service), the routes that carry them, the proxy's list for the meeting routes, and the page (every request is on the list, every string is in both
languages, nothing the server says goes into HTML, targets are 44px). The layout was checked in a headless browser against a faked API at 360x640
and 1280x900; the page is static here. No server is started."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_proxy, voice_settings as vs

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parents[2]
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicemeet.js").read_text(encoding="utf-8")
CSS = (WEB / "voicemeet.css").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")

CAREFUL = {"speak": False, "camera": False, "proactive": False, "consent_mode": "window", "consent_window_s": 45, "on_behalf_of": "", "display_name": "TaDaMoo AI assistant",
           "virtual_mic": "", "wake_phrase": "", "autojoin": False, "allow_programs_join": False, "copilot_replies": 3, "copilot_audio_reply": False, "copilot_hide_from_capture": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.root / "data"), "TADAMOO_CACHE": str(self.root / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def stored(self):
        return json.loads((self.root / "data" / "voice.json").read_text(encoding="utf-8"))


class SettingsTest(Base):
    def test_the_defaults_are_the_careful_ones(self):
        self.assertEqual(vs.meetings(), CAREFUL)

    def test_a_change_is_checked_and_only_the_named_fields_are_stored(self):
        out = vs.set_meetings({"consent_window_s": 90, "on_behalf_of": "  Mario Rossi "})
        self.assertEqual((out["consent_window_s"], out["on_behalf_of"]), (90, "Mario Rossi"))
        vs.set_meetings({"speak": True})
        self.assertEqual(self.stored()["meetings"], {"consent_window_s": 90, "on_behalf_of": "Mario Rossi", "speak": True})
        self.assertEqual(vs.meetings()["speak"], True)

    def test_every_bad_value_is_refused_and_nothing_is_written(self):
        bad = [{"speak": "yes"}, {"camera": 1}, {"proactive": 1}, {"autojoin": None}, {"allow_programs_join": "no"}, {"copilot_audio_reply": "true"}, {"copilot_hide_from_capture": 0},
               {"consent_mode": "silent"}, {"consent_window_s": 19}, {"consent_window_s": 301}, {"consent_window_s": 45.5}, {"consent_window_s": "45"},
               {"consent_window_s": True}, {"copilot_replies": 0}, {"copilot_replies": 7}, {"on_behalf_of": "x" * 81}, {"on_behalf_of": "a\nb"},
               {"display_name": "   "}, {"display_name": "x" * 41}, {"virtual_mic": 5}, {"wake_phrase": "x" * 41}, {"nothing": 1}, [], "x", None]
        for req in bad:
            with self.assertRaises(vs.VoiceSettingsError, msg=str(req)):
                vs.set_meetings(req)
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_damaged_file_reads_as_the_defaults(self):
        (self.root / "data").mkdir(parents=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"meetings": {"speak": "yes", "consent_window_s": 5, "consent_mode": "x", "copilot_replies": 99}}), encoding="utf-8")
        self.assertEqual(vs.meetings(), CAREFUL)
        (self.root / "data" / "voice.json").write_text(json.dumps({"meetings": 5}), encoding="utf-8")
        self.assertEqual(vs.meetings(), CAREFUL)

    def test_the_limits_are_the_services_own(self):
        """voice/callbridge/meetbot/consent.py holds the window limits and the modes; this module copies them, so a test keeps them equal."""
        spec = importlib.util.spec_from_file_location("consent_for_test", ROOT / "voice" / "callbridge" / "meetbot" / "consent.py")
        consent = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = consent                      # a dataclass looks its module up by name
        spec.loader.exec_module(consent)
        self.assertEqual(vs.CONSENT_WINDOW, (consent.WINDOW_MIN_S, consent.WINDOW_MAX_S, consent.WINDOW_DEFAULT_S))
        self.assertEqual(vs.CONSENT_MODES, consent.MODES)

    def test_the_service_reads_what_the_server_writes(self):
        import voice  # noqa: F401  (puts voice/ on sys.path)
        from voice import settings_bridge
        vs.set_meetings({"speak": True, "consent_mode": "explicit", "consent_window_s": 120, "on_behalf_of": "Mario Rossi", "virtual_mic": "CABLE", "copilot_replies": 4})
        got = settings_bridge.load(self.root / "data" / "voice.json").meetings
        self.assertEqual((got.speak, got.consent_mode, got.consent_window_s, got.on_behalf_of, got.virtual_mic, got.copilot_replies),
                         (True, "explicit", 120, "Mario Rossi", "CABLE", 4))
        self.assertEqual(settings_bridge.load(self.root / "data" / "voice.json").meetings_audit_file, str(self.root / "data" / "meetings-audit.jsonl"))


class RoutesTest(unittest.TestCase):
    def test_the_settings_routes_are_in_the_handler_and_loopback_only(self):
        self.assertEqual(HANDLER.count('"/api/voice/meetings"'), 3)                  # the GET list, the GET answer and the POST table, behind the loopback check
        self.assertIn('"/api/voice/meetings": voice_settings.set_meetings', HANDLER)
        self.assertIn("voice_settings.meetings()", HANDLER)

    def test_the_proxy_lists_the_meeting_routes_and_nothing_wider(self):
        for key in (("GET", "/meetbot/status"), ("POST", "/meetbot/join"), ("POST", "/meetbot/leave"), ("GET", "/meetings/list"), ("GET", "/copilot/state"),
                    ("POST", "/copilot/toggle"), ("GET", "/calendar/status"), ("GET", "/calendar/events")):
            self.assertIn(key, voice_proxy.ROUTES, key)
        mid = "20261109-100000-meet-ab12"
        for method, path in (("GET", f"/meetings/{mid}"), ("GET", f"/meetings/{mid}/transcript"), ("DELETE", f"/meetings/{mid}"), ("POST", "/calendar/google/connect"),
                             ("POST", "/calendar/microsoft/disconnect"), ("POST", "/calendar/events/g:abc_1-2/autojoin")):
            self.assertTrue(any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS), (method, path))
        for method, path in (("GET", "/meetings/../x"), ("DELETE", "/meetings/a/b"), ("POST", "/calendar/yahoo/connect"), ("POST", "/meetbot/speak"),
                             ("POST", "/calendar/events/x/autojoin/extra"), ("GET", "/meetings/")):
            self.assertFalse(any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS) or (method, path) in voice_proxy.ROUTES, (method, path))

    def test_a_join_from_the_page_must_confirm_the_right(self):
        check = voice_proxy.ROUTES[("POST", "/meetbot/join")]
        ok = {"url": "https://meet.google.com/abc-defg-hij", "rights_confirmed": True, "speak": False, "consent_mode": "explicit", "consent_window_s": 60, "title": "Weekly"}
        self.assertEqual(check(ok), ok)
        for bad in ({"url": "https://meet.google.com/x"}, {**ok, "rights_confirmed": False}, {**ok, "rights_confirmed": "yes"}, {**ok, "url": "ftp://x"},
                    {**ok, "speak": "no"}, {**ok, "consent_mode": "off"}, {**ok, "consent_window_s": 19}, {**ok, "consent_window_s": 301}, {**ok, "consent_window_s": "60"},
                    {**ok, "title": "x" * 121}, {**ok, "on_behalf_of": "Someone"}, {**ok, "rights": "calendar"}, {**ok, "host": "x"}, None, []):
            with self.assertRaises(voice_proxy.Refused, msg=str(bad)):
                check(bad)

    def test_the_autojoin_body_is_a_boolean(self):
        check = next(c for m, rx, c in voice_proxy.PATTERNS if rx.pattern.endswith("/autojoin"))
        self.assertEqual(check({"enabled": True}), {"enabled": True})
        for bad in ({"enabled": "yes"}, {}, {"enabled": True, "x": 1}, None):
            with self.assertRaises(voice_proxy.Refused):
                check(bad)

    def test_a_join_goes_to_the_service_with_its_own_timeout(self):
        seen = []
        with mock.patch.object(voice_proxy.voice_service, "call", side_effect=lambda m, p, b, timeout: seen.append((m, p, b, timeout)) or (202, {"session_id": "x"})):
            status, data = voice_proxy.call({"method": "POST", "path": "/meetbot/join", "body": {"url": "https://zoom.us/j/1", "rights_confirmed": True}})
        self.assertEqual((status, seen[0][:2], seen[0][3]), (202, ("POST", "/meetbot/join"), 60.0))


class PageTest(unittest.TestCase):
    def test_the_script_and_the_styles_load_before_the_tab_that_mounts_them(self):
        self.assertIn('<link rel="stylesheet" href="web/voicemeet.css">', HTML)
        self.assertEqual(HTML.count("web/voicemeet.js"), 1)
        self.assertLess(HTML.index("web/voicemeet.js"), HTML.index("web/voicetab.js"))
        self.assertIn("window.VoiceMeet = { mount, update, settingsCard }", JS)
        for call in ("window.VoiceMeet.mount(parts.meetings.s", "window.VoiceMeet.update()", "window.VoiceMeet.settingsCard(api)", '["meetings", "vt.meetings"]'):
            self.assertIn(call, TAB)

    def test_every_request_is_on_the_proxys_list(self):
        sample = {"${id}": "20261109-100000-meet-ab12", "${name}": "google", "${what}": "connect", "${e.id}": "g:abc", "${m.id}": "x"}
        calls = re.findall(r'svc\(\s*"(GET|POST|DELETE)"\s*,\s*([`"][^`"]+[`"])', JS)
        self.assertGreaterEqual(len(calls), 10)
        for method, raw in calls:
            path = raw[1:-1]
            for key, value in sample.items():
                path = path.replace(key, value)
            self.assertNotIn("${", path, raw)
            listed = (method, path) in voice_proxy.ROUTES or any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS)
            self.assertTrue(listed, (method, path))
        self.assertIn('"api/voice/meetings"', JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_the_bodies_the_page_sends_pass_the_checks(self):
        voice_proxy.ROUTES[("POST", "/meetbot/join")]({"url": "https://meet.google.com/abc", "rights_confirmed": True, "speak": False, "consent_mode": "window", "consent_window_s": 45})
        self.assertIn("rights_confirmed: ui.rights.checked === true", JS)
        self.assertIn("ui.join.disabled = !idle || !up || !D.on_behalf_of || !ui.url.value.trim() || !ui.rights.checked", JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_every_string_exists_twice_with_the_same_placeholders(self):
        used = set(re.findall(r'T\("(vm\.[a-z_.0-9]+)"', JS))
        keys = {k for k in used if not k.endswith(".")}
        keys |= {"vm.an." + s for s in ("none", "pending", "analyzing", "done", "error")}
        keys |= {"vm.state." + s for s in ("idle", "joining", "waiting_admit", "in_call", "leaving", "ended", "error")}
        keys |= {"vm.consent." + s for s in ("idle", "waiting", "granted", "ended")}
        keys |= {"vm.ended." + s for s in ("objection", "removed", "left", "no_consent")}
        keys |= {"vm.mode.window", "vm.mode.explicit", "vm.cal.google", "vm.cal.microsoft", "vt.meetings", "vm.past", "vm.cop", "vm.cal"}
        for key in re.findall(r'\["[a-z_]+", \d+, "(vm\.set\.[a-z.]+)", "(vm\.set\.[a-z.]+)"\]', JS):
            keys |= set(key)
        keys |= {"vm.set." + n + h for n in ("speak", "camera", "proactive", "autojoin", "audio", "hide") for h in ("", ".help")}
        self.assertGreater(len(keys), 70)
        for key in sorted(keys):
            found = re.findall(r'^\s*"' + re.escape(key) + r'": "((?:[^"\\]|\\.)*)"', I18N, re.M)
            self.assertEqual(len(found), 2, key)
            self.assertEqual(sorted(re.findall(r"\{\d+\}", found[0])), sorted(re.findall(r"\{\d+\}", found[1])), key)

    def test_the_consent_texts_say_what_the_rule_says(self):
        en = dict(re.findall(r'"(vm\.[a-z_.0-9]+)": "((?:[^"\\]|\\.)*)"', I18N.split("\n  it: {")[0]))
        self.assertIn("AI assistant", en["vm.intro"])
        self.assertIn("deletes what it heard", en["vm.intro"])
        self.assertIn("right to bring an AI assistant", en["vm.rights"])
        self.assertIn("twice the window", en["vm.mode.help"])

    def test_no_emoji_in_the_new_texts(self):
        for text in re.findall(r'"vm\.[a-z_.0-9]+": "([^"]*)"', I18N):
            self.assertFalse(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text), text)

    def test_targets_are_touch_sized_and_the_layout_is_mobile_first(self):
        self.assertRegex(CSS, r"\.vm-field input\[type=\"url\"\][^{]*\{[^}]*min-height: 44px")
        self.assertIn("font: 16px/1.4 var(--font-sans)", CSS)                      # 16px keeps a phone from zooming into the field
        self.assertIn("flex-wrap: wrap", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertRegex(CSS, r"@media \(min-width: 1000px\)[^}]*#vt-card-meetings")
        self.assertNotRegex(CSS, r"(?<!-)\bwidth: \d{4,}px")
        self.assertIn("prefers-reduced-motion", CSS)

    def test_the_join_controls_and_the_poll_are_there(self):
        for control in ("vm-url", "vm-rights", "vm-speak", "vm-mode", "vm-window", "vm-join", "vm-leave", "vm-cop-toggle"):
            self.assertIn(control, JS)
        self.assertIn('T("vm.past.confirm")', JS)                                   # a delete is asked twice
        self.assertIn("rowMsg", JS)


if __name__ == "__main__":
    unittest.main()
