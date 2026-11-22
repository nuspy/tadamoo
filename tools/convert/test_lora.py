"""Tests for lora.py. No GPU, no real checkpoint: synthetic PEFT adapters with the real GDN head geometry
(16 key heads, 48 value heads of 128) built directly as safetensors files."""
from __future__ import annotations

import json
import pathlib
import struct
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import checkpoint as CK  # noqa: E402
import lora as L  # noqa: E402
import model_sections as S  # noqa: E402
from gguf_reader import GGUFFile  # noqa: E402

RNG = np.random.default_rng(1234)


def _encode(arr: np.ndarray, dtype: str) -> bytes:
    a = np.ascontiguousarray(arr, dtype="<f4")
    if dtype == "F32":
        return a.tobytes()
    if dtype == "F16":
        return a.astype("<f2").tobytes()
    if dtype == "BF16":
        return (a.view(np.uint32) >> 16).astype("<u2").tobytes()
    raise ValueError(dtype)


# ------------------------------------------------------------------ tiny safetensors writer (PEFT's own format)
def write_safetensors(path: pathlib.Path, tensors: dict[str, np.ndarray], dtype: str = "F32") -> None:
    header = {}
    blobs = []
    offset = 0
    for name, arr in tensors.items():
        raw = _encode(arr, dtype)
        header[name] = {"dtype": dtype, "shape": list(np.asarray(arr).shape), "data_offsets": [offset, offset + len(raw)]}
        blobs.append(raw)
        offset += len(raw)
    header["__metadata__"] = {"format": "pt"}
    raw = json.dumps(header).encode("utf-8")
    pad = (-len(raw)) % 8
    raw += b" " * pad
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(raw)))
        f.write(raw)
        for b in blobs:
            f.write(b)


