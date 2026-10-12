"""The models folders, the header scan and its statuses, the verify and convert jobs (serve/models.py, /api/models),
load profiles with a model and the Loader's model choice. Tiny synthetic files only: a .basalt from
tools/model_file.py's writer and GGUF files whose header declares Qwen3.8-Flash-Next's tensors at a small geometry."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import struct
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from model_file import ModelWriter  # noqa: E402
from serve import loader, loader_api, models, profiles, settings, supervisor  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GiB = 1 << 30
ROOMY = {"ram_total": 128 * GiB, "ram_available": 120 * GiB, "commit_limit": 144 * GiB, "commit_used": 10 * GiB,
         "vram_total": 32 * GiB, "vram_free": 31 * GiB}
SMALL = {"arch": "qwen4exp", "n_embd": 256, "n_ff": 64, "n_layer": 2, "n_expert_max": 4}
IQ3_XXS, IQ4_NL, Q2_K, Q4_0, Q8_0, F32, F16 = 18, 20, 10, 2, 8, 0, 1
BLOCK = {IQ3_XXS: (256, 98), IQ4_NL: (32, 18), Q2_K: (256, 84), Q4_0: (32, 18), Q8_0: (32, 34), F32: (1, 4),
         F16: (1, 2)}


# ------------------------------------------------------------------------------------------------ synthetic files
def write_gguf(path, meta: dict, tensors: list, cut: int = 0) -> Path:
    """A GGUF v3 file: `meta` {key: value} (str, int as u32, float as f32, lists of str / int), `tensors`
    [(name, shape, ggml type)] with zero data of the right size; `cut` bytes are left off the end."""
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    def kv(k, v):
        out = s(k)
        if isinstance(v, str):
            return out + struct.pack("<I", 8) + s(v)
        if isinstance(v, bool):
            return out + struct.pack("<IB", 7, v)
        if isinstance(v, int):
            return out + struct.pack("<II", 4, v)
        if isinstance(v, float):
            return out + struct.pack("<If", 6, v)
        if isinstance(v, list) and v and isinstance(v[0], str):
            return out + struct.pack("<IIQ", 9, 8, len(v)) + b"".join(s(x) for x in v)
        if isinstance(v, list):
            return out + struct.pack("<IIQ", 9, 5, len(v)) + struct.pack(f"<{len(v)}i", *v)
        raise TypeError(v)

    body = struct.pack("<IIQQ", 0x46554747, 3, len(tensors), len(meta)) + b"".join(kv(k, v) for k, v in meta.items())
    off, data = 0, []
    for name, shape, t in tensors:
        n = 1
        for d in shape:
            n *= d
        nb = n // BLOCK[t][0] * BLOCK[t][1]
        body += s(name) + struct.pack("<I", len(shape)) + struct.pack(f"<{len(shape)}Q", *shape) + struct.pack("<IQ", t, off)
        data.append(nb)
        off += (nb + 31) // 32 * 32
    body += b"\0" * ((-len(body)) % 32)
    total = len(body) + off
    p = Path(path)
    with open(p, "wb") as f:
        f.write(body)
        f.truncate(total - cut)
    return p


def qwen_meta(**extra) -> dict:
    g = SMALL
    m = {"general.architecture": "qwen4exp", "general.name": "Tiny Flash Next", "general.file_type": 23,
         "qwen4exp.block_count": g["n_layer"], "qwen4exp.embedding_length": g["n_embd"],
         "qwen4exp.expert_feed_forward_length": g["n_ff"], "qwen4exp.expert_count": 4,
         "tokenizer.ggml.tokens": [f"t{i}" for i in range(300)]}         # a big array: skipped, not kept
    m.update(extra)
    return m


def layer_tensors(layer: int, gu=IQ3_XXS, d=IQ4_NL) -> list:
    g = SMALL
    return [(f"blk.{layer}.ffn_gate_exps.weight", [g["n_embd"], g["n_ff"], 4], gu),
            (f"blk.{layer}.ffn_up_exps.weight", [g["n_embd"], g["n_ff"], 4], gu),
            (f"blk.{layer}.ffn_down_exps.weight", [g["n_ff"], g["n_embd"], 4], d),
            (f"blk.{layer}.ffn_gate_inp.weight", [g["n_embd"], 4], F32),         # the router: BF16 in the engine
            (f"blk.{layer}.attn_q.weight", [g["n_embd"], g["n_embd"]], Q8_0)]    # served from the gguf section


def tiny_qwen(folder: Path, name="Tiny-Flash-Next-IQ3_XXS", shards=2, gu=IQ3_XXS, d=IQ4_NL, drop=None, meta=None):
    paths = []
    for i in range(shards):
        md = qwen_meta(**(meta or {})) if i == 0 else {}
        if shards > 1:
            md.update({"split.no": i, "split.count": shards, "split.tensors.count": 5 * SMALL["n_layer"]})
        layers = [n for n in range(SMALL["n_layer"]) if n % shards == i]
        tensors = [t for n in layers for t in layer_tensors(n, gu, d)]
        fname = f"{name}-{i + 1:05d}-of-{shards:05d}.gguf" if shards > 1 else f"{name}.gguf"
        if drop is not None and i == drop:
            continue
        paths.append(write_gguf(folder / fname, md, tensors))
    return paths


def tiny_basalt(path, name="Tiny basalt", mtp=True, vision=True) -> Path:
    path = Path(path)
    w = ModelWriter(str(path))
    meta = {"format": "basalt", "version": 1, "name": name, "gguf_kv": {"general.file_type": 23},
            "sources": [{"path": "Tiny-IQ3_XXS-00001-of-00002.gguf"}],
            "tensors": {"output.weight": {"bytes": 4096, "shape": [256, 1000]},
                        "token_embd.weight": {"bytes": 4096}, "per_layer_token_embd.weight": {"bytes": 8192}},
            "profile": {"slots": 8}}
    w.add_bytes("meta", json.dumps(meta).encode())
    w.add_bytes("dense", b"\1" * 5000)
    w.add_bytes("experts", b"\2" * 8000)
    w.add_bytes("experts.layout", b"# experts v5: layer gu_type d_type offset blob_bytes (n_expert 4, total 8000)\n"
                                  b"0 18 20 0 1000\n1 18 20 4000 1000\n")
    w.add_bytes("profile", b"\0" * 64)
    if mtp:
        w.add_bytes("mtp/dense", b"\3" * 100)
        w.add_bytes("mtp/experts", b"\4" * 100)
    if vision:
        w.add_bytes("vision/mmproj", b"\5" * 300)
    w.close()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.cache = self.root / "cache"
        self.models = self.root / "models"
        self.models.mkdir()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": str(self.cache), "BASALT_DATA": str(self.cache)})
        self.env.start()
        self.geo = mock.patch.object(models, "GEOMETRY", dict(SMALL))
        self.geo.start()
        models.clear_cache()
        models._cache = None
        self.jobs = mock.patch.object(models, "JOBS", models.Jobs())
        self.jobs.start()

    def tearDown(self):
        deadline = time.time() + 10
        while models.JOBS.running() and time.time() < deadline:
            models.JOBS.cancel()
            time.sleep(0.1)
        self.jobs.stop()
        self.geo.stop()
        self.env.stop()
        models._cache = None
        self.dir.cleanup()

    def by_name(self, listing, needle):
        return [m for m in listing["models"] if needle in Path(m["path"]).name]


# ------------------------------------------------------------------------------------------------ the scan
class Scan(Base):
    def test_a_basalt_file_is_ready_with_its_facts(self):
        tiny_basalt(self.models / "Tiny-IQ3_XXS.basalt")
        (m,) = models.scan([str(self.models)])["models"]
        self.assertEqual((m["status"], m["kind"], m["name"], m["quant"]), ("ready", "basalt", "Tiny basalt", "IQ3_XXS"))
        self.assertEqual((m["mtp"], m["vision"], m["experts_bytes"]), ("present", True, 8000))
        self.assertEqual(m["expert_types"], "IQ3_XXS + IQ4_NL (down)")
        self.assertEqual(models.facts_for(m["path"])["experts_bytes"], 8000)

    def test_broken_basalt_files_say_why(self):
        good = tiny_basalt(self.models / "a.basalt")
        (self.models / "magic.basalt").write_bytes(b"NOTBASALT" + b"\0" * 100)
        ver = tiny_basalt(self.models / "ver.basalt")
        raw = bytearray(ver.read_bytes())
        raw[8:12] = struct.pack("<I", 9)
        ver.write_bytes(bytes(raw))
        cut = tiny_basalt(self.models / "cut.basalt")
        with open(cut, "r+b") as f:
            f.truncate(cut.stat().st_size - 50)
        listing = models.scan([str(self.models)])
        status = {Path(m["path"]).name: (m["status"], m["reason"]["it"]) for m in listing["models"]}
        self.assertEqual(status[good.name][0], "ready")
        self.assertIn("Non è un file .basalt", status["magic.basalt"][1])
        self.assertIn("versione 9", status["ver.basalt"][1])
        self.assertIn("File incompleto", status["cut.basalt"][1])
        self.assertTrue(all(status[n][0] == "incompatible" for n in ("magic.basalt", "ver.basalt", "cut.basalt")))
        with self.assertRaises(ValueError):
            models.check_basalt(str(self.models / "cut.basalt"))

    def test_a_compatible_sharded_gguf_with_its_projector_and_drafter(self):
        tiny_qwen(self.models)
        write_gguf(self.models / "mmproj-Tiny-f16.gguf", {"general.architecture": "clip", "general.type": "mmproj",
                                                          "clip.vision.projection_dim": 256},
                   [("v.patch_embd.weight", [16, 16], F16)])
        write_gguf(self.models / "Tiny-MTP-draft.gguf", qwen_meta(**{"qwen4exp.nextn_predict_layers": 1,
                                                                     "qwen4exp.block_count": 3}),
                   layer_tensors(2, Q8_0, Q8_0))
        listing = models.scan([str(self.models)])
        (m,) = [x for x in listing["models"] if x["role"] == "model"]
        self.assertEqual(m["status"], "convert", m["reason"])
        self.assertEqual(len(m["files"]), 2)
        self.assertTrue(m["vision"] and m["mmproj"].endswith("mmproj-Tiny-f16.gguf"))
        self.assertTrue(m["compat_bf16"])                                 # the F32 routers become BF16
        self.assertEqual(m["mtp"], "fetch")
        self.assertGreater(m["estimate_bytes"], m["size_bytes"])
        self.assertEqual(m["expert_types"], "IQ3_XXS + IQ4_NL (down)")
        roles = {x["role"]: x for x in listing["models"] if x["role"] != "model"}
        self.assertEqual(set(roles), {"mmproj", "drafter"})
        for x in roles.values():
            self.assertEqual((x["status"], x["attached_to"]), ("incompatible", "Tiny Flash Next"))
        self.assertIn("Proiettore visivo", roles["mmproj"]["reason"]["it"])
        self.assertIn("drafter MTP", roles["drafter"]["reason"]["it"])

    def test_a_drafter_source_on_disk_is_reused(self):
        tiny_qwen(self.models, shards=1)
        src = self.models / "mtp-qwen"
        (src / "tensors").mkdir(parents=True)
        (src / "mtp-manifest.json").write_text("[]", encoding="utf-8")
        (m,) = models.scan([str(self.models)])["models"]
        self.assertEqual((m["mtp"], m["mtp_source"]), ("source", src.as_posix()))

    def test_unsupported_shapes_types_and_files(self):
        a, b, c, d, e, f = (self.models / x for x in "abcdef")
        for x in (a, b, c, d, e, f):
            x.mkdir()
        tiny_qwen(a, "Static-Q2_K", shards=1, gu=Q2_K)
        tiny_qwen(b, "Down-Q4_0", shards=1, d=Q4_0)
        tiny_qwen(c, "Half", shards=2, drop=1)
        write_gguf(d / "gemma-3-Q8_0.gguf", {"general.architecture": "gemma3", "general.name": "Gemma"},
                   [("token_embd.weight", [256, 8], Q8_0)])
        tiny_qwen(e, "Wide", shards=1, meta={"qwen4exp.embedding_length": 512})
        (p,) = tiny_qwen(f, "Cut", shards=1)
        with open(p, "r+b") as fh:
            fh.truncate(p.stat().st_size - 100)
        listing = models.scan([str(self.models)])
        why = {Path(m["dir"]).name: m["reason"]["it"] for m in listing["models"]}
        self.assertTrue(all(m["status"] == "incompatible" for m in listing["models"]))
        self.assertIn("gate/up in Q2_K", why["a"])
        self.assertIn("down in Q4_0", why["b"])
        self.assertIn("Mancano parti del modello (1 di 2)", why["c"])
        self.assertIn("Architettura «gemma3»", why["d"])
        self.assertIn("Geometria diversa", why["e"])
        self.assertIn("File incompleto", why["f"])
        self.assertEqual(listing["counts"], {"ready": 0, "convert": 0, "incompatible": 6})

    def test_headers_are_cached_by_size_and_time(self):
        (p,) = tiny_qwen(self.models, shards=1)
        models.scan([str(self.models)])
        with mock.patch.object(models, "gguf_info", side_effect=AssertionError("read again")):
            models.scan([str(self.models)])
        models._cache = None                                            # a new process: from the file on disk
        with mock.patch.object(models, "gguf_info", side_effect=AssertionError("read again")):
            models.scan([str(self.models)])
        os.utime(p, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
        with mock.patch.object(models, "gguf_info", wraps=models.gguf_info) as read:
            models.scan([str(self.models)])
            self.assertEqual(read.call_count, 1)

    def test_hidden_folders_and_unrelated_files_are_skipped(self):
        (self.models / ".cache").mkdir()
        tiny_basalt(self.models / ".cache" / "hidden.basalt")
        (self.models / "notes.txt").write_text("x")
        (self.models / "x.basalt.partial").write_bytes(b"\0" * 10)
        self.assertEqual(models.scan([str(self.models)])["models"], [])


# ------------------------------------------------------------------------------------------------ the folders
class Folders(Base):
    def test_not_set_then_checked_and_saved(self):
        with mock.patch.object(models, "suggestions", return_value=[str(self.models)]):
            out = models.listing()
        self.assertEqual((out["configured"], out["models"], out["suggestions"]), (False, [], [str(self.models)]))
        a_file = self.root / "f.txt"
        a_file.write_text("x")
        with self.assertRaises(models.DirsError) as e:
            models.set_dirs([str(self.root / "missing"), str(a_file), "relative\\dir", str(self.models)])
        self.assertEqual(set(e.exception.errors), {"0", "1", "2"})
        self.assertIn("non esiste", e.exception.errors["0"]["it"])
        self.assertIn("non una cartella", e.exception.errors["1"]["it"])
        self.assertIn("percorso completo", e.exception.errors["2"]["it"])
        self.assertEqual(models.dirs(), [])                             # nothing saved
        other = self.root / "gguf"
        other.mkdir()
        models.set_dirs([str(self.models), str(self.models).upper(), str(other)])
        self.assertEqual(models.dirs(), [str(self.models), str(other)])  # the same folder once
        self.assertTrue(models.listing()["configured"])
        self.assertTrue(models.store_path().is_file())
        with self.assertRaises(models.DirsError):
            models.set_dirs([os.path.abspath(os.sep)])                  # a whole disk

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(models.main(["dirs"]), 0)
        self.assertFalse(json.loads(out.getvalue())["configured"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(models.main(["set-dirs", str(self.root / "nope")]), 2)
        self.assertIn("non esiste", err.getvalue())
        tiny_basalt(self.models / "m.basalt")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(models.main(["set-dirs", str(self.models)]), 0)
            self.assertEqual(models.main(["json"]), 0)
            self.assertEqual(models.main(["list"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(json.loads(lines[1])["models"][0]["status"], "ready")
        self.assertTrue(lines[2].startswith("[Pronto] Tiny basalt"))


# ------------------------------------------------------------------------------------------------ the jobs
FAKE_CONVERT = textwrap.dedent("""
    import sys, time, os
    a = sys.argv[1:]
    out = a[a.index("--out") + 1]
    if os.environ.get("FAKE_SLOW"):
        time.sleep(30)
    with open(out + ".partial", "wb") as f:
        for i in range(5):
            f.write(b"x" * 1000)
            f.flush()
            print("  section", i, flush=True)
            time.sleep(0.05)
    os.replace(out + ".partial", out)
    print("args", " ".join(a), flush=True)
    print("done", flush=True)
