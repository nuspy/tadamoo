"""Generate the LoRA test adapters named in tests/lora_gpu_checks.md, from the real tensor shapes of real packs
-- never written by hand. Reuses lora.py's own convert() and test_lora.py's synthetic-PEFT-folder helpers, so an
adapter this script writes is exactly what tools/convert/lora.py would produce from a real PEFT checkpoint of the
same shapes.

    python3 tools/convert/gen_test_adapters.py --out-dir DIR --iq4xs-pack PATH --iq3s-pack PATH
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # for model_file, gguf_reader
import checkpoint as CK  # noqa: E402
import lora as L  # noqa: E402
import test_lora as T  # noqa: E402
from gguf_reader import GGML_TYPES  # noqa: E402
from model_file import ModelFile  # noqa: E402

RNG = np.random.default_rng(20261122)

# gguf suffix -> checkpoint module suffix (without ".weight"), inverted from CK.LAYER for the ten native families
GGUF_TO_CK_SUFFIX = {rule[0][:-len(".weight")]: key[:-len(".weight")] for key, rule in CK.LAYER.items()
                     if rule[0] in L.PROJECTION_GGUF_NAMES}
assert set(s + ".weight" for s in GGUF_TO_CK_SUFFIX) == L.PROJECTION_GGUF_NAMES, "GGUF_TO_CK_SUFFIX is incomplete"


def family_shape(pack: pathlib.Path, gguf_suffix: str, want_type: str | None = None) -> tuple[str, int, int, str]:
    """(tensor name, n_in, n_out, type) of the first (or, with want_type, the first matching) tensor of this
    family in `pack`'s meta section. For a single representative tensor only (P2b, P7, P8) -- not for coverage
    of a whole family, which needs every layer: see family_shapes_all. ne is [n_in, n_out] (GGUF order, reversed
    PyTorch shape)."""
    for name, n_in, n_out, type_name in family_shapes_all(pack, gguf_suffix):
        if want_type is None or type_name == want_type:
            return name, n_in, n_out, type_name
    raise SystemExit(f"{pack}: no tensor ends with {gguf_suffix!r}" + (f" of type {want_type}" if want_type else ""))


def family_shapes_all(pack: pathlib.Path, gguf_suffix: str) -> list[tuple[str, int, int, str]]:
    """Every tensor of this family, every layer: (name, n_in, n_out, type) each. A family is not one shape or one
    type -- two layers of the same family can differ (found on GSQ-RCO-IQ3_S: 47 of 48 ffn_down_shexp are IQ4_NL,
    one is Q8_0)."""
    meta = json.loads(ModelFile(str(pack)).read("meta"))
    out = []
    for name, t in meta["tensors"].items():
        if name.endswith(gguf_suffix):
            n_in, n_out = t["shape"]
            out.append((name, n_in, n_out, GGML_TYPES.get(t["type_id"], f"type{t['type_id']}")))
    if not out:
        raise SystemExit(f"{pack}: no tensor ends with {gguf_suffix!r}")
    return out


def layer_of(tensor_name: str) -> int:
    # "blk.N.<suffix>" -> N
    assert tensor_name.startswith("blk.")
    return int(tensor_name.split(".", 2)[1])


def module_path(layer: int, gguf_suffix: str) -> str:
    return f"model.language_model.layers.{layer}.{GGUF_TO_CK_SUFFIX[gguf_suffix]}"


def lora_pair(n_out: int, n_in: int, r: int, null: bool, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """scale 1.0: the delta this script used before it covered every layer (A sigma 0.05, B sigma 0.02, alpha/r
    2 -- per-rank-component norm close to a real tensor's spectral norm, fine for one or two tensors, too strong
    once every layer of a family carries its own independent one: use a smaller scale for full coverage."""
    A = RNG.standard_normal((r, n_in)).astype(np.float32) * (0.05 * scale)
    B = (np.zeros((n_out, r), dtype=np.float32) if null
         else RNG.standard_normal((n_out, r)).astype(np.float32) * (0.02 * scale))
    return A, B


def build(tmp: pathlib.Path, out: pathlib.Path, tensors: dict[str, tuple[np.ndarray, np.ndarray]], r: int = 8) -> dict:
    flat = {}
    for path, (A, B) in tensors.items():
        flat[f"base_model.model.{path}.lora_A.weight"] = A
        flat[f"base_model.model.{path}.lora_B.weight"] = B
    adapter_dir = T.write_adapter(tmp, flat, dtype="F32", r=r)
    base_config = T.write_base_config(tmp)
    result = L.convert(adapter_dir, base_config, out)
    print(f"{out.name}: {result['tensors']} tensors, energy share {result['energy_share']}")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, type=pathlib.Path)
    ap.add_argument("--iq4xs-pack", required=True, type=pathlib.Path,
                     help="the pack the identity/floor/speed tests run on (tests/lora_gpu_checks.md: MODEL.basalt)")
    ap.add_argument("--iq3s-pack", required=True, type=pathlib.Path,
                     help="the pack with a real Q8_0 native tensor, for P2b (tests/lora_gpu_checks.md)")
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    tmp = a.out_dir / "_scratch"

    ten = list(GGUF_TO_CK_SUFFIX)  # the ten native-matrix suffixes (no ".weight")
    shapes = {suf: family_shape(a.iq4xs_pack, suf + ".weight") for suf in ten}
    for suf, (name, n_in, n_out, type_name) in shapes.items():
        print(f"{suf}: {name} ne=[{n_in},{n_out}] {type_name}")

    def one_per_family(null: bool) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """One tensor per family (the first layer found), not full coverage -- for P7's mixed-types prove, where
        the point is one covered tensor beside uncovered ones in the same layer, not every layer."""
        out = {}
        for suf, (name, n_in, n_out, _) in shapes.items():
            out[module_path(layer_of(name), suf)] = lora_pair(n_out, n_in, 8, null)
        return out

    def all_layers(null: bool, scale: float) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Every tensor of every one of the ten families, every layer -- full coverage, for P6/P9/P4."""
        out = {}
        for suf in ten:
            for name, n_in, n_out, _ in family_shapes_all(a.iq4xs_pack, suf + ".weight"):
                out[module_path(layer_of(name), suf)] = lora_pair(n_out, n_in, 8, null, scale)
        return out

    full = all_layers(null=True, scale=1.0)
    print(f"full coverage: {len(full)} tensors")
    build(tmp, a.out_dir / "ADAPTER_NULL_FULL.gguf", full)
    # a delta two orders of magnitude smaller than the single-tensor adapters below: every layer of every family
    # now carries its own independent delta, so the single-tensor scale would be far too strong summed over all of them
    build(tmp, a.out_dir / "ADAPTER_FULL.gguf", all_layers(null=False, scale=0.01))

    # ADAPTER_PARTIAL_QV / _QKV: P7's mixed-types-in-one-layer prove -- one layer, not full coverage
    qv = {suf: shapes[suf] for suf in ("attn_q", "attn_v")}
    build(tmp, a.out_dir / "ADAPTER_PARTIAL_QV.gguf",
          {module_path(layer_of(n), s): lora_pair(no, ni, 8, False) for s, (n, ni, no, _) in qv.items()})
    qkv = {"attn_qkv": shapes["attn_qkv"]}
    build(tmp, a.out_dir / "ADAPTER_PARTIAL_QKV.gguf",
          {module_path(layer_of(n), s): lora_pair(no, ni, 8, False) for s, (n, ni, no, _) in qkv.items()})

    # ADAPTER_NULL_Q8TENSOR: P2b, the one real Q8_0 tensor on the GSQ-RCO-IQ3_S pack
    name, n_in, n_out, type_name = family_shape(a.iq3s_pack, "ffn_down_shexp.weight", want_type="Q8_0")
    print(f"P2b tensor: {name} ne=[{n_in},{n_out}] {type_name}")
    build(tmp, a.out_dir / "ADAPTER_NULL_Q8TENSOR.gguf",
          {module_path(layer_of(name), "ffn_down_shexp"): lora_pair(n_out, n_in, 8, null=True)})

    # ADAPTER_MIXED: P8, one expert tensor and one router tensor (outside the ten families) -- shapes don't need
    # to be real, this adapter is refused or partially fused, never merged into an actual kernel
    mixed = {
        "model.language_model.layers.0.mlp.experts.3.gate_proj": lora_pair(512, 256, 4, null=False),
        "model.language_model.layers.0.mlp.gate": lora_pair(128, 2560, 4, null=False),
    }
    build(tmp, a.out_dir / "ADAPTER_MIXED.gguf", mixed)

    print(f"\nwritten to {a.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
