"""Dictation in Settings > Voice: the rules in voice.json (validated, the same defaults and hotkey rules as the voice service), the routes that carry
them, the live state asked through the proxy, and the card (every string in both languages, nothing the server says goes into HTML, targets are 44px).
No server is started."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_proxy
from serve import voice_settings as vs

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parents[2]
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicedictation.js").read_text(encoding="utf-8")
CSS = (WEB / "voicedictation.css").read_text(encoding="utf-8")
TAB = (WEB / "voicetab.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")

DEFAULT = {"enabled": True, "hotkey": "alt+space"}


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
    def test_the_defaults_and_the_hotkey_rules_are_the_services(self):
        self.assertEqual(vs.dictation(), DEFAULT)
        sys.path.insert(0, str(ROOT / "voice"))
        try:
            from callbridge.desktop import base
            from callbridge.config import Config
            from voice import settings_bridge as sb
        finally:
            sys.path.pop(0)
        self.assertEqual(vs._HOTKEY_KEYS, base.KEY_NAMES)
        self.assertEqual(set(vs._HOTKEY_MODS), {"alt", "ctrl", "shift", "win"})
        c = Config()
        self.assertEqual({"enabled": c.dictation_enabled, "hotkey": c.dictation_hotkey}, DEFAULT)
        for sample in ("alt+space", "Ctrl+Shift+F9", "f8", "a", "ctrl+a", "alt+", "alt+nokey", "alt+ctrl", "space", "win+up", "", " ", "x" * 50, "ctrl+ctrl+a", "ctrl+a+b", "ctrl+f1+f2"):
            self.assertEqual(vs.clean_hotkey(sample) is not None, sb.hotkey_ok(sample), sample)       # one rule on both sides
        written = vs.set_dictation({"enabled": False, "hotkey": "Ctrl+Shift+F9"})
        self.assertEqual(written, {"enabled": False, "hotkey": "ctrl+shift+f9"})
        s = sb.load(self.root / "data" / "voice.json")
        self.assertEqual((s.dictation.enabled, s.dictation.hotkey), (False, "ctrl+shift+f9"))         # what is written is what the service reads

    def test_a_change_is_checked_and_only_the_named_fields_are_stored(self):
        vs.set_dictation({"hotkey": "ctrl+alt+d"})
        self.assertEqual(self.stored()["dictation"], {"hotkey": "ctrl+alt+d"})
        vs.set_dictation({"enabled": False})
        self.assertEqual(self.stored()["dictation"], {"hotkey": "ctrl+alt+d", "enabled": False})
        self.assertEqual(vs.dictation(), {"enabled": False, "hotkey": "ctrl+alt+d"})

    def test_every_bad_value_is_refused_and_nothing_is_written(self):
        bad = [{"enabled": "yes"}, {"enabled": 1}, {"hotkey": "a"}, {"hotkey": "alt+"}, {"hotkey": "alt+nokey"}, {"hotkey": 5}, {"hotkey": None},
               {"hotkey": "x" * 50}, {"nothing": 1}, [], "x", None]
        for req in bad:
            with self.assertRaises(vs.VoiceSettingsError, msg=str(req)):
                vs.set_dictation(req)
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_bad_value_in_the_file_is_the_default(self):
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "voice.json").write_text(json.dumps({"dictation": {"enabled": "no", "hotkey": "q"}}), encoding="utf-8")
        self.assertEqual(vs.dictation(), DEFAULT)

    def test_the_routes_are_behind_the_loopback_check_like_the_others(self):
        self.assertEqual(HANDLER.count('"/api/voice/dictation"'), 3)
        self.assertIn('"/api/voice/dictation": voice_settings.set_dictation', HANDLER)

    def test_the_live_state_is_one_of_the_routes_the_proxy_allows_and_takes_no_body(self):
        self.assertIn(("GET", "/components/status"), voice_proxy.ROUTES)


class PageTest(unittest.TestCase):
    def test_the_card_is_mounted_loaded_and_its_strings_are_in_both_languages(self):
        self.assertIn("window.VoiceDictation.settingsCard(api)", TAB)
        self.assertIn('src="web/voicedictation.js"', HTML)
        self.assertIn('href="web/voicedictation.css"', HTML)
        keys = set(re.findall(r'T\("(vd\.[\w.]+)"', JS)) | set(re.findall(r'"(vd\.[\w.]+)"', JS))
        self.assertGreater(len(keys), 15)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_card_asks_the_routes_the_server_has_and_everything_it_says_is_text(self):
        self.assertIn('"api/voice/dictation"', JS)
        self.assertIn('"/components/status"', JS)
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        for field in ("enabled", "hotkey"):
            self.assertIn(field, JS)

    def test_targets_are_44px_and_nothing_is_wider_than_a_phone(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width", CSS)
        self.assertNotIn("min-width: 4", CSS.replace("min-width: 44px", ""))


if __name__ == "__main__":
    unittest.main()