""")
FAKE_FETCH = textwrap.dedent("""
    import sys, os
    out = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(os.path.join(out, "tensors"), exist_ok=True)
    open(os.path.join(out, "mtp-manifest.json"), "w").write("[]")
    print(sys.argv[1], "ok", flush=True)
""")


class Jobs(Base):
    def setUp(self):
        super().setUp()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        (self.tools / "convert.py").write_text(FAKE_CONVERT, encoding="utf-8")
        (self.tools / "fetch.py").write_text(FAKE_FETCH, encoding="utf-8")
        self.patches = [mock.patch.dict(models.TOOLS, {"convert": [str(self.tools / "convert.py")],
                                                       "fetch": [str(self.tools / "fetch.py"), "fetch"],
                                                       "check": [str(self.tools / "fetch.py"), "verify"]}),
                        mock.patch.object(models, "_commit_share", return_value=0.5)]
        for p in self.patches:
            p.start()
        tiny_qwen(self.models)
        write_gguf(self.models / "mmproj-Tiny-f16.gguf", {"general.architecture": "clip"}, [("v.w", [16], F16)])
        models.set_dirs([str(self.models)])
        (self.model,) = [m for m in models.scan()["models"] if m["role"] == "model"]
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def wait(self):
        deadline = time.time() + 30
        while models.JOBS.running() and time.time() < deadline:
            time.sleep(0.05)
        return models.JOBS.status()

    def test_the_plan_states_the_size_and_the_disk(self):
        p = models.convert_plan(self.model["id"], str(self.out))
        self.assertTrue(p["ok"], p["problems"])
        self.assertEqual(p["out_path"], (self.out / "Tiny-Flash-Next-IQ3_XXS.basalt").as_posix())
        self.assertIn("Scrive un nuovo file di circa", p["summary"]["it"])
        self.assertIn(p["disk"], p["summary"]["it"])
        self.assertIn("scaricato il drafter MTP", p["summary"]["it"])
        self.assertEqual(p["steps"], ["fetch", "check", "convert"])
        for flag in ("--compat-bf16", "--mmproj", "--mtp-source"):
            self.assertIn(flag, p["args"])
        self.assertIsNotNone(p["free_bytes"])

    def test_a_conversion_needs_the_confirmation_of_the_plan(self):
        p = models.convert_plan(self.model["id"], str(self.out))
        for token, confirm, code in ((p["token"], None, "confirm"), (None, True, "confirm"),
                                     ("0" * 16, True, "plan_changed")):
            with self.assertRaises(models.Refused) as e:
                models.JOBS.start_convert(self.model["id"], str(self.out), token, confirm)
            self.assertEqual((e.exception.status, e.exception.body["error"]["code"]), (409, code))
            self.assertIn("plan", e.exception.body)
        self.assertFalse(models.JOBS.running())
        self.assertEqual(list(self.out.iterdir()), [])                  # nothing written
        models.JOBS.start_convert(self.model["id"], str(self.out), p["token"], True)
        st = self.wait()
        self.assertEqual(st["phase"], "done", st)
        self.assertEqual(st["progress"], 1.0)
        self.assertEqual(st["steps"], ["fetch", "check", "convert"])
        self.assertTrue((self.out / "Tiny-Flash-Next-IQ3_XXS.basalt").is_file())
        self.assertTrue((self.out / "mtp-qwen" / "mtp-manifest.json").is_file())
        self.assertTrue(any(line.startswith("args --gguf") and "--compat-bf16" in line for line in st["log"]))
        self.assertTrue(Path(st["log_file"]).is_file())
        self.assertEqual(models.read_store()["output_dir"], self.out.as_posix())
        # the output exists now: a second plan says so
        p2 = models.convert_plan(self.model["id"], str(self.out))
        self.assertIn("exists", [x["code"] for x in p2["problems"]])

    def test_refused_without_disk_space_with_a_high_commit_or_when_busy(self):
        usage = mock.Mock(free=1000, total=10000, used=9000)
        with mock.patch.object(models.shutil, "disk_usage", return_value=usage):
            p = models.convert_plan(self.model["id"], str(self.out))
            self.assertIn("disk", [x["code"] for x in p["problems"]])
            self.assertIn("Spazio insufficiente", p["problems"][0]["text_it"])
            with self.assertRaises(models.Refused) as e:
                models.JOBS.start_convert(self.model["id"], str(self.out), p["token"], True)
            self.assertEqual(e.exception.status, 422)
        with mock.patch.object(models, "_commit_share", return_value=0.8):
            p = models.convert_plan(self.model["id"], str(self.out))
            self.assertEqual([x["code"] for x in p["problems"]], ["commit"])
            self.assertIn("80%", p["problems"][0]["text_it"])
        p = models.convert_plan(self.model["id"], str(self.root / "nowhere"))
        self.assertEqual([x["code"] for x in p["problems"]], ["out_dir"])
        incompatible = next(m for m in models.scan()["models"] if m["role"] == "mmproj")
        p = models.convert_plan(incompatible["id"], str(self.out))
        self.assertIn("not_convertible", [x["code"] for x in p["problems"]])

    def test_cancel(self):
        p = models.convert_plan(self.model["id"], str(self.out))
        with mock.patch.dict(os.environ, {"FAKE_SLOW": "1"}), \
                mock.patch.dict(models.TOOLS, {"fetch": [str(self.tools / "fetch.py"), "fetch"]}):
            models.JOBS.start_convert(self.model["id"], str(self.out), p["token"], True)
            deadline = time.time() + 10
            while models.JOBS.status().get("step") != "convert" and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            with self.assertRaises(models.Refused):                     # one job at a time
                models.JOBS.start_verify(str(tiny_basalt(self.root / "v.basalt")))
            self.assertTrue(models.JOBS.cancel())
            st = self.wait()
        self.assertEqual(st["phase"], "cancelled", st)
        self.assertFalse((self.out / "Tiny-Flash-Next-IQ3_XXS.basalt").exists())

    def test_verify_runs_the_real_tool(self):
        good = tiny_basalt(self.root / "good.basalt")
        models.JOBS.start_verify(str(good))
        st = self.wait()
        self.assertEqual((st["phase"], st.get("result"), st["progress"]), ("done", "ok", 1.0), st)
        self.assertTrue(any(line.startswith("ok ") and "experts" in line for line in st["log"]))
        bad = tiny_basalt(self.root / "bad.basalt")
        raw = bytearray(bad.read_bytes())
        raw[-10] ^= 0xFF                                                # a flipped byte in the last section
        bad.write_bytes(bytes(raw))
        models.JOBS.start_verify(str(bad))
        st = self.wait()
        self.assertEqual((st["phase"], st.get("result")), ("failed", "bad"), st)
        self.assertTrue(any(line.startswith("BAD ") for line in st["log"]))
        with self.assertRaises(models.Refused):
            models.JOBS.start_verify(str(self.models / "Tiny-Flash-Next-IQ3_XXS-00001-of-00002.gguf"))


# ------------------------------------------------------------------------------------------------ profiles and Loader
class WithModel(Base):
    def setUp(self):
        super().setUp()
        self.a = tiny_basalt(self.models / "A-IQ3_XXS.basalt", name="Modello A")
        self.b = tiny_basalt(self.models / "B-Q2_0.basalt", name="Modello B")
        self.cfg = self.root / "basalt_config.json"
        self.cfg.write_text(json.dumps({"server": {"model": self.a.as_posix(), "alias": "Nome A", "port": 7232},
                                        "engine": {"kv": "int8"}}), encoding="utf-8")

    def test_profiles_keep_their_model(self):
        profiles.change({"action": "save", "name": "Con B", "values": {"engine.kv": "q4_0"}, "model": str(self.b)})
        with self.assertRaises(ValueError):
            profiles.change({"action": "save", "name": "x", "values": {}, "model": str(self.root / "no.basalt")})
        with self.assertRaises(ValueError):
            profiles.change({"action": "save", "name": "x", "values": {}, "model": "relative.basalt"})
        lst = profiles.listing(ROOMY)
        b = next(p for p in lst["load"] if p["name"] == "Con B")
        self.assertEqual((b["model"], b["summary"]["model"]), (self.b.as_posix(), "B-Q2_0"))
        self.assertIsNone(lst["load"][0]["model"])                      # Valori sicuri keeps the file's model
        profiles.apply(profiles.SAFE_NAME, str(self.cfg))
        self.assertEqual(settings.model_of(str(self.cfg)), self.a.as_posix())
        profiles.apply("Con B", str(self.cfg))
        new = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual((new["server"]["model"], new["server"]["alias"], new["engine"]["kv"]),
                         (self.b.as_posix(), "Modello B", "q4_0"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(profiles.main(["apply", profiles.SAFE_NAME, "--config", str(self.cfg),
                                            "--model", str(self.a)]), 0)
        self.assertEqual(json.loads(out.getvalue())["model"], self.a.as_posix())
        data = profiles.export()
        data["load"]["Con B"]["model"] = str(self.root / "gone.basalt")  # exported on another PC
        profiles.change({"action": "import", "data": data})
        self.assertIsNone(profiles.get_model("Con B"))

    def test_the_loader_plans_and_loads_another_model(self):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        svc.settings_info = {"config": str(self.cfg), "model": str(self.a), "cli": [],
                             "running": {"engine.max-context": 4096}}
        svc.supervisor = supervisor.Supervisor(svc, ["--mock-engine", "--config", str(self.cfg)], mock=True,
                                               facts=models.facts_for(str(self.a)))
        with mock.patch.object(loader, "system_now", return_value=dict(ROOMY)), \
                contextlib.redirect_stdout(io.StringIO()):
            p = loader_api.evaluate(svc, {}, model=str(self.b))
            self.assertEqual(p["model"]["name"], "Modello B")
            self.assertEqual(loader_api.info(svc, True)["config_model"], self.a.as_posix())
            with self.assertRaises(loader_api.Refused) as e:
                loader_api.load(svc, {"values": {}, "model": str(self.models / "nope.basalt")})
            self.assertEqual(e.exception.status, 400)
            out = loader_api.load(svc, {"values": {"engine.kv": "q4_0", "engine.expert-cache": 8},
                                         "model": str(self.b)})
        self.assertEqual((out["status"], out["notes"]), ("saved", ["model_on_restart"]))
        self.assertIn("server.model", out["changed"])
        self.assertEqual(settings.model_of(str(self.cfg)), self.b.as_posix())
        self.assertEqual(svc.supervisor.status()["phase"], "idle")       # no engine restart for another model


class StartModel(WithModel):
    def test_precedence_argument_profile_default_config(self):
        cfg = str(self.cfg)
        r = models.resolve(None, None, cfg)
        self.assertEqual((r["source"], r["model"], r["status"]), ("config", self.a.as_posix(), "ready"))
        models.set_default_model(str(self.b))
        self.assertEqual(models.resolve(None, None, cfg)["source"], "default")
        self.assertEqual(models.resolve(None, None, cfg)["model"], self.b.as_posix())
        profiles.change({"action": "save", "name": "P", "values": {}, "model": str(self.a)})
        r = models.resolve(None, "P", cfg)
        self.assertEqual((r["source"], r["model"]), ("profile", self.a.as_posix()))
        r = models.resolve(str(self.b), "P", cfg)
        self.assertEqual((r["source"], r["model"]), ("argument", self.b.as_posix()))
        with self.assertRaises(ValueError):
            models.set_default_model(str(self.root / "nope.basalt"))
        models.set_default_model(None)
        self.assertIsNone(models.default_model())
        empty = self.root / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        r = models.resolve(None, None, str(empty))
        self.assertEqual((r["status"], r["source"]), ("none", None))
        self.assertIn("--model", r["reason"]["it"])

    def test_a_gguf_to_convert_and_a_missing_file(self):
        (g,) = tiny_qwen(self.models, shards=1)
        r = models.resolve(str(g))
        self.assertEqual(r["status"], "convert")
        self.assertEqual(models.resolve(str(self.root / "gone.basalt"))["status"], "missing")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(models.main(["resolve", "--model", str(g)]), 5)
            self.assertEqual(models.main(["resolve", "--model", str(self.a)]), 0)
            with mock.patch.object(models, "_commit_share", return_value=0.5):
                self.assertEqual(models.main(["convert", "--model", str(g), "--out", str(self.root)]), 0)
        plan = json.loads(out.getvalue().splitlines()[-1])
        self.assertTrue(plan["ok"] and plan["token"])
        self.assertIn("Scrive un nuovo file", plan["summary"]["it"])


class SafeValues(unittest.TestCase):
    def test_computed_from_the_hardware_and_the_model(self):
        roomy = dict(ROOMY)
        hybrid = {"logical": 32, "physical": 24, "p_cores": 8, "e_cores": 16, "hybrid": True}
        why = {}
        v = loader.safe(loader.DEMO_FACTS, roomy, cpu=hybrid, why=why)
        self.assertEqual({k: v[k] for k in loader.SAFE if k != "engine.ple-io"},
                         {k: x for k, x in loader.SAFE.items() if k != "engine.ple-io"})   # the documented example
        self.assertIn("8", why["engine.pool-workers"][1])
        # a 16 GB card, a plain 6-core processor, a model without drafter nor projector, less RAM
        small = {**roomy, "vram_total": 16 * GiB, "vram_free": 15 * GiB, "ram_total": 64 * GiB,
                 "ram_available": 60 * GiB, "commit_limit": 80 * GiB}
        facts = {**loader.DEMO_FACTS, "has_mtp": False, "mmproj_bytes": 0}
        v = loader.safe(facts, small, cpu={"logical": 12, "physical": 6, "p_cores": 0, "e_cores": 0, "hybrid": False})
        self.assertEqual((v["engine.vram-reserve-mib"], v["engine.pool-workers"], v["engine.spec"], v["server.vision"]),
                         (3072, 5, 0, "off"))
        self.assertTrue(loader.plan(v, facts, small)["fits"])
        # nothing fits: the shortest context, and the reason says so
        why = {}
        tight = {**roomy, "ram_available": 8 * GiB}
        v = loader.safe(loader.DEMO_FACTS, tight, cpu=hybrid, why=why)
        self.assertEqual(v["engine.max-context"], loader.CONTEXT_STEPS[-1])
        self.assertIn("nessun contesto", why["engine.max-context"][1])

    def test_reserve_warning_names_the_other_programs(self):
        gpu = {"processes": [{"pid": 1, "name": "dwm.exe", "used": None}, {"pid": 2, "name": "basalt.exe"}]}
        self.assertEqual(loader.reserve_warnings({"engine.vram-reserve-mib": 4096, "engine.expert-cache": "auto"}, gpu), [])
        (w,) = loader.reserve_warnings({"engine.vram-reserve-mib": 1024, "engine.expert-cache": "auto"}, gpu)
        self.assertEqual(w["want_mib"], 2048)
        self.assertIn("dwm.exe", w["text_it"])
        self.assertNotIn("basalt.exe", w["text_it"])
        self.assertEqual(loader.reserve_warnings({"engine.vram-reserve-mib": 256, "engine.expert-cache": 100}, gpu), [])


class OverHttp(Base):
    def setUp(self):
        super().setUp()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.system = mock.patch.object(loader, "system_now", return_value=dict(ROOMY))
        self.system.start()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.system.stop()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers or {})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def test_folders_list_and_plan(self):
        js = {"Content-Type": "application/json"}
        code, body = self.req("GET", "/api/models")
        self.assertEqual((code, body["configured"]), (200, False))
        code, body = self.req("POST", "/api/models/dirs", {"dirs": [str(self.root / "nope")]}, js)
        self.assertEqual(code, 400)
        self.assertIn("non esiste", body["fields"]["0"]["it"])
        self.assertEqual(self.req("POST", "/api/models/dirs", {"dirs": [str(self.models)]})[0], 415)
        self.assertEqual(self.req("POST", "/api/models/dirs", {"dirs": [str(self.models)]},
                                  {**js, "Origin": "http://evil.example.com"})[0], 403)
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.req("POST", "/api/models/dirs", {"dirs": [str(self.models)]}, js)[0], 403)
        tiny_basalt(self.models / "m.basalt")
        tiny_qwen(self.models)
        code, body = self.req("POST", "/api/models/dirs", {"dirs": [str(self.models)]}, js)
        self.assertEqual((code, body["configured"], body["counts"]["ready"], body["counts"]["convert"]),
                         (200, True, 1, 1))
        conv = next(m for m in body["models"] if m["status"] == "convert")
        with mock.patch.object(models, "_commit_share", return_value=0.5):
            code, plan = self.req("POST", "/api/models/plan", {"id": conv["id"], "out_dir": str(self.models)}, js)
            self.assertEqual((code, plan["ok"]), (200, True), plan)
            code, body = self.req("POST", "/api/models/convert", {"id": conv["id"], "out_dir": str(self.models)}, js)
            self.assertEqual((code, body["error"]["code"]), (409, "confirm"))
        code, body = self.req("POST", "/api/models/verify", {"path": str(self.models / "missing.basalt")}, js)
        self.assertEqual(code, 400)
        self.assertEqual(self.req("GET", "/api/models/job")[1]["phase"], "idle")
        self.assertEqual(self.req("POST", "/api/models/explode", {}, js)[0], 404)


if __name__ == "__main__":
    unittest.main()
