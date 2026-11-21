"""Convert a PEFT LoRA adapter for the qwen4exp checkpoint into a llama.cpp-style GGUF adapter file.

    python3 tools/convert/lora.py --adapter DIR --base-config CONFIG.json --out ADAPTER.gguf [--force]

Input: a PEFT adapter directory (adapter_config.json + adapter_model.safetensors), trained on the HF checkpoint
whose module paths match checkpoint.py's LAYER/GLOBAL/INDEXER_QK tables. Output: one GGUF file, llama.cpp's
LoRA-adapter convention (general.type=adapter, adapter.type=lora, "<base tensor>.lora_a"/".lora_b"), with each
covered tensor given the same row/column reorder checkpoint.py gives the base weight (GDN value-head order, the
indexer's fused projection split into q/k). "<name>.lora_a" has ne = [n_in, r], "<name>.lora_b" has ne = [r,
n_out] — the same reversed-shape convention checkpoint.py uses for the base tensors. A reader applies, per
tensor, scale = adapter.lora.alpha / (rows of lora_a) * the caller's own scale: a single adapter.lora.alpha
metadata value is kept valid for every tensor by folding any rank_pattern/alpha_pattern/rslora difference into B.

The routed experts (mlp.experts.E.{gate,up,down}_proj) are converted too; whether the engine applies them is the
engine's decision, not this file's. Each expert tensor becomes its own small adapter pair
("blk.N.ffn_{gate,up,down}_exps.E.lora_a/b", a name of this converter's own choosing: llama.cpp has no
per-expert equivalent). The file's metadata records what share of the update's energy (the squared Frobenius
norm of scale*B*A, summed per group so the four shares add up to 1) sits in the ten projection families
`eligible()` in src/core/native_dense.cpp loads as native matrices, the router, the experts, and everything else
(hyper-connection, the indexer, the GDN alpha/beta gates, the output head) — so an adapter that reaches outside
that list carries that fact with it.

Unsupported PEFT features (DoRA, modules_to_save, a bias other than "none", LoRA on an embedding or a conv
module) raise ConvertError naming the thing this converter does not handle, the same way the checkpoint
converter refuses an incomplete checkpoint.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))   # for gguf_writer, gguf_reader
import checkpoint as CK  # noqa: E402
import model_sections as S  # noqa: E402
from gguf_writer import GGUFWriter  # noqa: E402

ConvertError = S.ConvertError

LORA_KEY = re.compile(r"^base_model\.model\.(?P<path>.+)\.lora_(?P<ab>[AB])(?:\.(?P<adapter>[^.]+))?\.weight$")
LAYER_RE = re.compile(r"model\.language_model\.layers\.(\d+)\.(.+)")
EXPERT_RE = re.compile(r"mlp\.experts\.(\d+)\.(.+)")
EXPERT_ROLE_TO_GGUF = {"gate_proj": "gate", "up_proj": "up", "down_proj": "down"}
ROUTER_SUFFIX = "mlp.gate.weight"
# the suffixes eligible() in src/core/native_dense.cpp loads as native matrices; keep the two lists equal
PROJECTION_GGUF_NAMES = {
    "attn_qkv.weight", "attn_gate.weight", "ssm_out.weight",
    "attn_q.weight", "attn_k.weight", "attn_v.weight", "attn_output.weight",
    "ffn_gate_shexp.weight", "ffn_up_shexp.weight", "ffn_down_shexp.weight",
}
GROUPS = ("projections", "router", "experts", "other")


def _group_for(suffix_key: str, gguf_name: str) -> str:
    if suffix_key == ROUTER_SUFFIX:
        return "router"
    return "projections" if gguf_name in PROJECTION_GGUF_NAMES else "other"


# ------------------------------------------------------------------ reading the PEFT adapter directory
def load_adapter_config(path: pathlib.Path) -> dict:
    cfg = json.loads((path / "adapter_config.json").read_text(encoding="utf-8"))
    if cfg.get("peft_type", "LORA").upper() != "LORA":
        raise ConvertError(f"adapter_config.json: peft_type {cfg.get('peft_type')!r} is not LoRA")
    if cfg.get("use_dora"):
        raise ConvertError("use_dora: DoRA adapters are not plain LoRA, not handled by this converter")
    if cfg.get("modules_to_save"):
        raise ConvertError(f"modules_to_save is not empty ({cfg['modules_to_save']!r}): not handled")
    if cfg.get("bias", "none") != "none":
        raise ConvertError(f"bias {cfg['bias']!r} is not \"none\": LoRA bias terms are not handled")
    if cfg.get("fan_in_fan_out"):
        raise ConvertError("fan_in_fan_out is set: not handled (expected for a Linear-only recipe)")
    if "lora_alpha" not in cfg:
        raise ConvertError("adapter_config.json has no lora_alpha")
    return cfg


def _dtype_to_f32(raw: bytes, dtype: str, shape: list[int]) -> np.ndarray:
    n = int(np.prod(shape)) if shape else 1
    if dtype == "F32":
        x = np.frombuffer(raw, dtype="<f4", count=n)
    elif dtype == "F16":
        x = np.frombuffer(raw, dtype="<f2", count=n).astype(np.float32)
    elif dtype == "BF16":
        u = np.frombuffer(raw, dtype="<u2", count=n)
        x = S._bf16_to_f32(u)
    else:
        raise ConvertError(f"adapter tensor has dtype {dtype}, expected F32/F16/BF16")
    return np.ascontiguousarray(x.reshape(shape), dtype=np.float32)


class AdapterTensors:
    """The lora_A/lora_B pairs of a PEFT adapter directory, as float32 numpy arrays in PEFT's own (r, n_in) /
    (n_out, r) shape, keyed by module path (e.g. "model.language_model.layers.3.self_attn.q_proj")."""

    def __init__(self, adapter_dir: pathlib.Path):
        path = adapter_dir / "adapter_model.safetensors"
        if not path.is_file():
            raise ConvertError(f"{path}: not found")
        head, base = S.read_safetensors_header(path)
        pairs: dict[str, dict[str, np.ndarray]] = {}
        adapter_names = set()
        with open(path, "rb") as f:
            for key, info in head.items():
                if key == "__metadata__":
                    continue
                m = LORA_KEY.match(key)
                if m is None:
                    raise ConvertError(f"{key}: not a plain Linear lora_A/lora_B tensor, not handled")
                if m.group("adapter"):
                    adapter_names.add(m.group("adapter"))
                a, b = info["data_offsets"]
                f.seek(base + a)
                arr = _dtype_to_f32(f.read(b - a), info["dtype"], info["shape"])
                pairs.setdefault(m.group("path"), {})[m.group("ab")] = arr
        if len(adapter_names) > 1:
            raise ConvertError(f"adapter_model.safetensors holds more than one adapter name: {sorted(adapter_names)}")
        missing = [p for p, d in pairs.items() if "A" not in d or "B" not in d]
        if missing:
            raise ConvertError(f"{missing[0]}: has a lora_A or a lora_B but not both")
        self.pairs = pairs  # path -> {"A": (r, n_in) f32, "B": (n_out, r) f32}


# ------------------------------------------------------------------ module path -> GGUF target(s)
class Target:
    """One piece of work: GGUF name(s) this module's A/B pair becomes, with the transform to get there."""

    def __init__(self, kind: str, names: list[tuple[str, str]], how: str, rows: tuple[int, int] | None = None):
        self.kind, self.names, self.how, self.rows = kind, names, how, rows
        # names: [(gguf_name, group)]; group is one of GROUPS