def write_adapter(tmp: pathlib.Path, tensors: dict[str, np.ndarray], dtype: str = "F32", **cfg_overrides) -> pathlib.Path:
    adapter_dir = tmp / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"peft_type": "LORA", "r": 8, "lora_alpha": 16, "rank_pattern": {}, "alpha_pattern": {},
           "use_rslora": False, "use_dora": False, "modules_to_save": None, "bias": "none",
           "fan_in_fan_out": False}
    cfg.update(cfg_overrides)
    (adapter_dir / "adapter_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    write_safetensors(adapter_dir / "adapter_model.safetensors", tensors, dtype=dtype)
    return adapter_dir


def _base_fields(**overrides) -> dict:
    cfg = {
        "indexer_n_heads": 4, "indexer_head_dim": 16,
        "linear_num_key_heads": S.GDN_K_HEADS, "linear_num_value_heads": S.GDN_V_HEADS,
        "linear_key_head_dim": S.GDN_HEAD, "linear_value_head_dim": S.GDN_HEAD,
    }
    cfg.update(overrides)
    return cfg


def write_base_config(tmp: pathlib.Path, path: str = "config.json", **overrides) -> pathlib.Path:
    """The flat form (no text_config), as most of the tests use it."""
    p = tmp / path
    p.write_text(json.dumps(_base_fields(**overrides)), encoding="utf-8")
    return p


def write_base_config_nested(tmp: pathlib.Path, path: str = "config.json", **overrides) -> pathlib.Path:
    """The real form: the model's text fields live under "text_config", beside an unrelated "vision_config"."""
    p = tmp / path
    whole = {"vision_config": {"not_a_text_field": 1}, "text_config": _base_fields(**overrides)}
    p.write_text(json.dumps(whole), encoding="utf-8")
    return p


def lora_pair(n_out: int, n_in: int, r: int = 8) -> tuple[np.ndarray, np.ndarray]:
    A = RNG.standard_normal((r, n_in)).astype(np.float32) * 0.1
    B = RNG.standard_normal((n_out, r)).astype(np.float32) * 0.1
    return A, B


def read_back(path: pathlib.Path):
    """(name -> f32 array in checkpoint/PyTorch order, the GGUFFile for its metadata)."""
    g = GGUFFile(path)
    out = {}
    with open(path, "rb") as f:
        for t in g.tensors:
            f.seek(g.data_start + t.offset)
            n = 1
            for d in t.shape:
                n *= d
            out[t.name] = np.frombuffer(f.read(n * 4), dtype="<f4").reshape(list(reversed(t.shape))).copy()
    return out, g


# the row/column axis each "how" permutes, exactly as checkpoint.py's jobs() picks it for the real weight
_AXIS = {"vrows": "rows", "zrows": "rows", "heads": "rows", "vcols": "cols"}


def checkpoint_transform(how: str, X: np.ndarray) -> np.ndarray:
    """checkpoint.py's own permutation (CK._index), applied to a plain float matrix exactly the way it is
    applied to the real base weight: rows for vrows/zrows/heads, columns for vcols, unchanged for id."""
    if how == "id":
        return X
    axis = _AXIS[how]
    n = X.shape[0] if axis == "rows" else X.shape[1]
    idx = CK._index(how, n, 2 * S.GDN_K_HEADS * S.GDN_HEAD)
    return X[idx] if axis == "rows" else X[:, idx]


# ------------------------------------------------------------------ T1: reorder() matches checkpoint.py's own index
@pytest.mark.parametrize("how,n_out,n_in,axis", [
    ("id", 32, 24, None),
    ("vrows", S.GDN_QK_ROWS + S.GDN_V_HEADS * S.GDN_HEAD, 24, "rows"),
    ("zrows", S.GDN_V_HEADS * S.GDN_HEAD, 24, "rows"),
    ("heads", S.GDN_V_HEADS, 24, "rows"),
    ("vcols", 24, S.GDN_V_HEADS * S.GDN_HEAD, "cols"),
])
def test_t1_reorder_parity(how, n_out, n_in, axis):
    W = RNG.standard_normal((n_out, n_in)).astype(np.float32)
    A, B = lora_pair(n_out, n_in)
    s = 1.0
    A2, B2 = L.reorder(how, A, B)
    lhs = checkpoint_transform(how, W + s * B @ A)
    rhs = checkpoint_transform(how, W) + s * B2 @ A2
    np.testing.assert_allclose(lhs, rhs, atol=1e-5, rtol=1e-5)


# ------------------------------------------------------------------ T4: unhandled cases raise ConvertError, named
def test_t4_unhandled_cases(tmp_path):
    base_config = write_base_config(tmp_path)

    def expect_error(tensors, cfg_overrides, needle):
        adapter = write_adapter(tmp_path / f"a{RNG.integers(1 << 30)}", tensors, **cfg_overrides)
        with pytest.raises(L.ConvertError, match=needle):
            L.convert(adapter, base_config, tmp_path / "out.gguf")

    A, B = lora_pair(8, 8)
    expect_error({"base_model.model.model.language_model.embed_tokens.lora_A.weight": A,
                  "base_model.model.model.language_model.embed_tokens.lora_B.weight": B}, {}, "embedding")
    expect_error({"base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
                  "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B},
                 {"use_dora": True}, "DoRA")
    expect_error({"base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
                  "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B},
                 {"modules_to_save": ["lm_head"]}, "modules_to_save")
    expect_error({"base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
                  "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B},
                 {"bias": "all"}, "bias")
    expect_error({"base_model.model.model.language_model.layers.0.not_a_real_module.lora_A.weight": A,
                  "base_model.model.model.language_model.layers.0.not_a_real_module.lora_B.weight": B},
                 {}, "not_a_real_module")
    # a bare lora_embedding key (PEFT's real name for an embedding adapter) has no ".weight" suffix and is caught
    # by the generic "not a plain Linear" branch, not a dedicated embedding check
    expect_error({"base_model.model.model.language_model.embed_tokens.lora_embedding_A": A},
                 {}, "not a plain Linear")


# ------------------------------------------------------------------ T5: round trip through the GGUF reader
def test_t5_round_trip(tmp_path):
    base_config = write_base_config(tmp_path)
    A, B = lora_pair(8, 12)
    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B,
    })
    out = tmp_path / "out.gguf"
    report = L.convert(adapter, base_config, out)
    assert report["tensors"]["projections"] == 1

    g = GGUFFile(out)
    assert g.metadata["general.type"] == "adapter"
    assert g.metadata["general.architecture"] == "qwen4exp"
    assert g.metadata["adapter.type"] == "lora"
    by_name = {t.name: t for t in g.tensors}
    assert by_name["blk.0.attn_q.weight.lora_a"].shape == list(reversed(A.shape))
    assert by_name["blk.0.attn_q.weight.lora_b"].shape == list(reversed(B.shape))


# ------------------------------------------------------------------ T6: a zero B gives a null adapter, for real
def test_t6_zero_b_gives_a_null_adapter_in_the_file(tmp_path):
    base_config = write_base_config(tmp_path)
    A, B = lora_pair(16, 12)
    B0 = np.zeros_like(B)
    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B0,
    })
    out = tmp_path / "out.gguf"
    report = L.convert(adapter, base_config, out)
    assert all(v == 0.0 for v in report["energy_share"].values())

    tensors, _ = read_back(out)
    assert np.all(tensors["blk.0.attn_q.weight.lora_b"] == 0)


