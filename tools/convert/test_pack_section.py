"""pack_section.py: one section copied out of a .basalt, checked against the file's own table."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pack_section  # noqa: E402
from model_file import ModelWriter  # noqa: E402


class Copy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "m.basalt")
        w = ModelWriter(self.path)
        self.tuned = bytes(range(256)) * 40
        w.add_bytes("meta", b"{}")
        w.add_bytes("mtp/dense", self.tuned)
        w.add_bytes("profile", b"P" * 100)
        w.close()

    def test_a_section_is_copied_whole(self):
        out = os.path.join(self.dir.name, "tuned.bin")
        self.assertEqual(pack_section.copy(self.path, "mtp/dense", out), len(self.tuned))
        self.assertEqual(open(out, "rb").read(), self.tuned)
        self.assertFalse(os.path.exists(out + ".part"))

    def test_the_table_lists_every_section(self):
        self.assertEqual(set(pack_section.table(self.path)), {"meta", "mtp/dense", "profile"})

    def test_a_missing_section_and_a_wrong_hash_are_refused_and_leave_nothing(self):
        out = os.path.join(self.dir.name, "x.bin")
        with self.assertRaises(ValueError):
            pack_section.copy(self.path, "vision/mmproj", out)
        with open(self.path, "r+b") as f:                       # flip a byte inside the section
            off = pack_section.table(self.path)["mtp/dense"][0]
            f.seek(off + 5)
            f.write(b"\xff")
        with self.assertRaises(ValueError):
            pack_section.copy(self.path, "mtp/dense", out)
        self.assertEqual([n for n in os.listdir(self.dir.name) if n.startswith("x.bin")], [])

    def test_a_file_that_is_not_a_model_is_refused(self):
        bad = os.path.join(self.dir.name, "bad.bin")
        open(bad, "wb").write(b"\0" * 70000)
        with self.assertRaises(ValueError):
            pack_section.table(bad)


if __name__ == "__main__":
    unittest.main()
