"""A vision projector the engine does not read as it is (F16 matrices, F16 or BF16 vectors) is rewritten at the load into one it
does: the values are the same, the types are the encoder's, the file is kept in the cache and one a person chose is read in place.
Sizes are shrunk to a few elements so the files hold real data."""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from serve import models, projector
from serve.engine import modelfile
from serve.tests.test_models import BF16, F16, F32, Q8_0, write_gguf

E, F, PROJ = 8, 16, 4
SMALL = {"VISION_EMBD": E, "VISION_FFN": F, "VISION_HPARAMS": (("clip.vision.embedding_length", E), ("clip.vision.attention.head_count", 16),
                                                                  ("clip.vision.patch_size", 16), ("clip.vision.feed_forward_length", F))}


def tensors(mat, vec):
    t = [("v.patch_embd.weight", [16, 16, 3, E], vec), ("v.patch_embd.weight.1", [16, 16, 3, E], vec), ("v.patch_embd.bias", [E], vec),
         ("v.position_embd.weight", [E, 2304], vec), ("v.post_ln.weight", [E], vec), ("v.post_ln.bias", [E], vec),
         ("mm.0.weight", [4 * E, 4 * E], mat), ("mm.0.bias", [4 * E], vec), ("mm.2.weight", [4 * E, PROJ], mat), ("mm.2.bias", [PROJ], vec)]
    p = "v.blk.0."
    t += [(p + "ln1.weight", [E], vec), (p + "ln1.bias", [E], vec), (p + "ln2.weight", [E], vec), (p + "ln2.bias", [E], vec),
          (p + "attn_qkv.weight", [E, 3 * E], mat), (p + "attn_qkv.bias", [3 * E], vec), (p + "attn_out.weight", [E, E], mat),
          (p + "attn_out.bias", [E], vec), (p + "ffn_up.weight", [E, F], mat), (p + "ffn_up.bias", [F], vec),
          (p + "ffn_down.weight", [F, E], mat), (p + "ffn_down.bias", [E], vec)]
    return t


META = {"general.architecture": "clip", "general.type": "mmproj", "clip.projector_type": "qwen3vl_merger", "clip.vision.embedding_length": E,
        "clip.vision.attention.head_count": 16, "clip.vision.patch_size": 16, "clip.vision.feed_forward_length": F, "clip.vision.block_count": 1,
        "clip.vision.projection_dim": PROJ, "clip.vision.spatial_merge_size": 2, "clip.vision.image_mean": [0.5, 0.5, 0.5],
        "clip.vision.image_std": [0.5, 0.5, 0.5]}


def encode(values: np.ndarray, kind: int) -> bytes:
    if kind == F32:
        return values.astype("<f4").tobytes()
    if kind == F16:
        return values.astype("<f2").tobytes()
    return (values.astype(np.float32).view(np.uint32) >> 16).astype("<u2").tobytes()      # BF16 by truncation: the values are exact in it


def with_data(path: Path, mat: int, vec: int, wrong=(), prefix: bytes = b"") -> dict:
    """A projector whose every element is k/64 for a small k, so it is exact in F16 and BF16; returns {name: float32 values}."""
    write_gguf(path, META, tensors(mat, vec))
    g = models.read_gguf(str(path))
    values, raw = {}, bytearray(path.read_bytes())
    for i, (name, shape, t, off) in enumerate(g["tensors"]):
        n = int(np.prod(shape))
        v = ((np.arange(n) * 7 + i) % 61 - 30).astype(np.float32) / 64
        values[name] = v
        data = encode(v, int(t))
        raw[g["data_start"] + off:g["data_start"] + off + len(data)] = data
    path.write_bytes(prefix + bytes(raw))
    return values


def read_values(path: str, offset: int = 0) -> dict:
    g = models.read_gguf(path, offset)
    raw, out = Path(path).read_bytes(), {}
    for name, shape, t, off in g["tensors"]:
        n, s = int(np.prod(shape)), offset + g["data_start"] + off
        if t == F32:
            out[name] = np.frombuffer(raw[s:s + 4 * n], dtype="<f4")
        elif t == F16:
            out[name] = np.frombuffer(raw[s:s + 2 * n], dtype="<f2").astype(np.float32)
        else:
            out[name] = (np.frombuffer(raw[s:s + 2 * n], dtype="<u2").astype(np.uint32) << 16).view(np.float32)
    return out


class Rewrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.patches = [mock.patch.object(models, k, v) for k, v in SMALL.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_f16_matrices_and_vectors_become_bf16_and_f32_with_the_same_values(self):
        src = self.root / "f16.gguf"
        values = with_data(src, F16, F16)
        out = self.root / "out.gguf"
        self.assertEqual(projector.rewrite(str(src), str(out)), 22)
        g = models.read_gguf(str(out))
        self.assertIsNone(models.projector_problem(g))
        self.assertEqual(models.projector_fix(g)["tensors"], 0)
        self.assertEqual(os.path.getsize(out), models.projector_fix(models.read_gguf(str(src)))["bytes"])
        got = read_values(str(out))
        self.assertEqual(set(got), set(values))
        for name, v in values.items():
            np.testing.assert_array_equal(got[name], v, name)

    def test_bf16_vectors_and_f32_matrices_are_handled_too(self):
        src = self.root / "mix.gguf"
        values = with_data(src, F32, BF16)
        out = self.root / "out.gguf"
        projector.rewrite(str(src), str(out))
        self.assertIsNone(models.projector_problem(models.read_gguf(str(out))))
        for name, v in read_values(str(out)).items():
            np.testing.assert_array_equal(v, values[name], name)

    def test_a_matrix_that_bf16_cannot_hold_is_rounded_to_nearest(self):
        values = np.array([1 / 3, -1 / 3, 1.0, 65504.0, 1e-8], dtype=np.float32)
        rounded = (np.frombuffer(projector._bf16(values), dtype="<u2").astype(np.uint32) << 16).view(np.float32)
        self.assertEqual(float(rounded[2]), 1.0)
        self.assertLessEqual(abs(float(rounded[0]) - 1 / 3), 2 ** -9 / 3 * 2)
        self.assertLess(abs(float(rounded[3]) - 65504.0) / 65504.0, 2 ** -8)
        self.assertEqual(float(rounded[0]), -float(rounded[1]))
        nan = np.frombuffer(projector._bf16(np.array([np.nan], dtype=np.float32)), dtype="<u2")
        self.assertEqual(int(nan[0]), 0x7FC0)

    def test_a_projector_stored_inside_another_file_is_read_at_its_offset(self):
        src = self.root / "inside.bin"
        values = with_data(src, F16, F16, prefix=b"\xab" * 96)
        out = self.root / "out.gguf"
        self.assertEqual(projector.rewrite(str(src), str(out), offset=96), 22)
        for name, v in read_values(str(out)).items():
            np.testing.assert_array_equal(v, values[name], name)

    def test_the_metadata_and_other_tensors_are_kept_and_a_failure_leaves_nothing(self):
        src = self.root / "f16.gguf"
        with_data(src, F16, F16)
        out = self.root / "out.gguf"
        projector.rewrite(str(src), str(out))
        a, b = models.read_gguf(str(src)), models.read_gguf(str(out))
        self.assertEqual(a["meta"], b["meta"])
        self.assertEqual([t[0] for t in a["tensors"]], [t[0] for t in b["tensors"]])
        bad = self.root / "q8.gguf"
        write_gguf(bad, META, tensors(Q8_0, F32))
        target = self.root / "never.gguf"
        with self.assertRaises(ValueError):
            projector.rewrite(str(bad), str(target))
        self.assertFalse(target.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith("never")], [])

    def test_a_truncated_projector_fails_and_leaves_no_file(self):
        src = self.root / "cut.gguf"
        with_data(src, F16, F16)
        src.write_bytes(src.read_bytes()[:-64])
        target = self.root / "t.gguf"
        with self.assertRaises(OSError):
            projector.rewrite(str(src), str(target))
        self.assertFalse(target.exists())


def fake_model_file(root: Path, embedded: Path | None = None):
    path = root / "m.basalt"
    blob = embedded.read_bytes() if embedded else b""
    path.write_bytes(b"\0" * 128 + blob)
    sections = {"meta": (0, 0, bytes(range(32)))}
    if embedded:
        sections["vision/mmproj"] = (128, len(blob), bytes(range(1, 33)))
    return SimpleNamespace(path=str(path), sections=sections, section=lambda n: sections[n][:2])


class TheLoadUsesIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.patches = [mock.patch.object(models, k, v) for k, v in SMALL.items()]
        for p in self.patches:
            p.start()
        self.vdir = self.root / "cache" / "vision"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_the_projector_in_the_model_file_is_rewritten_once_and_the_old_copy_goes(self):
        src = self.root / "p.gguf"
        values = with_data(src, F16, F16)
        mf = fake_model_file(self.root, src)
        first = modelfile.projector_file(mf, self.vdir)
        self.assertIsNone(models.projector_problem(models.read_gguf(str(first))))
        self.assertEqual(models.projector_fix(models.read_gguf(str(first)))["tensors"], 0)
        for name, v in read_values(str(first)).items():
            np.testing.assert_array_equal(v, values[name], name)
        stamp = first.stat().st_mtime_ns
        self.assertEqual(modelfile.projector_file(mf, self.vdir), first)
        self.assertEqual(first.stat().st_mtime_ns, stamp)                       # not made again
        stale = self.vdir / "mmproj-0123456789ab.bf16.gguf"
        stale.write_bytes(b"old")
        modelfile.projector_file(mf, self.vdir)
        self.assertFalse(stale.exists())
        self.assertTrue(first.exists())

    def test_a_projector_the_encoder_reads_is_unpacked_as_it_is(self):
        src = self.root / "p.gguf"
        with_data(src, BF16, F32)
        mf = fake_model_file(self.root, src)
        out = modelfile.projector_file(mf, self.vdir)
        self.assertEqual(out.name, "mmproj.gguf")
        self.assertEqual(out.read_bytes(), src.read_bytes())

    def test_a_chosen_file_is_read_in_place_when_it_reads_and_rewritten_when_it_does_not(self):
        good, other = self.root / "good.gguf", self.root / "f16.gguf"
        with_data(good, BF16, F32)
        with_data(other, F16, F16)
        mf = fake_model_file(self.root)
        self.assertEqual(modelfile.projector_file(mf, self.vdir, str(good)), good)
        made = modelfile.projector_file(mf, self.vdir, str(other))
        self.assertNotEqual(made, other)
        self.assertEqual(models.projector_fix(models.read_gguf(str(made)))["tensors"], 0)

    def test_what_cannot_be_used_says_why(self):
        bad = self.root / "q8.gguf"
        write_gguf(bad, META, tensors(Q8_0, F32))
        with self.assertRaises(ValueError) as e:
            modelfile.projector_file(fake_model_file(self.root), self.vdir, str(bad))
        self.assertIn("Q8_0", str(e.exception))
        with self.assertRaises(ValueError) as e:
            modelfile.projector_file(fake_model_file(self.root), self.vdir, str(self.root / "missing.gguf"))
        self.assertIn("is not there", str(e.exception))


if __name__ == "__main__":
    unittest.main()
