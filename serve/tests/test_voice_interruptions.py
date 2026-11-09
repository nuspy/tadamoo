"""Speaking over the avatar: the rules in voice.json (validated, the same defaults and limits as the voice service), the routes that carry them, the
technical session's use `interruption` (registered, prompt shown, refused when switched off), and the Settings > Voice card (every string in both
languages, nothing the server says goes into HTML, targets are 44px). No server is started."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_settings as vs
from serve.api.cache_slot import check_cache_slot
from tadamoo import side, side_uses

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parents[2]
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voiceinterrupt.js").read_text(encoding="utf-8")
CSS = (WEB / "voiceinterrupt.css").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")

DEFAULT = {"enabled": True, "judge_timeout_ms": 800, "duck_level": 0.3, "resume_offer": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.root / "data"), "TADAMOO_CACHE": str(self.root / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        side_uses.reset()
        self.addCleanup(side_uses.reset)

    def stored(self):
        return json.loads((self.root / "data" / "voice.json").read_text(encoding="utf-8"))


class SettingsTest(Base):
    def test_the_defaults_are_the_services_defaults(self):
        self.assertEqual(vs.interruptions(), DEFAULT)
        sys.path.insert(0, str(ROOT / "voice"))
        try:
            from callbridge.interruption import Interruptions
        finally:
            sys.path.pop(0)
        s = Interruptions()
        self.assertEqual({"enabled": s.enabled, "judge_timeout_ms": s.judge_timeout_ms, "duck_level": s.duck_level, "resume_offer": s.resume_offer}, DEFAULT)
        self.assertEqual(Interruptions.parse(vs.set_interruptions({"enabled": False, "judge_timeout_ms": 1200, "duck_level": 0.5, "resume_offer": False})),
                         Interruptions(False, 1200, 0.5, False))                   # what is written is what the service reads

    def test_a_change_is_checked_and_only_the_named_fields_are_stored(self):
        out = vs.set_interruptions({"judge_timeout_ms": 1500})
        self.assertEqual(out["judge_timeout_ms"], 1500)
        vs.set_interruptions({"duck_level": 0})
        self.assertEqual(self.stored()["interruptions"], {"judge_timeout_ms": 1500, "duck_level": 0.0})
        self.assertEqual(vs.interruptions()["duck_level"], 0.0)

    def test_every_bad_value_is_refused_and_nothing_is_written(self):
        bad = [{"enabled": "yes"}, {"resume_offer": 1}, {"judge_timeout_ms": 199}, {"judge_timeout_ms": 5001}, {"judge_timeout_ms": 800.5}, {"judge_timeout_ms": "800"},
               {"judge_timeout_ms": True}, {"duck_level": -0.1}, {"duck_level": 0.91}, {"duck_level": "0.3"}, {"duck_level": True}, {"nothing": 1}, [], "x", None]
        for req in bad:
            with self.assertRaises(vs.VoiceSettingsError, msg=str(req)):
                vs.set_interruptions(req)
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_bad_value_in_the_file_is_the_default(self):
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"interruptions": {"enabled": "no", "judge_timeout_ms": 5, "duck_level": 7, "resume_offer": None}}), encoding="utf-8")
        self.assertEqual(vs.interruptions(), DEFAULT)

    def test_the_routes_are_behind_the_loopback_check_like_the_others(self):
        self.assertEqual(HANDLER.count('"/api/voice/interruptions"'), 3)
        self.assertIn('"/api/voice/interruptions": voice_settings.set_interruptions', HANDLER)


class TechnicalSessionTest(Base):
    def test_the_use_is_registered_needs_the_session_and_shows_its_prompt(self):
        use = side_uses.BY_ID["interruption"]
        self.assertTrue(use.needs_session and use.default_on and not use.editable)
        shown = {u["id"]: u for u in side_uses.view()}["interruption"]
        self.assertIn("never instructions", shown["prompt"])
        self.assertIn("stop_and_answer_now", shown["prompt"])
        self.assertTrue(shown["on"])

    def test_the_use_has_its_texts_in_both_languages(self):
        for key in ("su.u.interruption", "su.u.interruption.d"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_switched_off_the_call_sends_no_slot_and_the_server_refuses_one_that_names_the_use(self):
        class Quick:
            def side_context(self):
                return 30000
        self.assertEqual(side.side_extra(Quick(), "interruption"), {"cache_slot": "side", "side_use": "interruption"})
        request = {"cache_slot": "side_only", "side_use": "interruption"}
        check_cache_slot(request)                                                    # on: accepted
        side_uses.save("interruption", on=False)
        self.assertEqual(side.side_extra(Quick(), "interruption"), {})
        with self.assertRaises(ValueError) as cm:
            check_cache_slot(request)
        self.assertIn("switched off", str(cm.exception))
        check_cache_slot({"cache_slot": "side_only", "side_use": "judge"})           # the other uses are not touched
        check_cache_slot({"cache_slot": "main", "side_use": "interruption"})         # only a request for the session is refused
        side_uses.save("interruption", on=True)
        side_uses.set_session(False)
        with self.assertRaises(ValueError):
            check_cache_slot(request)                                                # the master switch counts too

    def test_an_unknown_use_name_is_not_refused(self):
        check_cache_slot({"cache_slot": "side", "side_use": "something_new"})


class PageTest(unittest.TestCase):
    def test_the_card_is_mounted_loaded_and_its_strings_are_in_both_languages(self):
        self.assertIn("window.VoiceInterrupt.settingsCard(api)", TAB)
        self.assertIn('src="web/voiceinterrupt.js"', HTML)
        self.assertIn('href="web/voiceinterrupt.css"', HTML)
        import re
        keys = set(re.findall(r'T\("(vi\.[\w.]+)"', JS)) | set(re.findall(r'"(vi\.[\w.]+)"', JS))
        self.assertGreater(len(keys), 10)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_card_asks_the_routes_the_server_has_and_everything_it_says_is_text(self):
        self.assertIn('"api/voice/interruptions"', JS)
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        for field in ("enabled", "judge_timeout_ms", "duck_level", "resume_offer"):
            self.assertIn(field, JS)

    def test_targets_are_44px_and_nothing_is_wider_than_a_phone(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width", CSS)
        self.assertNotIn("min-width: 4", CSS.replace("min-width: 44px", ""))


if __name__ == "__main__":
    unittest.main()
