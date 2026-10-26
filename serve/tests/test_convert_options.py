"""What a conversion can be told: the name, the expert profile, the drafter, the projector and the BF16 rounding; each choice shows in the plan
(the command, the steps, the size) and a wrong one is a problem of the plan, not a failed job."""
from __future__ import annotations

import struct
import textwrap
import time
import unittest
from unittest import mock

from serve import models
from serve.tests.test_models import FAKE_CONVERT, FAKE_FETCH, SMALL, Base, BF16, F32, projector_gguf, tiny_basalt, tiny_qwen

FAKE_SECTION = textwrap.dedent("""
    import sys, os
    a = sys.argv[1:]
    src, section = a[0], a[1]
    out = a[a.index("--out") + 1]
    open(out, "wb").write(("copied " + section + " from " + src).encode())
    print("copied", flush=True)
""")


def profile_blob(magic: bytes = b"") -> bytes:
    n_layer, n_expert = SMALL["n_layer"], 4
    pairs = [(1, 2), (0, 3), (0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 3)]
    table = [-1] * (n_layer * n_expert)
    for rank, (layer, e) in enumerate(pairs):
        table[layer * n_expert + e] = rank
    return (magic + struct.pack("<5I", 1, n_layer, n_expert, len(pairs), len(pairs)) + b"".join(struct.pack("<HH", layer, e) for layer, e in pairs)
            + struct.pack(f"<{len(table)}i", *table))


