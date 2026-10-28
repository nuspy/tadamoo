"""Tests for qwen35_checkpoint.py on small synthetic safetensors files.

    python3 -m unittest discover -s tools/convert -p 'test_qwen35_checkpoint.py'"""
import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoint as C  # noqa: E402
import model_sections as S  # noqa: E402
import qwen35_checkpoint as Q  # noqa: E402
from test_checkpoint import bf16, modelopt_values, write_safetensors  # noqa: E402

K, V = 4, 8                       # key heads, value heads of the synthetic model
QK = 2 * K * Q.GDN_HEAD
CONV = QK + V * Q.GDN_HEAD


def fp8_codes(rng, shape):
    """E4M3 codes without the NaN pattern, and their float values."""
    codes = rng.integers(0, 256, size=shape, dtype=np.uint8)
    codes[(codes & 0x7F) == 0x7F] = 0x38
    return codes, S.fp8_e4m3_table()[codes]


class HeadOrderTest(unittest.TestCase):
    def test_matches_the_qwen4exp_order_at_its_geometry(self):
        np.testing.assert_array_equal(Q.gdn_order(S.GDN_V_HEADS, S.GDN_K_HEADS), S.gdn_block_source())
        n = S.GDN_HEAD * 48 + 4096
        np.testing.assert_array_equal(Q.gdn_index(n, 4096, 48, 16), S.gdn_index(n, 4096))

    def test_qwen36_geometry(self):
        o = Q.gdn_order(32, 16)
        self.assertEqual(sorted(o), list(range(32)))
        self.assertEqual(o[:4].tolist(), [0, 2, 4, 6])         # ratio 0 of key heads 0..3
        self.assertEqual(o[16:19].tolist(), [1, 3, 5])         # ratio 1 of key heads 0..2
        idx = Q.gdn_index(4096 + 32 * 128, 4096, 32, 16)
        np.testing.assert_array_equal(idx[:4096], np.arange(4096))
        self.assertEqual(idx[4096], 4096)
        self.assertEqual(idx[4096 + 128], 4096 + 2 * 128)


class Fp4Test(unittest.TestCase):
    def test_values_match_the_modelopt_definition(self):
        rng = np.random.default_rng(3)
        codes = rng.integers(0, 256, size=(6, 32), dtype=np.uint8)
        scales = rng.integers(1, 0x7F, size=(6, 4), dtype=np.uint8)
        got = Q.fp4_values(codes, scales, 0.25)
        np.testing.assert_allclose(got, modelopt_values(codes, scales) * 0.25, rtol=1e-6)