def classify(module_path: str, qk_rows: int) -> Target:
    """module_path has no ".weight" and no "base_model.model." prefix (e.g.
    "model.language_model.layers.3.self_attn.q_proj")."""
    key = module_path + ".weight"
    m = LAYER_RE.fullmatch(module_path)
    if m is None:
        rule = CK.GLOBAL.get(key)
        if rule is None:
            raise ConvertError(f"{module_path}: not a recognised module (no layer number and not a global tensor)")
        if "embed_tokens" in module_path:
            raise ConvertError(f"{module_path}: LoRA on the token embedding is not handled")
        return Target("dense", [(rule[0], _group_for(key, rule[0]))], rule[2])

    layer, suffix = m.group(1), m.group(2)
    em = EXPERT_RE.fullmatch(suffix)
    if em is not None:
        expert_idx, role = em.group(1), em.group(2)
        gguf_role = EXPERT_ROLE_TO_GGUF.get(role)
        if gguf_role is None:
            raise ConvertError(f"{module_path}: unknown expert role {role!r}")
        return Target("expert", [(f"blk.{layer}.ffn_{gguf_role}_exps.{expert_idx}.weight", "experts")], "id")

    suffix_key = suffix + ".weight"
    if suffix_key == CK.INDEXER_QK:
        return Target("split",
                       [(f"blk.{layer}.indexer.q_proj.weight", "other"), (f"blk.{layer}.indexer.k_proj.weight", "other")],
                       "split", rows=(0, qk_rows))

    rule = CK.LAYER.get(suffix_key)
    if rule is None:
        raise ConvertError(f"{module_path}: not a recognised per-layer module")
    group = _group_for(suffix_key, rule[0])
    return Target("dense", [(f"blk.{layer}.{rule[0]}", group)], rule[2])


