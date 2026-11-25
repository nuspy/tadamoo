"""voice.json is never replaced by what a failed read gave: a read Windows refuses for a moment is tried again, and a write that cannot
read the file raises instead of writing an empty one over it."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_settings

START = {"models_dir": "C:\\Modelli\\basalt\\voice", "brain": {"persona": "shan", "mode": "direct"},
         "language": {"enabled": ["it", "en"]}, "services": {"stt": {"selected": "whisper-medium", "enabled": True}}}


class Refused(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "voice.json"
        self.path.write_text(json.dumps(START), encoding="utf-8")
        patch = mock.patch.object(voice_settings, "_file", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)
        slow = mock.patch.object(voice_settings.time, "sleep")
        slow.start()
        self.addCleanup(slow.stop)

    def refuse(self, times):
        """Path.read_text on voice.json raises PermissionError `times` times, then reads."""
        real, calls = Path.read_text, [0]

        def read_text(path, *a, **k):
            if path == self.path and calls[0] < times:
                calls[0] += 1
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **k)
        return mock.patch.object(Path, "read_text", read_text)

    def test_a_read_refused_for_a_moment_is_tried_again(self):
        with self.refuse(3):
            self.assertEqual(voice_settings._read()["models_dir"], START["models_dir"])

    def test_a_write_after_a_refused_read_keeps_every_other_setting(self):
        with self.refuse(3):
            voice_settings.set_enabled("stt", False)
        now = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({k: now[k] for k in ("models_dir", "brain", "language")}, {k: START[k] for k in ("models_dir", "brain", "language")})
        self.assertFalse(now["services"]["stt"]["enabled"])

    def test_a_write_that_cannot_read_raises_and_leaves_the_file_as_it_is(self):
        before = self.path.read_bytes()
        with self.refuse(10 ** 6):
            with self.assertRaises(PermissionError):
                voice_settings.set_enabled("stt", False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_reader_that_cannot_read_gets_nothing_instead_of_an_error(self):
        with self.refuse(10 ** 6):
            self.assertEqual(voice_settings._read(), {})

    def test_no_file_and_a_broken_file_read_as_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(voice_settings._read(strict=True), {})
        self.path.unlink()
        self.assertEqual(voice_settings._read(strict=True), {})

    def test_a_write_uses_a_temporary_file_of_its_own_and_leaves_none_behind(self):
        stale = self.path.with_name(self.path.name + ".tmp")
        stale.write_text("another writer's", encoding="utf-8")        # the name the writer used to share
        voice_settings.set_enabled("stt", False)
        self.assertEqual(stale.read_text(encoding="utf-8"), "another writer's")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["voice.json", "voice.json.tmp"])

    def test_a_failed_replace_leaves_no_temporary_file_and_the_file_as_it_was(self):
        before = self.path.read_bytes()
        with mock.patch.object(voice_settings.userdata, "replace_file", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                voice_settings.set_enabled("stt", False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["voice.json"])


if __name__ == "__main__":
    unittest.main()