# ------------------------------------------------------------------ B1 + B2: real parity through convert() + the
# written file, for every "how" and for alpha_pattern's actual (suffix-matching) PEFT rule
_MODULES = {
    # suffix -> (n_out, n_in, r, how, gguf_name)
    "self_attn.q_proj": (32, 20, 4, "id", "attn_q.weight"),
    "linear_attn.in_proj_qkv": (S.GDN_QK_ROWS + S.GDN_V_HEADS * S.GDN_HEAD, 20, 8, "vrows", "attn_qkv.weight"),
    "linear_attn.in_proj_z": (S.GDN_V_HEADS * S.GDN_HEAD, 20, 6, "zrows", "attn_gate.weight"),
    "linear_attn.in_proj_a": (S.GDN_V_HEADS, 20, 5, "heads", "ssm_alpha.weight"),
    "linear_attn.out_proj": (24, S.GDN_V_HEADS * S.GDN_HEAD, 3, "vcols", "ssm_out.weight"),
    "mlp.gate": (6, 20, 7, "id", "ffn_gate_inp.weight"),  # router: one row per expert, not a single row
}


def test_r3_modules_table_matches_checkpoint():
    """_MODULES is written by hand; this ties it to the real table instead of letting it drift from it."""
    for suffix, (_n_out, _n_in, _r, how, gguf_name) in _MODULES.items():
        rule = CK.LAYER[suffix + ".weight"]
        assert rule[0] == gguf_name, suffix
        assert rule[2] == how, suffix


def test_b2_real_parity_every_how_and_alpha_pattern(tmp_path):
    base_config = write_base_config(tmp_path)
    global_alpha = 16.0
    # B1: a short key ("q_proj") and a longer, partial key ("layers.0.mlp.gate") must both match by PEFT's
    # suffix rule, not by exact module-path equality
    alpha_pattern = {"q_proj": 40.0, "layers.0.mlp.gate": 24.0}

    hf_weights, tensors, expected_scale = {}, {}, {}
    for suffix, (n_out, n_in, r, how, _gguf_name) in _MODULES.items():
        A, B = lora_pair(n_out, n_in, r)
        hf_weights[suffix] = RNG.standard_normal((n_out, n_in)).astype(np.float32)
        tensors[f"base_model.model.model.language_model.layers.0.{suffix}.lora_A.weight"] = A
        tensors[f"base_model.model.model.language_model.layers.0.{suffix}.lora_B.weight"] = B
        alpha = alpha_pattern.get("q_proj") if suffix == "self_attn.q_proj" else \
            alpha_pattern.get("layers.0.mlp.gate") if suffix == "mlp.gate" else global_alpha
        expected_scale[suffix] = (A, B, alpha / r, how)

    adapter = write_adapter(tmp_path, tensors, lora_alpha=global_alpha, alpha_pattern=alpha_pattern)
    out = tmp_path / "out.gguf"
    L.convert(adapter, base_config, out)
    written, g = read_back(out)
    file_alpha = g.metadata["adapter.lora.alpha"]

    for suffix, (n_out, n_in, r, how, gguf_name) in _MODULES.items():
        A, B, scale, _ = expected_scale[suffix]
        W = hf_weights[suffix]
        expected = checkpoint_transform(how, W + scale * B @ A)

        A_file = written[f"blk.0.{gguf_name}.lora_a"]
        B_file = written[f"blk.0.{gguf_name}.lora_b"]
        r_file = A_file.shape[0]
        assert r_file == r
        got = checkpoint_transform(how, W) + (file_alpha / r_file) * B_file @ A_file
        np.testing.assert_allclose(expected, got, atol=1e-4, rtol=1e-4, err_msg=suffix)