# ------------------------------------------------------------------ the GDN/indexer reorder, mirroring checkpoint.py
def reorder(how: str, A: np.ndarray, B: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A is (r, n_in), B is (n_out, r), both in the checkpoint's own row/column order. Returns them reordered so
    that B's rows and A's columns read in the GGUF value-head order checkpoint.py gives the base weight."""
    if how == "id":
        return A, B
    if how == "vrows":
        return A, B[S.gdn_index(B.shape[0], S.GDN_QK_ROWS)]
    if how == "zrows":
        return A, B[S.gdn_index(B.shape[0], 0)]
    if how == "heads":
        return A, B[S.gdn_block_source()]
    if how == "vcols":
        return A[:, S.gdn_index(A.shape[1], 0)], B
    raise ConvertError(f"a module with transform {how!r} is not handled by this converter")


# ------------------------------------------------------------------ scale folding (rank_pattern/alpha_pattern/rslora)
def _pattern_lookup(pattern: dict, module_path: str, default):
    """PEFT's own rule: a pattern key matches a module whose path ends with it, preceded by nothing or by
    "<something>.". The first matching key (in the dict's own order) wins."""
    for key, value in pattern.items():
        if re.match(rf"(.*\.)?({key})$", module_path):
            return value
    return default


def effective_scale(cfg: dict, module_path: str, r: int) -> float:
    alpha = _pattern_lookup(cfg.get("alpha_pattern") or {}, module_path, cfg["lora_alpha"])
    denom = np.sqrt(r) if cfg.get("use_rslora") else r
    return float(alpha) / float(denom)


# ------------------------------------------------------------------ energy (the squared Frobenius norm of scale*B*A)
def energy_sq(scale: float, A: np.ndarray, B: np.ndarray) -> float:
    """scale**2 * ||B @ A||_F**2, without ever forming the n_out x n_in product: ||B@A||_F**2 =
    sum((B^T @ B) * (A @ A^T)), two r x r matrices, computed in float64."""
    bt_b = B.astype(np.float64).T @ B.astype(np.float64)
    a_at = A.astype(np.float64) @ A.astype(np.float64).T
    return float(scale) ** 2 * float(np.sum(bt_b * a_at))


# ------------------------------------------------------------------ writing
def _add_tensor(writer: GGUFWriter, name: str, arr: np.ndarray) -> None:
    """arr is in checkpoint/PyTorch row-major order (rows, cols); the GGUF shape is declared reversed, with the
    bytes written unchanged — the same convention checkpoint.py uses for `ne`."""
    a = np.ascontiguousarray(arr, dtype="<f4")
    writer.add_raw(name, list(reversed(a.shape)), "F32", a.tobytes())


def _req_int(cfg: dict, key: str) -> int:
    if key not in cfg:
        raise ConvertError(f"{key}: missing from the base model's config.json")
    return int(cfg[key])


def _check_gdn_geometry(base: dict) -> None:
    """Same check as checkpoint.py's CheckpointDense.__init__: this converter uses the same fixed GDN constants,
    so a base model with a different head geometry would silently misplace rows."""
    if _req_int(base, "linear_num_key_heads") != S.GDN_K_HEADS or _req_int(base, "linear_num_value_heads") != S.GDN_V_HEADS \
            or _req_int(base, "linear_key_head_dim") != S.GDN_HEAD or _req_int(base, "linear_value_head_dim") != S.GDN_HEAD:
        raise ConvertError("the GDN head geometry differs from the GGUF head order this converter knows")


def convert(adapter_dir: pathlib.Path, base_config: pathlib.Path, out: pathlib.Path) -> dict:
    cfg = load_adapter_config(adapter_dir)
    base = json.loads(base_config.read_text(encoding="utf-8"))
    base = base.get("text_config", base)  # the model also has a visual config; same unwrap as checkpoint.py
    _check_gdn_geometry(base)
    qk_rows = _req_int(base, "indexer_n_heads") * _req_int(base, "indexer_head_dim")
    tensors = AdapterTensors(adapter_dir)

    writer = GGUFWriter()
    writer.add("general.type", "adapter")
    writer.add("general.architecture", "qwen4exp")
    writer.add("adapter.type", "lora")
    writer.add("adapter.lora.alpha", float(cfg["lora_alpha"]), "f32")

    group_energy = {g: 0.0 for g in GROUPS}
    n_tensors = {g: 0 for g in GROUPS}
    ranks: set[int] = set()

    for module_path, ab in sorted(tensors.pairs.items()):
        A, B = ab["A"], ab["B"]
        r = A.shape[0]
        if B.shape[1] != r:
            raise ConvertError(f"{module_path}: lora_A rank {r} does not match lora_B rank {B.shape[1]}")
        ranks.add(r)
        target = classify(module_path, qk_rows)
        scale = effective_scale(cfg, module_path, r)
        correction = scale * r / float(cfg["lora_alpha"])
        B_corrected = B * correction  # fold rank_pattern/alpha_pattern/rslora so one global alpha works everywhere

        if target.kind == "split":
            lo, hi = target.rows
            if hi >= B.shape[0]:
                raise ConvertError(f"{module_path}: {B.shape[0]} output rows leaves nothing for indexer.k_proj "
                                   f"(qk_rows is {qk_rows})")
            # indexer.q_proj takes rows [0, qk_rows), indexer.k_proj takes the rest; A is shared, unchanged
            parts = [(target.names[0][0], target.names[0][1], B[lo:hi], B_corrected[lo:hi]),
                     (target.names[1][0], target.names[1][1], B[hi:], B_corrected[hi:])]
            for name, group, B_orig_part, B_corr_part in parts:
                _add_tensor(writer, name + ".lora_a", A)
                _add_tensor(writer, name + ".lora_b", B_corr_part)
                group_energy[group] += energy_sq(scale, A, B_orig_part)
                n_tensors[group] += 1
            continue

        A2, B2 = reorder(target.how, A, B_corrected)
        name, group = target.names[0]
        _add_tensor(writer, name + ".lora_a", A2)
        _add_tensor(writer, name + ".lora_b", B2)
        # the energy itself uses the UN-reordered A/B: a row/column permutation does not change a Frobenius norm
        group_energy[group] += energy_sq(scale, A, B)
        n_tensors[group] += 1

    # "energy" is the squared Frobenius norm (the usual signal-processing sense), so the shares sum to 1 exactly
    total = sum(group_energy.values())
    shares = {g: (group_energy[g] / total if total > 0 else 0.0) for g in GROUPS}
    for g in GROUPS:
        writer.add(f"adapter.basalt.energy_share.{g}", shares[g], "f32")
    writer.add("adapter.basalt.ranks", sorted(ranks), "array:i32")

    out.parent.mkdir(parents=True, exist_ok=True)
    writer.write(out)
    return {"tensors": n_tensors, "energy_share": shares, "ranks": sorted(ranks)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", required=True, type=pathlib.Path, help="PEFT adapter directory")
    ap.add_argument("--base-config", required=True, type=pathlib.Path, help="the base model's config.json")
    ap.add_argument("--out", required=True, type=pathlib.Path, help="output .gguf path")
    ap.add_argument("--force", action="store_true", help="overwrite --out")
    args = ap.parse_args(argv)

    if args.out.exists() and not args.force:
        print(f"{args.out}: already exists, pass --force to overwrite", file=sys.stderr)
        return 1
    try:
        report = convert(args.adapter, args.base_config, args.out)
    except ConvertError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"wrote {args.out}")
    print("  tensors: " + ", ".join(f"{g} {report['tensors'][g]}" for g in GROUPS))
    print(f"  ranks: {report['ranks']}")
    print("  energy share: " + ", ".join(f"{g} {report['energy_share'][g]:.1%}" for g in GROUPS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
