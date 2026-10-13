"""A .basalt converted without an expert profile (convert.py writes the plain fill order: expert 0 of every layer, then
expert 1, ...) starts its VRAM cache with the lowest-numbered experts; the first real load of such a file ran at 80
tok/s with 80-92% cache hits where 98% was expected. The scan says so, and a conversion plan warns up front."""
from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import models
from serve.tests.test_models import tiny_basalt

N_LAYER, N_EXPERT = 4, 8


def profile_blob(pairs) -> bytes:
    table = [-1] * (N_LAYER * N_EXPERT)
    for rank, (layer, e) in enumerate(pairs):
        table[layer * N_EXPERT + e] = rank
    return (struct.pack("<5I", 1, N_LAYER, N_EXPERT, len(pairs), len(pairs))
            + b"".join(struct.pack("<HH", layer, e) for layer, e in pairs)
            + struct.pack(f"<{len(table)}i", *table))


def with_profile(path: Path, blob: bytes) -> Path:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from model_file import ModelFile, ModelWriter
    src = tiny_basalt(path.with_name("src-" + path.name), vision=False)
    mf = ModelFile(str(src))
    w = ModelWriter(str(path))
    for name in mf.names():
        w.add_bytes(name, blob if name == "profile" else mf.read(name))
    w.close()
    return path


class PlainProfile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name})
        self.env.start()
        (self.root / "m").mkdir()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_plain_and_ranked(self):
        plain = [(layer, e) for e in range(N_EXPERT) for layer in range(N_LAYER)]
        ranked = [(1, 5), (0, 3), (3, 7), (2, 0)] + [p for p in plain if p not in ((1, 5), (0, 3), (3, 7), (2, 0))]
        a = with_profile(self.root / "m" / "plain.basalt", profile_blob(plain))
        b = with_profile(self.root / "m" / "ranked.basalt", profile_blob(ranked))
        c = with_profile(self.root / "m" / "short.basalt", profile_blob(plain[:12]))   # fewer pairs than experts
        self.assertIs(models.profile_plain(str(a)), True)
        self.assertIs(models.profile_plain(str(b)), False)
        self.assertIs(models.profile_plain(str(c)), True)
        self.assertIsNone(models.profile_plain(str(tiny_basalt(self.root / "none.basalt"))))   # an empty profile
        items = {Path(x["path"]).name: x for x in models.scan([str(self.root / "m")])["models"]}
        self.assertTrue(items["plain.basalt"].get("profile_plain"))
        self.assertTrue(any("without an expert profile" in n["en"] for n in items["plain.basalt"]["notes"]))
        self.assertFalse(items["ranked.basalt"].get("profile_plain"))
        self.assertEqual(items["plain.basalt"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
