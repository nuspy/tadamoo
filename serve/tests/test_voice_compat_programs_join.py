"""The routes that let a program start the meeting bot (serve/voice_compat.py, gate `programs_join`): refused for programs by default with a plain 403 and
its code and texts in English and Italian, accepted only while `meetings.allow_programs_join` is on, read at every request, and never in the way of the
app's own Voice tab (which reaches the service through serve/voice_proxy.py, not this port). The voice service is a stand-in; nothing real starts."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from serve import voice_compat, voice_proxy, voice_settings
from serve.tests.test_voice_compat import TOKEN, Base

URL = {"url": "https://meet.google.com/abc-defg-hij", "rights_confirmed": True}
GATED = [("POST", "/meetbot/join", URL), ("POST", "/meetings/plan", {"url": URL["url"], "start": "2027-01-15T15:00:00+01:00"}),
         ("PUT", "/meetings/plan/p0123456789ab", {"participate": True}), ("PATCH", "/meetings/plan/p0123456789ab", {"role": "listen"}),
         ("DELETE", "/meetings/plan/p0123456789ab", None), ("POST", "/meetings/plan/p0123456789ab/join-now", {"rights_confirmed": True}),
         ("POST", "/calendar/events/g:abc123/autojoin", {"enabled": True})]
OPEN = [("GET", "/meetings/plan", None), ("GET", "/meetbot/status", None), ("POST", "/meetbot/leave", {}), ("GET", "/meetings/list", None),
        ("GET", "/calendar/status", None), ("GET", "/calendar/events", None), ("POST", "/calendar/google/connect", {}), ("GET", "/copilot/state", None),
        ("POST", "/copilot/toggle", {}), ("GET", "/meetings/20261109-100000-meet-ab12", None), ("DELETE", "/meetings/20261109-100000-meet-ab12", None)]


class ProgramsJoinTests(Base):
    def test_the_switch_is_off_by_default(self):
        self.assertIs(voice_settings.meetings()["allow_programs_join"], False)
        self.assertIs(voice_settings.meetings()["autojoin"], False)

    def test_every_route_that_starts_the_bot_is_refused_with_a_code_and_two_texts_and_reaches_no_service(self):
        for method, path, body in GATED:
            status, out = self.jsonrequest(method, path, body)
            self.assertEqual(status, 403, f"{method} {path}")
            self.assertEqual(out["code"], "programs_join_off", path)
            self.assertIn("may not start the meeting bot", out["error"])
            self.assertIn("Settings > Voice > Meetings", out["error"])
            self.assertIn("Impostazioni > Voce > Riunioni", out["message_it"])
        self.assertEqual(self.voice.seen, [])

    def test_a_big_refused_body_is_read_away_so_the_caller_sees_the_answer(self):
        status, out = self.jsonrequest("POST", "/meetings/plan", {"url": URL["url"], "notes": "x" * 30000})
        self.assertEqual((status, out["code"]), (403, "programs_join_off"))

    def test_reading_and_leaving_and_the_other_meeting_routes_are_not_gated(self):
        for method, path, body in OPEN:
            status, _ = self.jsonrequest(method, path, body)
            self.assertEqual(status, 200, f"{method} {path}")
            self.assertEqual((self.voice.seen[-1]["method"], self.voice.seen[-1]["path"]), (method, path))

    def test_with_the_switch_on_a_program_on_this_pc_reaches_the_service_and_the_bodies_pass_through(self):
        voice_settings.set_meetings({"allow_programs_join": True})
        for method, path, body in GATED:
            status, out = self.jsonrequest(method, path, body)
            self.assertEqual(status, 200, f"{method} {path}")
            seen = self.voice.seen[-1]
            self.assertEqual((seen["method"], seen["path"], seen["auth"]), (method, path, "Bearer " + TOKEN))
            self.assertEqual(seen["size"], len(json.dumps(body).encode()) if body is not None else 0)

    def test_the_switch_is_read_at_every_request_so_turning_it_off_acts_at_once(self):
        voice_settings.set_meetings({"allow_programs_join": True})
        self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL)[0], 200)
        voice_settings.set_meetings({"allow_programs_join": False})
        self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL)[0], 403)
        self.assertEqual(len([s for s in self.voice.seen if s["path"] == "/meetbot/join"]), 1)

    def test_the_other_switch_does_not_open_it(self):
        voice_settings.set_meetings({"autojoin": True, "speak": True})
        self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL)[0], 403)

    def test_a_missing_key_or_another_pc_is_still_refused_first_by_the_usual_rules(self):
        voice_settings.set_meetings({"allow_programs_join": True})
        self.svc.api_key = "the-key"
        self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL)[0], 401)
        self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL, headers={"Authorization": "Bearer the-key"})[0], 200)
        with mock.patch("serve.voice_compat.settings.is_loopback", return_value=False):
            self.assertEqual(self.jsonrequest("POST", "/meetbot/join", URL, headers={"Authorization": "Bearer the-key"})[1]["code"], "loopback_only")

    def test_a_wrong_method_is_405_and_a_bad_id_is_404(self):
        self.assertEqual(self.jsonrequest("GET", "/meetings/plan/p1/join-now")[0], 405)
        self.assertEqual(self.jsonrequest("PUT", "/meetings/plan")[0], 405)
        self.assertEqual(self.jsonrequest("PUT", "/meetings/plan/../x", {})[0], 404)

    def test_the_table_marks_exactly_these_routes_as_gated_and_local(self):
        gated = {(r.method, r.pattern.pattern) for r in voice_compat.ROUTES if r.gate}
        self.assertEqual(len(gated), len(GATED))
        for r in voice_compat.ROUTES:
            if r.gate:
                self.assertEqual((r.gate, r.kind), (voice_compat.PROGRAMS_JOIN, "local"), r.pattern.pattern)
        self.assertFalse(voice_compat.find("POST", "/meetbot/leave")[0].gate)
        self.assertFalse(voice_compat.find("GET", "/meetings/plan")[0].gate)

    def test_the_apps_own_tab_is_not_affected_with_the_switch_off(self):
        self.assertIs(voice_settings.meetings()["allow_programs_join"], False)
        seen = []

        def call(method, path, body, timeout):
            seen.append((method, path, body))
            return 202, {"ok": True}

        with mock.patch.object(voice_proxy.voice_service, "call", side_effect=call):
            join = {"url": URL["url"], "rights_confirmed": True}
            self.assertEqual(voice_proxy.call({"method": "POST", "path": "/meetbot/join", "body": join})[0], 202)
            item = {"url": URL["url"], "start": "2027-01-15T15:00:00Z", "role": "listen", "persona": "ada", "camera": True, "participate": True}
            self.assertEqual(voice_proxy.call({"method": "POST", "path": "/meetings/plan", "body": item})[0], 202)
            self.assertEqual(voice_proxy.call({"method": "PUT", "path": "/meetings/plan/p0123456789ab", "body": {"participate": False}})[0], 202)
            self.assertEqual(voice_proxy.call({"method": "POST", "path": "/meetings/plan/p0123456789ab/join-now", "body": {"rights_confirmed": True}})[0], 202)
            self.assertEqual(voice_proxy.call({"method": "DELETE", "path": "/meetings/plan/p0123456789ab", "body": None})[0], 202)
            self.assertEqual(voice_proxy.call({"method": "GET", "path": "/meetings/plan", "body": None})[0], 202)
        self.assertEqual([s[:2] for s in seen], [("POST", "/meetbot/join"), ("POST", "/meetings/plan"), ("PUT", "/meetings/plan/p0123456789ab"),
                                                ("POST", "/meetings/plan/p0123456789ab/join-now"), ("DELETE", "/meetings/plan/p0123456789ab"), ("GET", "/meetings/plan")])


if __name__ == "__main__":
    unittest.main()