class Options(Base):
    def setUp(self):
        super().setUp()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name, text in (("convert.py", FAKE_CONVERT), ("fetch.py", FAKE_FETCH), ("section.py", FAKE_SECTION)):
            (self.tools / name).write_text(text, encoding="utf-8")
        self.patches = [mock.patch.dict(models.TOOLS, {"convert": [str(self.tools / "convert.py")], "fetch": [str(self.tools / "fetch.py"), "fetch"],
                                                       "check": [str(self.tools / "fetch.py"), "verify"], "section": [str(self.tools / "section.py")]}),
                        mock.patch.object(models, "_commit_share", return_value=0.5), mock.patch.object(models, "MTP_DENSE_BYTES", 64)]
        for p in self.patches:
            p.start()
        tiny_qwen(self.models)
        projector_gguf(self.models / "mmproj-Tiny-f16.gguf")
        models.set_dirs([str(self.models)])
        (self.model,) = [m for m in models.scan()["models"] if m["role"] == "model"]
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def plan(self, **options):
        return models.convert_plan(self.model["id"], str(self.out), options=options or None)

    def wait(self):
        deadline = time.time() + 30
        while models.JOBS.running() and time.time() < deadline:
            time.sleep(0.05)
        return models.JOBS.status()

    def value(self, p, flag):
        return p["args"][p["args"].index(flag) + 1]

    def test_without_options_the_plan_is_what_it_was(self):
        p = self.plan()
        self.assertEqual(p["steps"], ["fetch", "check", "convert"])
        self.assertEqual(p["options"], {"name": None, "profile": {"mode": "auto"}, "mtp": {"mode": "original"}, "vision": {"mode": "auto"}, "compat_bf16": "auto"})
        self.assertEqual(self.value(p, "--name"), "Tiny Flash Next IQ3_XXS")

    def test_the_name_the_bf16_rounding_and_a_wrong_option(self):
        p = self.plan(name="  My   model ", compat_bf16=False)
        self.assertEqual(self.value(p, "--name"), "My model")
        self.assertNotIn("--compat-bf16", p["args"])
        self.assertIn("--compat-bf16", self.plan(compat_bf16=True)["args"])
        for bad in ({"name": "x" * 201}, {"profile": "everything"}, {"mtp": {"mode": "tuned_file"}}, {"vision": {"mode": "file"}}, {"compat_bf16": "maybe"},
                    {"mtp": {"mode": "tuned_pack", "pack": "nope"}}):
            with self.assertRaises(models.Refused) as e:
                self.plan(**bad)
            self.assertEqual(e.exception.status, 400, bad)

    def test_the_projector(self):
        full = self.plan()
        none = self.plan(vision="none")
        self.assertIn("--no-vision", none["args"])
        self.assertNotIn("--mmproj", none["args"])
        self.assertLess(none["estimate_bytes"], full["estimate_bytes"])
        other = self.models / "other-projector.gguf"
        projector_gguf(other, mat=BF16, vec=F32)
        p = self.plan(vision={"mode": "file", "path": str(other)})
        self.assertEqual(self.value(p, "--mmproj"), other.as_posix())
        bad = self.plan(vision={"mode": "file", "path": str(self.root / "missing.gguf")})
        self.assertFalse(bad["ok"])
        self.assertIn("vision_file", [x["code"] for x in bad["problems"]])

    def test_the_drafter(self):
        none = self.plan(mtp="none")
        self.assertEqual(none["steps"], ["convert"])
        self.assertNotIn("--mtp-source", none["args"])
        self.assertLess(none["estimate_bytes"], self.plan()["estimate_bytes"])
        layer = self.root / "tuned.bin"
        layer.write_bytes(b"T" * 64)
        p = self.plan(mtp={"mode": "tuned_file", "path": str(layer)})
        self.assertEqual(self.value(p, "--mtp-dense"), layer.as_posix())
        self.assertIn("--mtp-source", p["args"])
        layer.write_bytes(b"T" * 65)
        bad = self.plan(mtp={"mode": "tuned_file", "path": str(layer)})
        self.assertIn("mtp_file", [x["code"] for x in bad["problems"]])
        pack = models.tuned_sources()[0]
        p = self.plan(mtp={"mode": "tuned_pack", "pack": pack["id"]})
        self.assertEqual(p["steps"], ["fetch", "check", "tuned", "convert"])
        self.assertTrue(self.value(p, "--mtp-dense").endswith(f"mtp-dense-{pack['id']}.bin"))
        self.assertEqual((p["tuned"]["url"], p["tuned"]["section"]), (pack["url"], "mtp/dense"))
        self.assertTrue(pack["url"].startswith("https://huggingface.co/jesdga/"))

    def test_the_expert_profile(self):
        user = self.cache / models.PROFILE_DIR / "Tiny-Flash-Next-IQ3_XXS.bin"
        user.parent.mkdir(parents=True)
        user.write_bytes(profile_blob())
        self.assertEqual(self.plan()["profile"], user.as_posix())                 # auto: the one found
        plain = self.plan(profile="plain")
        self.assertIsNone(plain["profile"])
        self.assertNotIn("--profile", plain["args"])
        self.assertTrue(any("plain" in w["en"] for w in plain["warnings"]))
        mine = self.root / "mine.bin"
        mine.write_bytes(profile_blob())
        self.assertEqual(self.value(self.plan(profile={"mode": "file", "path": str(mine)}), "--profile"), mine.as_posix())
        mine.write_bytes(profile_blob()[:-4])
        bad = self.plan(profile={"mode": "file", "path": str(mine)})
        self.assertIn("profile", [x["code"] for x in bad["problems"]])

    def test_the_profile_of_another_model(self):
        src = tiny_basalt(self.root / "src.basalt")                  # its profile section is not a profile: it cannot be the source
        bad = self.plan(profile={"mode": "model", "path": str(src)})
        self.assertIn("profile", [x["code"] for x in bad["problems"]])
        good = self.root / "good.basalt"
        tiny_basalt(good)
        from model_file import ModelWriter
        w = ModelWriter(str(self.root / "withprofile.basalt"))
        for name, data in (("meta", b'{"name": "t"}'), ("dense", b"D" * 10), ("experts", b"E" * 8000),
                           ("experts.layout", b"# experts v5: layer gu_type d_type offset blob_bytes (n_expert 4, total 8000)\n0 18 20 0 1000\n1 18 20 4000 1000\n"),
                           ("profile", profile_blob())):
            w.add_bytes(name, data)
        w.close()
        p = self.plan(profile={"mode": "model", "path": str(self.root / "withprofile.basalt")})
        self.assertTrue(p["ok"], p["problems"])
        self.assertEqual(p["steps"], ["fetch", "check", "profile", "convert"])
        self.assertEqual(self.value(p, "--profile"), p["prepare"][0]["out"])
        models.JOBS.start_convert(self.model["id"], str(self.out), p["token"], True,
                                 options={"profile": {"mode": "model", "path": str(self.root / "withprofile.basalt")}})
        st = self.wait()
        self.assertEqual(st["phase"], "done", st)
        self.assertTrue(open(p["prepare"][0]["out"], "rb").read().startswith(b"copied profile from "))

    def test_the_token_follows_the_options(self):
        self.assertNotEqual(self.plan()["token"], self.plan(vision="none")["token"])
        self.assertEqual(self.plan(vision="none")["token"], self.plan(vision="none")["token"])

    def test_a_conversion_runs_the_steps_the_options_ask_for(self):
        pack = models.tuned_sources()[0]
        opts = {"mtp": {"mode": "tuned_pack", "pack": pack["id"]}, "name": "Chosen"}
        p = self.plan(**opts)
        models.JOBS.start_convert(self.model["id"], str(self.out), p["token"], True, options=opts)
        st = self.wait()
        self.assertEqual(st["phase"], "done", st)
        self.assertEqual(st["steps"], ["fetch", "check", "tuned", "convert"])
        tuned = self.out / "mtp-qwen" / f"mtp-dense-{pack['id']}.bin"
        self.assertTrue(tuned.read_text().startswith("copied mtp/dense from https://huggingface.co/"))
        text = "\n".join(st["log"])
        self.assertIn("--mtp-dense", text)
        self.assertIn("--name Chosen", text)


if __name__ == "__main__":
    unittest.main()
