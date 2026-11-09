"""serve/voice_adopt.py: hash-checked hard links of models already on the disk."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import downloads, voice_adopt, voice_catalog


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


A, B, C = b"alpha" * 1000, b"bravo" * 2000, b"charlie" * 500


def fake_entry(id_="fake", files=(("a.bin", A), ("sub/b.bin", B)), unknown_size=False):
    srcs = [{"type": "url", "url": "http://invalid/" + n, "name": n, "sha256": sha(d), "size": None if unknown_size else len(d), "archive": ""}
            for n, d in files if "/" not in n]
    hf = [(n, d) for n, d in files if "/" in n]
    if hf:
        srcs.append({"type": "hf", "repo": "o/r", "revision": "0" * 40, "files": [n for n, _ in hf], "subdir": "", "strip": "",
                     "pins": {n: sha(d) for n, d in hf}, "sizes": {n: len(d) for n, d in hf}})
    return {"id": id_, "service": "tts", "folder": "", "sources": srcs}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "found"
        self.root.mkdir()
        self.models = self.base / "models"
        self.entry = fake_entry()

    def put(self, name, data):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def run_(self, apply=True, entry=None, **kw):
        return voice_adopt.run(str(self.models), [str(self.root)], apply=apply, catalog=[entry or self.entry], **kw)

    def dest(self, name, id_="fake"):
        return self.models / "tts" / id_ / name


class Adopt(Case):
    def test_a_matching_hash_is_linked_and_the_entry_becomes_installed(self):
        a, b = self.put("x/anything.dat", A), self.put("other.dat", B)
        rep = self.run_()
        e = rep["entries"][0]
        self.assertEqual(e["status"], "complete")
        self.assertEqual(e["marker"], "written")
        self.assertTrue(voice_catalog.installed({**self.entry, "probe": []}, str(self.models)))
        self.assertTrue(os.path.samefile(a, self.dest("a.bin")))
        self.assertTrue(os.path.samefile(b, self.dest("sub/b.bin")))
        self.assertEqual(rep["summary"]["bytes_saved"], len(A) + len(B))

    def test_a_plan_changes_nothing(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        rep = self.run_(apply=False)
        self.assertEqual(rep["entries"][0]["status"], "would_be_complete")
        self.assertEqual(rep["entries"][0]["marker"], "would_write")
        self.assertFalse(self.models.exists())

    def test_a_different_hash_is_not_linked(self):
        self.put("a.bin", b"z" * len(A))
        self.put("b.bin", B)
        e = self.run_()["entries"][0]
        by = {f["name"]: f for f in e["files"]}
        self.assertEqual(by["a.bin"]["state"], "source_differs")
        self.assertEqual(by["sub/b.bin"]["state"], "linked")
        self.assertFalse(self.dest("a.bin").exists())
        self.assertEqual(e["status"], "partial")
        self.assertEqual(e["marker"], "no")
        self.assertFalse((self.models / "tts" / "fake" / voice_catalog.MARKER).exists())

    def test_an_unknown_size_is_found_by_name_and_still_hashed(self):
        ent = fake_entry(unknown_size=True, files=(("a.bin", A),))
        self.put("deep/a.bin", b"q" * len(A))
        self.assertEqual(self.run_(entry=ent, apply=False)["entries"][0]["files"][0]["state"], "source_differs")
        self.put("deeper/a.bin", A)
        self.assertEqual(self.run_(entry=ent, apply=False)["entries"][0]["files"][0]["state"], "linkable")

    def test_a_blob_is_found_by_its_name(self):
        blob = self.root / "models--o--r" / "blobs" / sha(A)
        blob.parent.mkdir(parents=True)
        blob.write_bytes(A)
        ent = fake_entry(unknown_size=True, files=(("zzz.bin", A),))
        f = self.run_(entry=ent)["entries"][0]["files"][0]
        self.assertEqual(f["state"], "linked")
        self.assertTrue(os.path.samefile(blob, self.dest("zzz.bin")))

    def test_an_existing_different_destination_is_untouched(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        d = self.dest("a.bin")
        d.parent.mkdir(parents=True)
        d.write_bytes(b"mine")
        e = self.run_()["entries"][0]
        self.assertEqual({f["name"]: f["state"] for f in e["files"]}["a.bin"], "dest_differs")
        self.assertEqual(d.read_bytes(), b"mine")
        self.assertEqual(e["marker"], "no")

    def test_an_existing_correct_destination_counts_as_done(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        d = self.dest("a.bin")
        d.parent.mkdir(parents=True)
        d.write_bytes(A)                                    # a copy, not a link
        e = self.run_()["entries"][0]
        self.assertEqual({f["name"]: f["state"] for f in e["files"]}["a.bin"], "present")
        self.assertEqual(e["status"], "complete")
        self.assertFalse(os.path.samefile(self.root / "a.bin", d))
        again = self.run_()["entries"][0]
        self.assertEqual(again["marker"], "exists")

    def test_the_marker_is_what_the_downloader_writes(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        self.run_()
        mark = self.models / "tts" / "fake" / voice_catalog.MARKER
        text = mark.read_text(encoding="utf-8")
        got = json.loads(text)
        p = {"id": "fake", "folder": str(self.models / "tts" / "fake"),
             "files": [{"name": "a.bin", "size": len(A), "sha256": sha(A)}, {"name": "sub/b.bin", "size": len(B), "sha256": sha(B)}]}
        dl = downloads.Downloader()
        dl._marker(p)
        self.assertEqual(text, mark.read_text(encoding="utf-8"))
        self.assertEqual(got["files"], p["files"])

    def test_another_volume_is_skipped_with_a_reason_unless_copy_is_asked(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        with mock.patch.object(os, "link", side_effect=OSError("cross-device link")):
            e = self.run_()["entries"][0]
            self.assertTrue(all(f["state"] == "skipped" and "another volume" in f["reason"] for f in e["files"]))
            self.assertEqual(e["marker"], "no")
            self.assertFalse(self.dest("a.bin").exists())
            c = self.run_(copy=True)["entries"][0]
        self.assertEqual({f["state"] for f in c["files"]}, {"copied"})
        self.assertEqual(self.dest("a.bin").read_bytes(), A)
        self.assertEqual(c["marker"], "written")

    def test_no_source_is_ever_modified(self):
        paths = [self.put("a.bin", A), self.put("b.bin", B), self.put("junk.bin", C)]
        before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_size) for p in paths]
        names = sorted(os.listdir(self.root))
        self.run_()
        self.run_()
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_size) for p in paths])
        self.assertEqual(names, sorted(os.listdir(self.root)))

    def test_a_models_folder_inside_a_root_is_refused(self):
        self.put("a.bin", A)
        rep = voice_adopt.run(str(self.root / "models"), [str(self.root)], apply=True, catalog=[self.entry])
        self.assertIn("error", rep)
        self.assertFalse((self.root / "models").exists())

    def test_symlinks_are_never_sources(self):
        real = self.base / "elsewhere.bin"
        real.write_bytes(A)
        try:
            os.symlink(real, self.root / "a.bin")
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        ent = fake_entry(files=(("a.bin", A),))
        self.assertEqual(self.run_(entry=ent, apply=False)["entries"][0]["files"][0]["state"], "missing")

    def test_a_bad_file_does_not_stop_the_others(self):
        self.put("a.bin", A)
        self.put("b.bin", B)
        real = voice_adopt._place
        def flaky(f, *a, **k):
            if f["name"] == "a.bin":
                raise RuntimeError("boom")
            return real(f, *a, **k)
        with mock.patch.object(voice_adopt, "_place", flaky):
            e = self.run_()["entries"][0]
        self.assertEqual({f["name"]: f["state"] for f in e["files"]}, {"a.bin": "skipped", "sub/b.bin": "linked"})

    def test_an_unpinned_file_is_never_linked(self):
        ent = fake_entry(files=(("a.bin", A),))
        ent["sources"][0]["sha256"] = None
        self.put("a.bin", A)
        self.assertEqual(self.run_(entry=ent)["entries"][0]["files"][0]["state"], "missing")


class Downloader(Case):
    def test_the_downloader_skips_a_linked_file_and_asks_only_for_the_missing_one(self):
        self.put("a.bin", A)
        e = self.run_()["entries"][0]                       # a.bin linked, sub/b.bin missing: partial, no marker
        self.assertEqual(e["status"], "partial")
        dl_entry = {"id": "fake", "name": "Fake", "kind": "voice", "convert": "ok", "gated": False, "uncensored": False, "repo": "",
                    "revision": "main", "files": ["*"], "folder": "tts/fake", "license_info": voice_catalog.LICENSES["mit"],
                    "noncommercial": False, "marker": True, "why_not": None, "license": "mit",
                    "sources": [{"type": "url", "url": "http://invalid/a.bin", "name": "a.bin", "sha256": sha(A), "size": len(A), "archive": ""},
                                {"type": "url", "url": "http://invalid/b.bin", "name": "b.bin", "sha256": sha(B), "size": len(B), "archive": ""}]}
        with mock.patch.object(downloads, "find_entry", return_value=dl_entry):
            p = downloads.plan("fake", str(self.models), folders=[])
        by = {f["name"]: f for f in p["files"]}
        self.assertTrue(by["a.bin"]["have"])
        self.assertFalse(by["b.bin"]["have"])
        self.assertEqual(p["remaining_bytes"], len(B))


if __name__ == "__main__":
    unittest.main()
