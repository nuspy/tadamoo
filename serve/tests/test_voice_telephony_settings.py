"""Settings > Voice > Phone calls: the telephony rules in voice.json (validated, atomic, careful defaults), the routes that carry them, the
voice tab's calls to the service for approvals and the log, and the page. No server is started."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice  # noqa: F401  (puts voice/ on sys.path so that `callbridge` imports by that name)
from serve import voice_api, voice_proxy, voice_settings as vs

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parents[2]
JS = (WEB / "voice.js").read_text(encoding="utf-8")
CSS = (WEB / "voice.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")


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


CAREFUL = {"transport": "off", "outgoing_approval": "manual", "ai_disclosure_on_start": True, "incoming_auto_answer": False, "on_behalf_of": ""}


class TelephonySettingsTest(Base):
    def test_the_defaults_are_the_careful_ones(self):
        self.assertEqual(vs.telephony(), CAREFUL)
        self.assertEqual(voice_api.telephony_view(), CAREFUL)

    def test_a_change_is_stored_and_the_others_stay(self):
        vs.set_brain({"mode": "harness"})
        out = vs.set_telephony({"transport": "gsm", "on_behalf_of": "  Mario Rossi "})
        self.assertEqual(out, {**CAREFUL, "transport": "gsm", "on_behalf_of": "Mario Rossi"})
        out = vs.set_telephony({"outgoing_approval": "auto"})
        self.assertEqual((out["transport"], out["outgoing_approval"]), ("gsm", "auto"))
        self.assertEqual(self.stored()["brain"]["mode"], "harness")
        self.assertEqual(self.stored()["telephony"], {"transport": "gsm", "on_behalf_of": "Mario Rossi", "outgoing_approval": "auto"})
        self.assertFalse(list((self.root / "data").glob("*.tmp")))                     # replaced whole, nothing left behind

    def test_the_two_safety_switches_are_real_booleans_and_say_so_when_they_are_turned(self):
        with self.assertLogs("serve.voice_settings", "INFO") as logged:
            out = vs.set_telephony({"ai_disclosure_on_start": False, "incoming_auto_answer": True})
        self.assertFalse(out["ai_disclosure_on_start"])
        self.assertTrue(out["incoming_auto_answer"])
        self.assertTrue(any("disclosure" in line for line in logged.output))
        for key in ("ai_disclosure_on_start", "incoming_auto_answer"):
            for bad in ("false", 0, None, "no"):
                with self.assertRaises(vs.VoiceSettingsError) as ctx:
                    vs.set_telephony({key: bad})
                self.assertEqual(ctx.exception.code, key)

    def test_a_bad_request_changes_nothing(self):
        vs.set_telephony({"transport": "auto"})
        for bad in ({"transport": "bluetooth"}, {"outgoing_approval": "yes"}, {"on_behalf_of": "x" * 81}, {"on_behalf_of": "a\nb"}, {"on_behalf_of": 5},
                    {"mode": "x"}, {"transport": "gsm", "nothing": 1}, [], "x", None):
            with self.assertRaises(vs.VoiceSettingsError):
                vs.set_telephony(bad)
        self.assertEqual(self.stored()["telephony"], {"transport": "auto"})
        self.assertEqual(vs.set_telephony({"on_behalf_of": ""})["on_behalf_of"], "")

    def test_a_damaged_value_in_the_file_reads_as_the_default(self):
        (self.root / "data").mkdir(parents=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"telephony": {
            "transport": "x", "outgoing_approval": 1, "ai_disclosure_on_start": "no", "incoming_auto_answer": "true", "on_behalf_of": "a\nb"}}), encoding="utf-8")
        self.assertEqual(vs.telephony(), CAREFUL)
        (self.root / "data" / "voice.json").write_text(json.dumps({"telephony": 5}), encoding="utf-8")
        self.assertEqual(vs.telephony(), CAREFUL)

    def test_what_the_server_writes_is_what_the_service_reads(self):
        from voice import settings_bridge
        vs.set_telephony({"transport": "android", "outgoing_approval": "auto", "ai_disclosure_on_start": False, "incoming_auto_answer": True,
                          "on_behalf_of": "Mario"})
        got = settings_bridge.load(self.root / "data" / "voice.json").telephony
        self.assertEqual((got.transport, got.outgoing_approval, got.ai_disclosure_on_start, got.incoming_auto_answer, got.on_behalf_of),
                         ("android", "auto", False, True, "Mario"))

    def test_the_values_the_server_accepts_are_the_ones_the_service_accepts(self):
        from callbridge import call_policy
        self.assertEqual(vs.TRANSPORTS, call_policy.TRANSPORT_MODES)
        self.assertEqual(vs.APPROVALS, call_policy.APPROVAL_MODES)

    def test_the_clients_disclosure_markers_are_accepted_by_the_service(self):
        from callbridge.call_policy import has_disclosure
        from tadamoo.integrations import call_api_client as client
        for marker in client.AI_DISCLOSURE_MARKERS:
            self.assertTrue(has_disclosure(marker), marker)
        self.assertTrue(has_disclosure(client.build_greeting("Mario Rossi", "Chiamo per un appuntamento.")))


class RoutesTest(unittest.TestCase):
    def test_the_settings_routes_are_in_the_handler_and_loopback_only(self):
        self.assertEqual(HANDLER.count('"/api/voice/telephony"'), 3)                       # the GET list, the GET answer and the POST table, behind the loopback check
        self.assertIn('"/api/voice/telephony": voice_api.set_telephony', HANDLER)
        self.assertIn("voice_api.telephony_view()", HANDLER)

    def test_the_voice_tab_may_list_and_answer_the_calls_a_program_asked_for_and_read_the_log(self):
        self.assertIn(("GET", "/call/approvals"), voice_proxy.ROUTES)
        self.assertIn(("GET", "/call/audit"), voice_proxy.ROUTES)
        for verdict in ("approve", "deny"):
            self.assertTrue(any(m == "POST" and p.fullmatch(f"/call/approvals/0123456789abcdef/{verdict}") for m, p, _ in voice_proxy.PATTERNS))

    def test_nothing_else_of_the_call_routes_is_reachable_from_the_page(self):
        for path in ("/call", "/hangup", "/external/call", "/call/approvals/x/approve", "/call/approvals/0123456789abcdef/maybe",
                     "/call/approvals/0123456789abcdef/approve/extra", "/call/approvals/../approve"):
            for method in ("GET", "POST"):
                if (method, path) in voice_proxy.ROUTES:
                    continue
                with self.assertRaises(voice_proxy.Refused) as ctx:
                    voice_proxy.call({"method": method, "path": path, "body": {}})
                self.assertEqual(ctx.exception.status, 403)

    def test_an_approval_answer_goes_to_the_service_without_a_body(self):
        seen = []
        with mock.patch.object(voice_proxy.voice_service, "call", side_effect=lambda m, p, b, timeout: seen.append((m, p, b)) or (200, {"status": "approved"})):
            status, data = voice_proxy.call({"method": "POST", "path": "/call/approvals/0123456789abcdef/approve"})
        self.assertEqual((status, data, seen), (200, {"status": "approved"}, [("POST", "/call/approvals/0123456789abcdef/approve", None)]))
        with self.assertRaises(voice_proxy.Refused):
            voice_proxy.call({"method": "POST", "path": "/call/approvals/0123456789abcdef/approve", "body": {"x": 1}})


class PageTest(unittest.TestCase):
    def test_the_card_asks_the_server_only_through_its_own_routes(self):
        for route in ('"api/voice/telephony"', '"api/voice/service/call"', '"/call/approvals"', '"/call/audit"', '"/call/approvals/" + id + "/" + verdict'):
            self.assertIn(route, JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_the_card_has_every_control_and_a_warning_for_each_risky_choice(self):
        for control in ("vo-tel-transport", "vo-tel-approval", "vo-tel-disclosure", "vo-tel-answer", "vo-tel-behalf", "vo-tel-save", "vo-tel-refresh"):
            self.assertIn(control, JS)
        for key in ("vo.tel.approval.warn", "vo.tel.disclosure.warn", "vo.tel.answer.warn"):
            self.assertIn(f'T("{key}")', JS)
        self.assertIn('aria-labelledby', JS)

    def test_the_text_keys_exist_twice_in_the_translations_with_the_same_placeholders(self):
        keys = sorted({k for k in re.findall(r'T\("(vo\.tel\.[a-z.]+)"', JS) if not k.endswith(".")} | {"vo.tel.transport." + v for v in vs.TRANSPORTS} | {"vo.tel.approval." + v for v in vs.APPROVALS}
                      | {"vo.tel.ev.start", "vo.tel.ev.end", "vo.tel.ev.refused", "vo.tel.dir.outgoing", "vo.tel.dir.incoming"})
        self.assertGreater(len(keys), 25)
        for key in keys:
            found = re.findall(r'^\s*"' + re.escape(key) + r'": "((?:[^"\\]|\\.)*)"', I18N, re.M)
            self.assertEqual(len(found), 2, key)
            self.assertEqual(sorted(re.findall(r"\{\d+\}", found[0])), sorted(re.findall(r"\{\d+\}", found[1])), key)

    def test_the_controls_are_touch_sized_and_the_layout_is_mobile_first(self):
        self.assertIn(".vo-root .btn { min-height: 44px; min-width: 44px;", CSS)
        self.assertRegex(CSS, r"\.vo-check \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.vo-field input, \.vo-field select \{[^}]*min-height: 44px")
        self.assertIn(".vo-tel-rows li", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS.split(".vo-tel-rows li")[1].split("}")[0])
        self.assertNotRegex(CSS.split(".vo-tel-live")[1], r"\n\.[a-z-]+ \{[^}]*\bwidth: \d{4,}px")

    def test_the_page_polls_for_waiting_calls_only_while_the_tab_is_shown(self):
        self.assertIn("pollTel", JS)
        self.assertIn("clearTimeout(telTimer)", JS)
        self.assertIn('e.detail !== "voice"', JS)

    def test_no_emoji_in_the_new_texts(self):
        for text in re.findall(r'"vo\.tel\.[a-z.]+": "([^"]*)"', I18N):
            self.assertFalse(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text), text)


if __name__ == "__main__":
    unittest.main()
