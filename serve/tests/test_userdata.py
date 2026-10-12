"""The user's data folder: where it is, and the one-off copy of the settings earlier versions kept in the cache."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import loader, models, profiles, userdata


class Where(unittest.TestCase):
    def test_the_variable_wins(self):
        with mock.patch.dict(os.environ, {"BASALT_DATA": "X:/somewhere"}):
            self.assertEqual(userdata.user_data_dir(), Path("X:/somewhere"))

    def test_the_platform_folders(self):
        d = userdata.default_dir
        self.assertEqual(Path(d("nt", "win32", {"APPDATA": "C:/Users/u/AppData/Roaming"}, "C:/Users/u")),
                         Path("C:/Users/u/AppData/Roaming/BasaltPannello"))
        self.assertEqual(Path(d("nt", "win32", {}, "C:/Users/u")), Path("C:/Users/u/AppData/Roaming/BasaltPannello"))
        self.assertEqual(d("posix", "linux", {}, "/home/u"), "/home/u/.local/share/basalt")
        self.assertEqual(d("posix", "linux", {"XDG_DATA_HOME": "/xdg"}, "/home/u"), "/xdg/basalt")
        self.assertEqual(d("posix", "darwin", {}, "/Users/u"), "/Users/u/Library/Application Support/Basalt")
        self.assertEqual(d("nt", "win32", {"BASALT_DATA": "D:/b", "APPDATA": "C:/x"}, "C:/Users/u"), "D:/b")

    def test_no_machine_path_in_the_code(self):
        text = Path(userdata.__file__).read_text(encoding="utf-8")
        for bad in ("atain", "E:/Modelli", "E:\\\\Modelli", "C:/Users/"):
            self.assertNotIn(bad, text)


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache, self.data = root / "cache", root / "data"
        (self.cache / "0123456789abcdef").mkdir(parents=True)
        (self.cache / "profiles.json").write_text(json.dumps(
            {"load": {"Predefinito": {"values": {"engine.kv": "q4_0"}}}, "inference": {}, "default": "Predefinito"}),
            encoding="utf-8")
        (self.cache / "models.json").write_text(json.dumps({"dirs": [str(root)], "default_model": None}),
                                                encoding="utf-8")
        (self.cache / "0123456789abcdef" / "calibration.json").write_text('{"runs": {}}', encoding="utf-8")
        (self.cache / "models-scan.json").write_text("{}", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": str(self.cache), "BASALT_DATA": str(self.data)})
        self.env.start()
        userdata._checked.clear()

    def tearDown(self):
        self.env.stop()
        userdata._checked.clear()
        self.tmp.cleanup()

    def test_the_files_are_copied_never_moved(self):
        self.assertEqual(profiles.default_name(), "Predefinito")
        self.assertTrue((self.data / "profiles.json").is_file())
        self.assertTrue((self.data / "models.json").is_file())
        self.assertTrue((self.data / "calibration" / "0123456789abcdef.json").is_file())
        self.assertFalse((self.data / "models-scan.json").exists())       # a cache stays a cache
        for name in ("profiles.json", "models.json", "0123456789abcdef/calibration.json", "models-scan.json"):
            self.assertTrue((self.cache / name).is_file(), name)          # the old files are all still there
        marker = json.loads((self.data / userdata.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(sorted(marker["copied"]), ["calibration/0123456789abcdef.json", "models.json", "profiles.json"])

    def test_once_only_and_never_over_a_newer_file(self):
        self.data.mkdir()
        (self.data / "profiles.json").write_text('{"load": {}, "inference": {}, "default": "Valori sicuri"}',
                                                 encoding="utf-8")
        self.assertEqual(sorted(userdata.migrate()), ["calibration/0123456789abcdef.json", "models.json"])
        self.assertEqual(profiles.default_name(), "Valori sicuri")       # the data folder's own file stayed
        (self.data / "models.json").unlink()                             # the user removes it: not copied again
        userdata._checked.clear()
        self.assertEqual(userdata.migrate(), [])
        self.assertFalse((self.data / "models.json").exists())

    def test_the_readers_use_the_data_folder(self):
        self.assertEqual(models.store_path(), self.data / "models.json")
        self.assertEqual(profiles.path(), self.data / "profiles.json")
        self.assertEqual(loader.calibration_path({"meta_hash": "0123456789abcdef"}),
                         self.data / "calibration" / "0123456789abcdef.json")
        models.set_dirs([self.tmp.name])
        self.assertIn(self.tmp.name, json.loads((self.data / "models.json").read_text(encoding="utf-8"))["dirs"])

    def test_nothing_to_copy(self):
        empty = Path(self.tmp.name) / "empty"
        with mock.patch.dict(os.environ, {"BASALT_CACHE": str(empty)}):
            self.assertEqual(userdata.migrate(), [])
            self.assertTrue((self.data / userdata.MARKER).is_file())


if __name__ == "__main__":
    unittest.main()
