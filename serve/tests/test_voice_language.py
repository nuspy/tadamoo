"""The spoken language of the avatar: the rules in voice.json (validated, the same defaults and limits as the voice service), the routes that carry them, and
the Settings > Voice card (every string in both languages, nothing the server says goes into HTML, targets are 44px). No server is started."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_settings as vs

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parents[2]
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicelanguage.js").read_text(encoding="utf-8")
CSS = (WEB / "voicelanguage.css").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")

DEFAULT = {"follow": True, "enabled": ["it", "en"], "threshold": 0.8, "short_s": 1.5, "voices": {}}


def service_modules():
    sys.path.insert(0, str(ROOT / "voice"))
    try:
        from callbridge import language_follow, language_names
    finally:
        sys.path.pop(0)
    return language_follow, language_names


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
    def test_the_defaults_are_the_services_defaults_and_the_codes_the_same_as_the_names_table(self):
        self.assertEqual(vs.language(), DEFAULT)
        follow, names = service_modules()
        s = follow.LanguageSettings()
        self.assertEqual({"follow": s.follow, "enabled": list(s.enabled), "threshold": s.threshold, "short_s": s.short_s, "voices": s.voices}, DEFAULT)
        self.assertEqual(tuple(vs.LANGUAGE_CODES), tuple(names.CODES))
        self.assertEqual((vs.LANGUAGE_THRESHOLD, vs.LANGUAGE_SHORT_S, vs.LANGUAGE_MAX_ENABLED), (follow.THRESHOLD_RANGE, follow.SHORT_S_RANGE, follow.MAX_ENABLED))
        out = vs.set_language({"follow": False, "enabled": ["it", "fr"], "threshold": 0.9, "short_s": 2, "voices": {"fr": "ff_siwis"}})
        self.assertEqual(follow.LanguageSettings.parse(out), follow.LanguageSettings(False, ("it", "fr"), 0.9, 2.0, {"fr": "ff_siwis"}))     # what is written is what the service reads

    def test_a_change_is_checked_and_only_the_named_fields_are_stored(self):
        vs.set_language({"threshold": 0.9})
        vs.set_language({"enabled": ["en", "fr"]})
        self.assertEqual(self.stored()["language"], {"threshold": 0.9, "enabled": ["en", "fr"]})
        self.assertEqual(vs.language()["enabled"], ["en", "fr"])

    def test_the_voices_replace_the_map_and_an_empty_name_drops_a_language(self):
        vs.set_language({"voices": {"fr": "ff_siwis", "de": "de_DE-thorsten-medium"}})
        out = vs.set_language({"voices": {"fr": " ff_siwis ", "de": "  "}})
        self.assertEqual(out["voices"], {"fr": "ff_siwis"})

    def test_every_bad_value_is_refused_with_both_languages_and_nothing_is_written(self):
        bad = [{"follow": "yes"}, {"follow": 1}, {"enabled": []}, {"enabled": "it"}, {"enabled": ["it", 5]}, {"enabled": ["it", "xx"]}, {"enabled": ["IT"]},
               {"enabled": list(vs.LANGUAGE_CODES) + ["it"]}, {"threshold": 0.49}, {"threshold": 1}, {"threshold": "0.8"}, {"threshold": True},
               {"short_s": 0.4}, {"short_s": 5.1}, {"short_s": "1"}, {"voices": []}, {"voices": {"xx": "a"}}, {"voices": {"fr": 5}}, {"voices": {"fr": "../x"}},
               {"voices": {"fr": "a/b"}}, {"voices": {"fr": "a\\b"}}, {"voices": {"fr": "x" * 81}}, {"voices": {"fr": "bad\nname"}}, {"engine": "piper"},
               {"nothing": 1}, [], "x", None]
        for req in bad:
            with self.assertRaises(vs.VoiceSettingsError, msg=str(req)) as cm:
                vs.set_language(req)
            body = cm.exception.body()
            self.assertTrue(body["message"] and body["message_it"] and body["message"] != body["message_it"], str(req))
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_bad_value_in_the_file_is_the_default(self):
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"language": {"follow": "no", "enabled": ["xx"], "threshold": 7, "short_s": None, "voices": {"fr": 3, "zz": "a"}}}), encoding="utf-8")
        self.assertEqual(vs.language(), DEFAULT)

    def test_the_routes_are_behind_the_loopback_check_like_the_others(self):
        self.assertEqual(HANDLER.count('"/api/voice/language"'), 3)
        self.assertIn('"/api/voice/language": voice_settings.set_language', HANDLER)


class BridgeTest(Base):
    def test_voice_json_reaches_the_config_the_service_reads_and_a_damaged_file_is_the_defaults(self):
        sys.path.insert(0, str(ROOT / "voice"))
        sys.path.insert(0, str(ROOT))
        try:
            from callbridge.config import Config
            from voice import settings_bridge as sb
        finally:
            sys.path.pop(0)
            sys.path.pop(0)
        path = self.root / "voice.json"
        path.write_text(json.dumps({"language": {"follow": False, "enabled": ["it", "fr", "de"], "threshold": 0.9, "short_s": 2.5, "voices": {"fr": "ff_siwis"}}}), encoding="utf-8")
        config = Config()
        sb.apply(config, sb.load(path))
        self.assertEqual((config.language_follow, config.language_enabled, config.language_threshold, config.language_short_s, config.language_voices),
                         (False, ["it", "fr", "de"], 0.9, 2.5, {"fr": "ff_siwis"}))
        config = Config()
        sb.apply(config, sb.load(self.root / "missing.json"))
        self.assertEqual((config.language_follow, config.language_enabled, config.language_threshold, config.language_short_s, config.language_voices),
                         (True, ["it", "en"], 0.8, 1.5, {}))
        path.write_text("{broken", encoding="utf-8")
        sb.apply(config, sb.load(path))
        self.assertEqual(config.language_threshold, 0.8)


class PageTest(unittest.TestCase):
    def test_the_card_is_mounted_loaded_and_its_strings_are_in_both_languages(self):
        self.assertIn("window.VoiceLanguage.settingsCard(api", TAB)
        self.assertIn('src="web/voicelanguage.js"', HTML)
        self.assertIn('href="web/voicelanguage.css"', HTML)
        keys = set(re.findall(r'T\("(vlg\.[\w.]+)"', JS)) | set(re.findall(r'"(vlg\.[\w.]+)"', JS))
        keys = {k for k in keys if not k.endswith(".")}                         # the language names are built from the code; the next test checks them
        self.assertGreater(len(keys), 15)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_language_names_are_in_both_languages(self):
        _, names = service_modules()
        for code in names.CODES:
            self.assertEqual(I18N.count(f'"vlg.lang.{code}":'), 2, code)

    def test_the_card_asks_the_routes_the_server_has_and_everything_it_says_is_text(self):
        self.assertIn('"api/voice/language"', JS)
        self.assertIn("/session/language", JS)
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        for field in ("follow", "enabled", "threshold", "short_s", "voices"):
            self.assertIn(field, JS)

    def test_targets_are_44px_and_nothing_is_wider_than_a_phone(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width", CSS)
        self.assertNotIn("min-width: 4", CSS.replace("min-width: 44px", ""))


if __name__ == "__main__":
    unittest.main()