class Qwen35DenseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        rng = np.random.default_rng(4)
        H = 256
        self.v = {}
        L0, L1 = "model.language_model.layers.0.", "model.language_model.layers.1."
        tens = {}

        def bf(name, shape, scale=1.0):
            x = (rng.integers(-64, 64, size=shape) * scale / 64).astype(np.float32)
            self.v[name] = x
            tens[name] = ("BF16", shape, bf16(x).tobytes())

        def fp8(name, shape, scale):
            c, x = fp8_codes(rng, shape)
            self.v[name] = x * np.float32(scale)
            tens[name] = ("F8_E4M3", shape, c.tobytes())
            base = name.rsplit(".", 1)[0]
            tens[base + ".weight_scale"] = ("F32", (), np.float32(scale).tobytes())
            tens[base + ".input_scale"] = ("F32", (), np.float32(1).tobytes())

        def fp4(name, rows, cols, scale2):
            c = rng.integers(0, 256, size=(rows, cols // 2), dtype=np.uint8)
            s = rng.integers(1, 0x7F, size=(rows, cols // 16), dtype=np.uint8)
            self.v[name] = Q.fp4_values(c, s, scale2)
            base = name.rsplit(".", 1)[0]
            tens[name] = ("U8", c.shape, c.tobytes())
            tens[base + ".weight_scale"] = ("F8_E4M3", s.shape, s.tobytes())
            tens[base + ".weight_scale_2"] = ("F32", (), np.float32(scale2).tobytes())
            tens[base + ".input_scale"] = ("F32", (), np.float32(1).tobytes())

        bf(L0 + "linear_attn.A_log", (V,))
        bf(L0 + "linear_attn.conv1d.weight", (CONV, 1, 4))
        bf(L0 + "linear_attn.in_proj_a.weight", (V, H))
        fp8(L0 + "linear_attn.in_proj_qkv.weight", (CONV, H), 0.5)
        fp8(L0 + "linear_attn.in_proj_z.weight", (V * 128, H), 0.25)
        fp8(L0 + "linear_attn.out_proj.weight", (H, V * 128), 0.125)
        bf(L0 + "linear_attn.norm.weight", (128,))
        bf(L0 + "input_layernorm.weight", (H,))
        bf(L0 + "mlp.shared_expert_gate.weight", (1, H))
        fp4(L0 + "mlp.shared_expert.down_proj.weight", H, 64, 0.0625)
        bf(L1 + "self_attn.q_norm.weight", (64,))
        fp8(L1 + "self_attn.q_proj.weight", (128, H), 2.0)
        fp4("lm_head.weight", 32, 256, 0.5)
        bf("model.language_model.embed_tokens.weight", (32, H))
        tens[L0 + "mlp.experts.0.gate_proj.weight"] = ("U8", (2, 2), b"\0" * 4)
        tens["mtp.layers.0.mlp.gate.weight"] = ("BF16", (1,), b"\0\0")
        tens["model.visual.x.weight"] = ("BF16", (1,), b"\0\0")
        write_safetensors(d / "model-00001-of-00001.safetensors", tens)
        cfg = {"text_config": {"model_type": "qwen3_5_moe_text", "linear_num_key_heads": K, "linear_num_value_heads": V,
                               "linear_key_head_dim": 128, "linear_value_head_dim": 128,
                               "num_hidden_layers": 2, "layer_types": ["linear_attention", "full_attention"]}}
        (d / "config.json").write_text(json.dumps(cfg))
        self.d, self.L0, self.L1 = d, L0, L1
        self.dn = Q.Qwen35Dense(C.Checkpoint(d), require_complete=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_types_shapes(self):
        t = {n: (x.type_name, x.ne) for n, x in self.dn.tensors.items()}
        self.assertEqual(list(t), [
            "blk.0.ssm_a", "blk.0.ssm_conv1d.weight", "blk.0.ssm_alpha.weight", "blk.0.attn_qkv.weight",
            "blk.0.attn_gate.weight", "blk.0.ssm_out.weight", "blk.0.ssm_norm.weight", "blk.0.attn_norm.weight",
            "blk.0.ffn_gate_inp_shexp.weight", "blk.0.ffn_down_shexp.weight", "blk.1.attn_q_norm.weight",
            "blk.1.attn_q.weight", "output.weight", "token_embd.weight"])
        self.assertEqual(t["blk.0.attn_qkv.weight"], ("Q8_0", [256, CONV]))
        self.assertEqual(t["blk.0.ffn_down_shexp.weight"], ("Q8_0", [64, 256]))
        self.assertEqual(t["output.weight"], ("Q8_0", [256, 32]))
        self.assertEqual(t["blk.0.ffn_gate_inp_shexp.weight"], ("BF16", [256]))
        self.assertEqual(t["blk.0.ssm_conv1d.weight"], ("F32", [4, CONV]))

    def test_transforms(self):
        L, v, dn = self.L0, self.v, self.dn
        order = Q.gdn_order(V, K)
        f32 = lambda n: dn.bytes(n).view("<f4")
        a = (-np.exp(v[L + "linear_attn.A_log"].astype(np.float64))).astype(np.float32)[order]
        np.testing.assert_array_equal(f32("blk.0.ssm_a"), a)
        np.testing.assert_array_equal(f32("blk.0.attn_norm.weight"), v[L + "input_layernorm.weight"] + 1)
        np.testing.assert_array_equal(f32("blk.1.attn_q_norm.weight"), v[self.L1 + "self_attn.q_norm.weight"] + 1)
        conv = v[L + "linear_attn.conv1d.weight"][:, 0, :]
        np.testing.assert_array_equal(f32("blk.0.ssm_conv1d.weight").reshape(CONV, 4), conv[Q.gdn_index(CONV, QK, V, K)])
        self.assertEqual(dn.bytes("blk.0.ssm_alpha.weight").tobytes(),
                         bf16(v[L + "linear_attn.in_proj_a.weight"][order]).tobytes())
        self.assertEqual(dn.bytes("blk.0.ffn_gate_inp_shexp.weight").tobytes(),
                         bf16(v[L + "mlp.shared_expert_gate.weight"][0]).tobytes())

    def test_quantized_streams(self):
        L, v, dn = self.L0, self.v, self.dn
        names = ("blk.0.attn_qkv.weight", "blk.0.attn_gate.weight", "blk.0.ssm_out.weight", "blk.0.ffn_down_shexp.weight",
                 "blk.1.attn_q.weight", "output.weight", "token_embd.weight")
        with multiprocessing.Pool(2) as pool:
            got = {n: b"".join(dn.chunks(n, pool)) for n in names}
        q8 = lambda x: S.q8_0(np.ascontiguousarray(x, dtype=np.float32).reshape(-1))
        self.assertEqual(got["blk.0.attn_qkv.weight"], q8(v[L + "linear_attn.in_proj_qkv.weight"][Q.gdn_index(CONV, QK, V, K)]))
        self.assertEqual(got["blk.0.attn_gate.weight"], q8(v[L + "linear_attn.in_proj_z.weight"][Q.gdn_index(V * 128, 0, V, K)]))
        self.assertEqual(got["blk.0.ssm_out.weight"], q8(v[L + "linear_attn.out_proj.weight"][:, Q.gdn_index(V * 128, 0, V, K)]))
        self.assertEqual(got["blk.0.ffn_down_shexp.weight"], q8(v[L + "mlp.shared_expert.down_proj.weight"]))
        self.assertEqual(got["blk.1.attn_q.weight"], q8(v[self.L1 + "self_attn.q_proj.weight"]))
        self.assertEqual(got["output.weight"], q8(v["lm_head.weight"]))
        self.assertEqual(got["token_embd.weight"], q8(v["model.language_model.embed_tokens.weight"]))
        for n, b in got.items():
            self.assertEqual(len(b), dn.tensors[n].expected_bytes(), n)

    def test_wrong_dtype_and_unknown_are_refused(self):
        p = self.d / "model-00001-of-00001.safetensors"
        head, base = S.read_safetensors_header(p)
        raw = p.read_bytes()
        tens = {k: (h["dtype"], h["shape"], raw[base + h["data_offsets"][0]:base + h["data_offsets"][1]])
                for k, h in head.items()}
        bad = dict(tens)
        bad[self.L0 + "linear_attn.new_thing.weight"] = ("BF16", (2,), b"\0" * 4)
        write_safetensors(p, bad)
        with self.assertRaises(S.ConvertError):
            Q.Qwen35Dense(C.Checkpoint(self.d), require_complete=False)
        bad = dict(tens)
        bad[self.L0 + "input_layernorm.weight"] = ("F32", (256,), b"\0" * 1024)
        write_safetensors(p, bad)
        with self.assertRaises(S.ConvertError):
            Q.Qwen35Dense(C.Checkpoint(self.d), require_complete=False)

    def test_incomplete_checkpoint_is_refused(self):
        with self.assertRaises(S.ConvertError):
            Q.Qwen35Dense(C.Checkpoint(self.d))


if __name__ == "__main__":
    unittest.main()