def test_b2_real_parity_indexer_split(tmp_path):
    base_config = write_base_config(tmp_path)  # indexer_n_heads=4, indexer_head_dim=16 -> qk_rows=64
    qk_rows, k_rows, n_in, r = 64, 20, 24, 5
    n_out = qk_rows + k_rows
    A, B = lora_pair(n_out, n_in, r)
    W = RNG.standard_normal((n_out, n_in)).astype(np.float32)
    lora_alpha = 16.0
    scale = lora_alpha / r

    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.indexer.index_qk_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.indexer.index_qk_proj.lora_B.weight": B,
    }, lora_alpha=lora_alpha)
    out = tmp_path / "out.gguf"
    report = L.convert(adapter, base_config, out)
    assert report["tensors"]["other"] == 2
    written, g = read_back(out)
    file_alpha = g.metadata["adapter.lora.alpha"]

    full_expected = W + scale * B @ A
    for name, lo, hi in [("blk.0.indexer.q_proj.weight", 0, qk_rows), ("blk.0.indexer.k_proj.weight", qk_rows, n_out)]:
        A_file, B_file = written[f"{name}.lora_a"], written[f"{name}.lora_b"]
        r_file = A_file.shape[0]
        got = W[lo:hi] + (file_alpha / r_file) * B_file @ A_file
        np.testing.assert_allclose(full_expected[lo:hi], got, atol=1e-4, rtol=1e-4, err_msg=name)


def test_b1_rslora_denominator(tmp_path):
    base_config = write_base_config(tmp_path)
    n_out, n_in, r, lora_alpha = 32, 20, 9, 16.0
    A, B = lora_pair(n_out, n_in, r)
    W = RNG.standard_normal((n_out, n_in)).astype(np.float32)
    scale = lora_alpha / np.sqrt(r)  # rslora: alpha / sqrt(r), not alpha / r

    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B,
    }, lora_alpha=lora_alpha, use_rslora=True)
    out = tmp_path / "out.gguf"
    L.convert(adapter, base_config, out)
    written, g = read_back(out)
    file_alpha = g.metadata["adapter.lora.alpha"]

    A_file, B_file = written["blk.0.attn_q.weight.lora_a"], written["blk.0.attn_q.weight.lora_b"]
    expected = W + scale * B @ A
    got = W + (file_alpha / A_file.shape[0]) * B_file @ A_file
    np.testing.assert_allclose(expected, got, atol=1e-4, rtol=1e-4)


# ------------------------------------------------------------------ end-to-end: projections + router + split + experts
def test_full_adapter_with_experts_router_split(tmp_path):
    base_config = write_base_config(tmp_path)  # indexer_n_heads=4, indexer_head_dim=16 -> qk_rows=64
    Aq, Bq = lora_pair(32, 16)      # self_attn.q_proj: "id"
    n_expert_pretend = 6
    Ar, Br = lora_pair(n_expert_pretend, 16)  # mlp.gate (router): "id", one row per expert, not a single row
    Aidx, Bidx = lora_pair(64 + 20, 16)  # self_attn.indexer.index_qk_proj: "split"
    Ae, Be = lora_pair(8, 16)       # mlp.experts.3.gate_proj: "id"
    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": Aq,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": Bq,
        "base_model.model.model.language_model.layers.0.mlp.gate.lora_A.weight": Ar,
        "base_model.model.model.language_model.layers.0.mlp.gate.lora_B.weight": Br,
        "base_model.model.model.language_model.layers.0.self_attn.indexer.index_qk_proj.lora_A.weight": Aidx,
        "base_model.model.model.language_model.layers.0.self_attn.indexer.index_qk_proj.lora_B.weight": Bidx,
        "base_model.model.model.language_model.layers.0.mlp.experts.3.gate_proj.lora_A.weight": Ae,
        "base_model.model.model.language_model.layers.0.mlp.experts.3.gate_proj.lora_B.weight": Be,
    })
    report = L.convert(adapter, base_config, tmp_path / "out.gguf")
    assert report["tensors"] == {"projections": 1, "router": 1, "experts": 1, "other": 2}
    shares = report["energy_share"]
    assert shares["projections"] > 0 and shares["router"] > 0 and shares["experts"] > 0 and shares["other"] > 0
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6)

    g = GGUFFile(tmp_path / "out.gguf")
    names = {t.name for t in g.tensors}
    assert "blk.0.indexer.q_proj.weight.lora_a" in names
    assert "blk.0.indexer.k_proj.weight.lora_a" in names
    assert "blk.0.ffn_gate_exps.3.weight.lora_a" in names
    assert "blk.0.ffn_gate_inp.weight.lora_a" in names  # mlp.gate -> ffn_gate_inp.weight


