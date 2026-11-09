"""The planner of the Meetings card and the two new settings, statically: the settings `meetings.autojoin` (the master switch, renamed from
calendar_autojoin with no alias) and `meetings.allow_programs_join` (validated, atomic, careful defaults, read by the voice service's bridge and by the
compat layer), the proxy's list for the plan routes (bodies checked, nothing wider), and the page (every request is on the list, every string is in both
languages with the same placeholders, nothing the server says goes into HTML, targets are 44px, mobile-first). The layout was checked in a headless
browser against a faked API at 360x640 and 1280x900; no server is started here."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_compat, voice_proxy, voice_settings as vs

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
PLAN = (WEB / "voiceplan.js").read_text(encoding="utf-8")
MEET = (WEB / "voicemeet.js").read_text(encoding="utf-8")
CSS = (WEB / "voiceplan.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


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
    def test_both_switches_start_off(self):
        got = vs.meetings()
        self.assertEqual((got["autojoin"], got["allow_programs_join"]), (False, False))
        self.assertNotIn("calendar_autojoin", got)

    def test_each_is_stored_alone_and_read_back(self):
        vs.set_meetings({"allow_programs_join": True})
        self.assertEqual(self.stored()["meetings"], {"allow_programs_join": True})
        self.assertEqual((vs.meetings()["autojoin"], vs.meetings()["allow_programs_join"]), (False, True))
        vs.set_meetings({"autojoin": True})
        self.assertEqual(self.stored()["meetings"], {"allow_programs_join": True, "autojoin": True})
        vs.set_meetings({"allow_programs_join": False})
        self.assertEqual((vs.meetings()["autojoin"], vs.meetings()["allow_programs_join"]), (True, False))

    def test_only_a_boolean_is_taken_and_nothing_is_written_on_a_refusal(self):
        for bad in ({"allow_programs_join": "true"}, {"allow_programs_join": 1}, {"allow_programs_join": None}, {"autojoin": "yes"}, {"autojoin": 0}):
            with self.assertRaises(vs.VoiceSettingsError, msg=str(bad)) as raised:
                vs.set_meetings(bad)
            body = raised.exception.body()
            self.assertTrue(body["message"] and body["message_it"] and body["code"] in bad.__str__())
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_the_old_name_is_an_unknown_field_and_a_stored_old_value_means_nothing(self):
        with self.assertRaises(vs.VoiceSettingsError) as raised:
            vs.set_meetings({"calendar_autojoin": True})
        self.assertEqual(raised.exception.code, "field")
        (self.root / "data").mkdir(parents=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"meetings": {"calendar_autojoin": True, "allow_programs_join": "yes", "autojoin": 1}}), encoding="utf-8")
        self.assertEqual((vs.meetings()["autojoin"], vs.meetings()["allow_programs_join"]), (False, False))

    def test_turning_either_on_is_logged_and_the_file_is_replaced_whole(self):
        with self.assertLogs("serve.voice_settings", "INFO") as logs:
            vs.set_meetings({"autojoin": True, "allow_programs_join": True})
        text = "\n".join(logs.output)
        self.assertIn("autojoin was turned on", text)
        self.assertIn("allow_programs_join was turned on", text)
        self.assertEqual(list((self.root / "data").glob("*.tmp")), [])

    def test_the_voice_service_reads_the_master_switch_and_ignores_the_other(self):
        import voice  # noqa: F401  (puts voice/ on sys.path)
        from voice import settings_bridge
        vs.set_meetings({"autojoin": True, "allow_programs_join": True, "on_behalf_of": "Mario Rossi"})
        loaded = settings_bridge.load(self.root / "data" / "voice.json")
        self.assertTrue(loaded.meetings.autojoin)
        self.assertEqual(settings_bridge.overrides(loaded)["meetings_autojoin"], True)
        self.assertNotIn("meetings_calendar_autojoin", settings_bridge.overrides(loaded))
        (self.root / "data" / "voice.json").write_text(json.dumps({"meetings": {"calendar_autojoin": True}}), encoding="utf-8")
        self.assertFalse(settings_bridge.load(self.root / "data" / "voice.json").meetings.autojoin)

    def test_the_compat_layer_reads_the_switch_from_the_file_at_every_call(self):
        self.assertFalse(voice_compat._gate_open(voice_compat.find("POST", "/meetbot/join")[0]))
        vs.set_meetings({"allow_programs_join": True})
        self.assertTrue(voice_compat._gate_open(voice_compat.find("POST", "/meetbot/join")[0]))
        self.assertTrue(voice_compat._gate_open(voice_compat.find("GET", "/meetings/plan")[0]))
        vs.set_meetings({"allow_programs_join": False})
        self.assertFalse(voice_compat._gate_open(voice_compat.find("POST", "/meetings/plan")[0]))
        self.assertTrue(voice_compat._gate_open(voice_compat.find("GET", "/meetings/plan")[0]))      # reading is not gated


class ProxyTest(unittest.TestCase):
    def test_the_plan_routes_are_listed_and_nothing_wider(self):
        self.assertIn(("GET", "/meetings/plan"), voice_proxy.ROUTES)
        self.assertIn(("POST", "/meetings/plan"), voice_proxy.ROUTES)
        for method, path in (("PUT", "/meetings/plan/p0123456789ab"), ("DELETE", "/meetings/plan/c-0123456789abcdef"), ("POST", "/meetings/plan/p1/join-now")):
            self.assertTrue(any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS), (method, path))
        for method, path in (("PATCH", "/meetings/plan/p1"), ("PUT", "/meetings/plan"), ("POST", "/meetings/plan/p1"), ("PUT", "/meetings/plan/a/b"),
                             ("POST", "/meetings/plan/p1/join-now/x"), ("PUT", "/meetings/plan/../x")):
            self.assertFalse(any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS) or (method, path) in voice_proxy.ROUTES, (method, path))

    def test_an_item_from_the_page_is_checked(self):
        add = voice_proxy.ROUTES[("POST", "/meetings/plan")]
        ok = {"url": "https://meet.google.com/abc-defg-hij", "start": "2027-01-15T14:00:00.000Z", "title": "Weekly", "role": "speak", "persona": "ada", "camera": True,
              "participate": True, "notes": "n", "duration_min": 30}
        self.assertEqual(add(ok), ok)
        self.assertEqual(add({**ok, "duration_min": None})["duration_min"], None)
        for bad in ({"start": ok["start"]}, {"url": ok["url"]}, {**ok, "rights": "planned"}, {**ok, "id": "x"}, {**ok, "session_id": "x"}, {**ok, "title": "x" * 121},
                    {**ok, "url": "h" * 2001}, {**ok, "camera": "yes"}, {**ok, "participate": 1}, {**ok, "duration_min": True}, {**ok, "duration_min": "30"}, {**ok, "role": 5},
                    {**ok, "notes": "x" * 501}, {**ok, "persona": "p" * 62}, None, [], "x"):
            with self.assertRaises(voice_proxy.Refused, msg=str(bad)):
                add(bad)
        edit = next(c for m, rx, c in voice_proxy.PATTERNS if m == "PUT" and "plan" in rx.pattern)
        self.assertEqual(edit({"participate": False}), {"participate": False})
        self.assertEqual(edit({}), {})
        with self.assertRaises(voice_proxy.Refused):
            edit({"x": 1})

    def test_join_now_needs_the_right_confirmed(self):
        check = next(c for m, rx, c in voice_proxy.PATTERNS if rx.pattern.endswith("/join-now"))
        self.assertEqual(check({"rights_confirmed": True}), {"rights_confirmed": True})
        for bad in ({}, {"rights_confirmed": False}, {"rights_confirmed": "yes"}, {"rights_confirmed": True, "url": "x"}, None):
            with self.assertRaises(voice_proxy.Refused, msg=str(bad)):
                check(bad)

    def test_join_now_waits_as_long_as_a_join(self):
        seen = []
        with mock.patch.object(voice_proxy.voice_service, "call", side_effect=lambda m, p, b, timeout: seen.append(timeout) or (202, {})):
            voice_proxy.call({"method": "POST", "path": "/meetings/plan/p1/join-now", "body": {"rights_confirmed": True}})
            voice_proxy.call({"method": "GET", "path": "/meetings/plan", "body": None})
        self.assertEqual(seen, [60.0, voice_proxy.voice_service.TIMEOUT])


class PageTest(unittest.TestCase):
    def test_the_script_and_the_styles_load_before_the_tab_that_mounts_the_card(self):
        self.assertIn('<link rel="stylesheet" href="web/voiceplan.css">', HTML)
        self.assertEqual(HTML.count("web/voiceplan.js"), 1)
        self.assertLess(HTML.index("web/voicemeet.js"), HTML.index("web/voiceplan.js"))
        self.assertLess(HTML.index("web/voiceplan.js"), HTML.index("web/voicetab.js"))
        self.assertIn("window.VoicePlan = { mount, update,", PLAN)
        for call in ("window.VoicePlan.mount(ui.plan", "window.VoicePlan.update()"):
            self.assertIn(call, MEET)

    def test_every_request_is_on_the_proxys_list(self):
        sample = {"${editing}": "p0123456789ab", "${item.id}": "p0123456789ab"}
        calls = re.findall(r'svc\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*([`"][^`"]+[`"])', PLAN)
        self.assertGreaterEqual(len(calls), 5)
        seen = set()
        for method, raw in calls:
            path = raw[1:-1]
            for key, value in sample.items():
                path = path.replace(key, value)
            self.assertNotIn("${", path, raw)
            listed = (method, path) in voice_proxy.ROUTES or any(m == method and rx.fullmatch(path) for m, rx, _ in voice_proxy.PATTERNS)
            self.assertTrue(listed, (method, path))
            seen.add((method, re.sub(r"p0123456789ab", "<id>", path)))
        self.assertEqual(seen, {("GET", "/meetings/plan"), ("POST", "/meetings/plan"), ("PUT", "/meetings/plan/<id>"), ("DELETE", "/meetings/plan/<id>"),
                                ("POST", "/meetings/plan/<id>/join-now")})
        self.assertNotRegex(PLAN, r'fetch\(\s*["`]/')

    def test_the_bodies_the_page_sends_pass_the_checks(self):
        voice_proxy.ROUTES[("POST", "/meetings/plan")]({"role": "minutes", "persona": "", "camera": False, "participate": True, "notes": "", "title": "", "url": "https://zoom.us/j/1",
                                                      "start": "2027-01-15T14:00:00.000Z", "duration_min": None})
        for needle in ("rights_confirmed: true", "start.toISOString()", 'host.svc("PUT"', "ui.add.hidden"):
            self.assertIn(needle, PLAN)
        self.assertIn("askRights", PLAN)                                           # Join now is not sent before the person ticks the right

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, PLAN)
            self.assertNotIn(banned, MEET)

    def test_every_string_exists_twice_with_the_same_placeholders(self):
        keys = {k for k in re.findall(r'T\("(vp\.[a-z_.0-9]+)"', PLAN) if not k.endswith(".")}      # a name ending in a dot is completed in code, below
        keys |= {"vp.st." + s for s in ("upcoming", "running", "done", "missed", "past")} | {"vp.role." + r for r in ("listen", "minutes", "speak")}
        keys |= {"vp.src." + s for s in ("google", "microsoft")} | {"vp.cam." + r for r in ("no_persona", "persona_missing", "no_avatar")} | {"vp.note." + r for r in ("no_persona", "persona_missing", "no_avatar", "virtual_camera_off", "avatar_not_loaded")}
        keys |= {"vp.title", "vm.set.programs", "vm.set.programs.help", "vm.set.autojoin", "vm.set.autojoin.help", "vm.cal.autojoin.on", "vm.cal.autojoin.off"}
        self.assertGreater(len(keys), 50)
        for key in sorted(keys):
            found = re.findall(r'^\s*"' + re.escape(key) + r'": "((?:[^"\\]|\\.)*)"', I18N, re.M)
            self.assertEqual(len(found), 2, key)
            self.assertEqual(sorted(re.findall(r"\{\d+\}", found[0])), sorted(re.findall(r"\{\d+\}", found[1])), key)
        for gone in ("vm.cal.events", "vm.cal.nolink"):
            self.assertNotIn(f'"{gone}"', I18N)

    def test_the_planner_texts_say_what_the_rules_say(self):
        en = dict(re.findall(r'"((?:vp|vm\.set)\.[a-z_.0-9]+)": "((?:[^"\\]|\\.)*)"', I18N.split("\n  it: {")[0]))
        self.assertIn("cannot be skipped", en["vp.consent"])
        self.assertIn("announces itself", en["vp.consent"])
        self.assertIn("off by default", en["vm.set.autojoin.help"].lower())
        self.assertIn("consent rule still apply", en["vm.set.autojoin.help"])
        self.assertIn("Off by default", en["vm.set.programs.help"])
        self.assertIn("this page is not affected", en["vm.set.programs.help"])

    def test_no_emoji_in_the_new_texts(self):
        for text in re.findall(r'"(?:vp\.[a-z_.0-9]+|vm\.set\.programs[a-z.]*)": "([^"]*)"', I18N):
            self.assertFalse(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text), text)

    def test_the_settings_card_has_both_switches_and_the_old_name_is_gone(self):
        self.assertIn('boolBox("autojoin", "vm.set.autojoin", "vm.set.autojoin.help")', MEET)
        self.assertIn('boolBox("allow_programs_join", "vm.set.programs", "vm.set.programs.help")', MEET)
        for text in (MEET, PLAN, I18N):
            self.assertNotIn("calendar_autojoin", text)

    def test_targets_are_touch_sized_and_the_layout_is_mobile_first(self):
        self.assertRegex(CSS, r"datetime-local[^{]*\{[^}]*min-height: 44px")
        self.assertIn("font: 16px/1.4 var(--font-sans)", CSS)                      # 16px keeps a phone from zooming into the field
        self.assertRegex(CSS, r"\.vp-item \.vt-check \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.vp-plan \.btn \{[^}]*min-height: 44px")
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertNotRegex(CSS, r"(?<!-)\bwidth: \d{4,}px")
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotIn(":hover", CSS)                                            # nothing depends on hover

    def test_the_controls_and_states_are_there(self):
        for control in ("vp-add", "vp-save", "vp-cancel", "vp-title", "vp-url", "vp-start", "vp-role", "vp-persona", "vp-camera", "vp-participate", "vp-notes"):
            self.assertIn(control, PLAN)
        for state in ('"missed"', '"done"', '"running"', '"upcoming"'):
            self.assertIn(state, PLAN)
        self.assertIn("T(\"vp.delete.confirm\")", PLAN)                              # a delete is asked twice
        self.assertIn("host.hasRecord(it.session_id)", PLAN)                        # done: a link to the record, or the plain fact that none is kept
        self.assertIn('T("vp.consent")', PLAN)                                       # the consent reminder is on the add form
        self.assertIn("(plan.platforms || [])", PLAN)                                # only the registered adapters are named


if __name__ == "__main__":
    unittest.main()
