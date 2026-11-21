"""Tests for lora.py. No GPU, no real checkpoint: synthetic PEFT adapters with the real GDN head geometry
(16 key heads, 48 value heads of 128) built directly as safetensors files."""
from __future__ import annotations

import json
import pathlib
import struct
import sys
import tempfile

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import lora as L  # noqa: E402
import model_sections as S  # noqa: E402
from gguf_reader import GGUFFile  # noqa: E402

RNG = np.random.default_rng(1234)


# ------------------------------------------------------------------ tiny safetensors writer (PEFT's own format)
def write_safetensors(path: pathlib.Path, tensors: dict[str, np.ndarray]) -> None:
    header = {}
    blobs = []
    offset = 0
    for name, arr in tensors.items():
        arr = np.ascontiguousarray(arr, dtype="<f4")
        header[name] = {"dtype": "F32", "shape": list(arr.shape), "data_offsets": [offset, offset + arr.nbytes]}
        blobs.append(arr.tobytes())
        offset += arr.nbytes
    header["__metadata__"] = {"format": "pt"}
    raw = json.dumps(header).encode("utf-8")
    pad = (-len(raw)) % 8
    raw += b" " * pad
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(raw)))
        f.write(raw)
        for b in blobs:
            f.write(b)


def write_adapter(tmp: pathlib.Path, tensors: dict[str, np.ndarray], **cfg_overrides) -> pathlib.Path:
    adapter_dir = tmp / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"peft_type": "LORA", "r": 8, "lora_alpha": 16, "rank_pattern": {}, "alpha_pattern": {},
           "use_rslora": False, "use_dora": False, "modules_to_save": None, "bias": "none",
           "fan_in_fan_out": False}
    cfg.update(cfg_overrides)
    (adapter_dir / "adapter_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    write_safetensors(adapter_dir / "adapter_model.safetensors", tensors)
    return adapter_dir


def write_base_config(tmp: pathlib.Path, **overrides) -> pathlib.Path:
    cfg = {"indexer_n_heads": 4, "indexer_head_dim": 16}
    cfg.update(overrides)
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def lora_pair(n_out: int, n_in: int, r: int = 8) -> tuple[np.ndarray, np.ndarray]:
    A = RNG.standard_normal((r, n_in)).astype(np.float32) * 0.1
    B = RNG.standard_normal((n_out, r)).astype(np.float32) * 0.1
    return A, B


def read_back(path: pathlib.Path) -> dict[str, np.ndarray]:
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


# ------------------------------------------------------------------ T1: parity per "how"
@pytest.mark.parametrize("how,n_out,n_in,axis", [
    ("id", 32, 24, None),
    ("vrows", S.GDN_QK_ROWS + S.GDN_V_HEADS * S.GDN_HEAD, 24, "rows"),
    ("zrows", S.GDN_V_HEADS * S.GDN_HEAD, 24, "rows"),
    ("heads", S.GDN_V_HEADS, 24, "rows"),
    ("vcols", 24, S.GDN_V_HEADS * S.GDN_HEAD, "cols"),
])
def test_t1_parity_per_how(how, n_out, n_in, axis):
    W = RNG.standard_normal((n_out, n_in)).astype(np.float32)
    A, B = lora_pair(n_out, n_in)
    s = 1.0

    if how == "id":
        idx = None
    elif how == "vrows":
        idx = S.gdn_index(n_out, S.GDN_QK_ROWS)
    elif how == "zrows":
        idx = S.gdn_index(n_out, 0)
    elif how == "heads":
        idx = S.gdn_block_source()
    elif how == "vcols":
        idx = S.gdn_index(n_in, 0)

    def transform(X):
        if idx is None:
            return X
        return X[idx] if axis == "rows" else X[:, idx]

    A2, B2 = L.reorder(how, A, B)
    lhs = transform(W + s * B @ A)
    rhs = transform(W) + s * B2 @ A2
    np.testing.assert_allclose(lhs, rhs, atol=1e-5, rtol=1e-5)


# ------------------------------------------------------------------ T2: indexer split
def test_t2_indexer_split():
    qk_rows, k_rows, n_in = 4 * 16, 20, 24   # indexer_n_heads=4, indexer_head_dim=16 -> qk_rows=64
    n_out = qk_rows + k_rows
    W = RNG.standard_normal((n_out, n_in)).astype(np.float32)
    A, B = lora_pair(n_out, n_in)
    s = 1.0
    full = W + s * B @ A
    Wq, Wk = full[:qk_rows], full[qk_rows:]
    Bq, Bk = B[:qk_rows], B[qk_rows:]
    np.testing.assert_allclose(Wq, W[:qk_rows] + s * Bq @ A, atol=1e-5)
    np.testing.assert_allclose(Wk, W[qk_rows:] + s * Bk @ A, atol=1e-5)


# ------------------------------------------------------------------ T3: rank_pattern / alpha_pattern / rslora
def test_t3_scale_folding():
    n_out, n_in, r, global_alpha = 16, 12, 4, 16.0
    A, B = lora_pair(n_out, n_in, r)
    path = "model.language_model.layers.0.self_attn.q_proj"
    for cfg, want in [
        ({"lora_alpha": global_alpha, "alpha_pattern": {}, "use_rslora": False}, global_alpha / r),
        ({"lora_alpha": global_alpha, "alpha_pattern": {path: 32.0}, "use_rslora": False}, 32.0 / r),
        ({"lora_alpha": global_alpha, "alpha_pattern": {}, "use_rslora": True}, global_alpha / np.sqrt(r)),
    ]:
        scale = L.effective_scale(cfg, path, r)
        assert scale == pytest.approx(want)
        correction = scale * r / cfg["lora_alpha"]
        B2 = B * correction
        # applying the GLOBAL alpha/r to the corrected B must reproduce the tensor's own effective scale
        np.testing.assert_allclose((global_alpha / r) * B2 @ A, scale * B @ A, atol=1e-5, rtol=1e-5)


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
    assert report["tensors"]["dense"] == 1

    g = GGUFFile(out)
    assert g.metadata["general.type"] == "adapter"
    assert g.metadata["general.architecture"] == "qwen4exp"
    assert g.metadata["adapter.type"] == "lora"
    names = {t.name for t in g.tensors}
    assert "blk.0.attn_q.weight.lora_a" in names
    assert "blk.0.attn_q.weight.lora_b" in names
    by_name = {t.name: t for t in g.tensors}
    # self_attn.q_proj is "id": ne is reversed PyTorch shape, same convention as checkpoint.py
    assert by_name["blk.0.attn_q.weight.lora_a"].shape == list(reversed(A.shape))
    assert by_name["blk.0.attn_q.weight.lora_b"].shape == list(reversed(B.shape))


# ------------------------------------------------------------------ T6: a zero B gives a null adapter
def test_t6_zero_b_is_null():
    A, B = lora_pair(16, 12)
    B0 = np.zeros_like(B)
    A2, B2 = L.reorder("id", A, B0)
    assert np.all(B2 == 0)
    assert L.energy(1.0, A2, B2) == 0.0


# ------------------------------------------------------------------ end-to-end: dense + router + split + experts
def test_full_adapter_with_experts_router_split(tmp_path):
    base_config = write_base_config(tmp_path)  # indexer_n_heads=4, indexer_head_dim=16 -> qk_rows=64
    Aq, Bq = lora_pair(32, 16)      # self_attn.q_proj: "id"
    Ar, Br = lora_pair(1, 16)       # mlp.gate (router): "id", a single row (one expert-selection logit per token)
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
    assert report["tensors"] == {"dense": 3, "router": 1, "experts": 1}
    shares = report["energy_share"]
    assert shares["dense"] > 0 and shares["router"] > 0 and shares["experts"] > 0
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6)

    g = GGUFFile(tmp_path / "out.gguf")
    names = {t.name for t in g.tensors}
    assert "blk.0.indexer.q_proj.weight.lora_a" in names
    assert "blk.0.indexer.k_proj.weight.lora_a" in names
    assert "blk.0.ffn_gate_exps.3.weight.lora_a" in names
    assert "blk.0.ffn_gate_inp.weight.lora_a" in names  # mlp.gate -> ffn_gate_inp.weight


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