# ------------------------------------------------------------------ N1: the base config's real, nested shape
def test_n1_text_config_unwrap_gives_the_same_file(tmp_path):
    A, B = lora_pair(32, 16, 4)
    tensors = {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B,
    }
    adapter = write_adapter(tmp_path, tensors)

    flat_config = write_base_config(tmp_path, path="flat.json")
    nested_config = write_base_config_nested(tmp_path, path="nested.json")
    out_flat, out_nested = tmp_path / "flat.gguf", tmp_path / "nested.gguf"
    L.convert(adapter, flat_config, out_flat)
    L.convert(adapter, nested_config, out_nested)

    assert out_flat.read_bytes() == out_nested.read_bytes()


def test_n1_missing_field_gives_convert_error(tmp_path):
    A, B = lora_pair(32, 16, 4)
    adapter = write_adapter(tmp_path, {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B,
    })
    cfg = _base_fields()
    del cfg["indexer_n_heads"]
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(L.ConvertError, match="indexer_n_heads"):
        L.convert(adapter, tmp_path / "config.json", tmp_path / "out.gguf")


# ------------------------------------------------------------------ R2: F16/BF16 adapters give the same file as F32
def test_r2_f16_and_bf16_give_the_same_file_as_f32(tmp_path):
    base_config = write_base_config(tmp_path)
    # small integers: exactly representable in F32, F16 and BF16 alike, so the three files must be identical
    A = RNG.integers(-4, 5, size=(4, 12)).astype(np.float32)
    B = RNG.integers(-4, 5, size=(16, 4)).astype(np.float32)
    tensors = {
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.language_model.layers.0.self_attn.q_proj.lora_B.weight": B,
    }
    outs = {}
    for dtype in ("F32", "F16", "BF16"):
        adapter = write_adapter(tmp_path / dtype, tensors, dtype=dtype)
        out = tmp_path / f"out_{dtype}.gguf"
        L.convert(adapter, base_config, out)
        outs[dtype] = out.read_bytes()
    assert outs["F32"] == outs["F16"] == outs["BF16"]


# ------------------------------------------------------------------ the committed sample: closes the loop with lora_merge_test
SAMPLE_DIR = pathlib.Path(__file__).resolve().parent / "testdata" / "lora_sample"
SAMPLE_EXPECTED = pathlib.Path(__file__).resolve().parent / "testdata" / "lora_sample_expected.gguf"


def test_committed_sample_reproduces_byte_for_byte(tmp_path):
    """The committed fixture (testdata/lora_sample/, a fixed PEFT folder with one native tensor and one router
    tensor) converts to exactly the committed testdata/lora_sample_expected.gguf -- the file lora_merge_test
    opens with the C++ reader. If this test ever fails, lora_merge_test's own numbers are checked against a
    sample lora.py no longer produces, so both must be regenerated together."""
    out = tmp_path / "sample.gguf"
    L.convert(SAMPLE_DIR, SAMPLE_DIR / "base_config.json", out)
    assert out.read_bytes() == SAMPLE_EXPECTED.read_bytes()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
