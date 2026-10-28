"""A .basalt pack of the Qwen3.6-35B-A3B family (GGUF architecture qwen35moe) from NVIDIA Model Optimizer's NVFP4
safetensors checkpoint, for tools/convert/convert.py.

  experts / experts.layout  the routed experts' NVFP4 rows moved into Basalt's blocks (checkpoint.CheckpointExperts)
  gguf, dense, dense.index  every other language-model tensor under the qwen35moe GGUF names (include/basalt/qwen35/
                            layout.hpp): norms as F32 with the "+1" folded in, GDN value heads in the GGUF head order,
                            FP8 and NVFP4 matrices (dense projections, shared experts, output head) dequantized once
                            and stored as Q8_0, the token embedding Q8_0
  gguf_kv, tokenizer        the model metadata under `qwen35moe.*` and the tokenizer, from the checkpoint's files

There is no drafter, vision or expert-profile section. Needs numpy.
"""
from __future__ import annotations

import json
import pathlib
import re

import numpy as np

import checkpoint as C
import model_sections as S

FAMILY = "qwen35moe"
ARCH = "qwen35moe."
GDN_HEAD = 128
E2M1 = np.array([0, 0.5, 1, 1.5, 2, 3, 4, 6], np.float32)

# HF suffix under `model.language_model.layers.N.` -> (GGUF name, stored type, source form, transform).
# Source forms: bf16 as is, fp8 (E4M3 with a per-tensor weight_scale), fp4 (NVFP4 with block scales and a
# per-tensor weight_scale_2). Transforms are those of checkpoint.METHODS.
LAYER = {
    "input_layernorm.weight": ("attn_norm.weight", "F32", "bf16", "norm"),
    "post_attention_layernorm.weight": ("post_attention_norm.weight", "F32", "bf16", "norm"),
    "linear_attn.A_log": ("ssm_a", "F32", "bf16", "negexp"),
    "linear_attn.conv1d.weight": ("ssm_conv1d.weight", "F32", "bf16", "gconv"),
    "linear_attn.dt_bias": ("ssm_dt.bias", "F32", "bf16", "heads"),
    "linear_attn.in_proj_a.weight": ("ssm_alpha.weight", "BF16", "bf16", "heads"),
    "linear_attn.in_proj_b.weight": ("ssm_beta.weight", "BF16", "bf16", "heads"),
    "linear_attn.in_proj_qkv.weight": ("attn_qkv.weight", "Q8_0", "fp8", "vrows"),
    "linear_attn.in_proj_z.weight": ("attn_gate.weight", "Q8_0", "fp8", "zrows"),
    "linear_attn.norm.weight": ("ssm_norm.weight", "F32", "bf16", "id"),
    "linear_attn.out_proj.weight": ("ssm_out.weight", "Q8_0", "fp8", "vcols"),
    "mlp.gate.weight": ("ffn_gate_inp.weight", "BF16", "bf16", "id"),
    "mlp.shared_expert.down_proj.weight": ("ffn_down_shexp.weight", "Q8_0", "fp4", "id"),
    "mlp.shared_expert.gate_proj.weight": ("ffn_gate_shexp.weight", "Q8_0", "fp4", "id"),
    "mlp.shared_expert.up_proj.weight": ("ffn_up_shexp.weight", "Q8_0", "fp4", "id"),
    "mlp.shared_expert_gate.weight": ("ffn_gate_inp_shexp.weight", "BF16", "bf16", "id"),
    "self_attn.k_norm.weight": ("attn_k_norm.weight", "F32", "bf16", "norm"),
    "self_attn.q_norm.weight": ("attn_q_norm.weight", "F32", "bf16", "norm"),
    "self_attn.k_proj.weight": ("attn_k.weight", "Q8_0", "fp8", "id"),
    "self_attn.o_proj.weight": ("attn_output.weight", "Q8_0", "fp8", "id"),
    "self_attn.q_proj.weight": ("attn_q.weight", "Q8_0", "fp8", "id"),
    "self_attn.v_proj.weight": ("attn_v.weight", "Q8_0", "fp8", "id"),
}
GLOBAL = {
    "lm_head.weight": ("output.weight", "Q8_0", "fp4", "id"),
    "model.language_model.embed_tokens.weight": ("token_embd.weight", "Q8_0", "bf16", "id"),
    "model.language_model.norm.weight": ("output_norm.weight", "F32", "bf16", "norm"),
}
AUX = re.compile(r"\.(weight_scale|weight_scale_2|input_scale)$")
SKIP = re.compile(r"^(mtp\.|model\.visual\.|model\.language_model\.layers\.\d+\.mlp\.experts\.)")
LAYER_KEY = re.compile(r"model\.language_model\.layers\.(\d+)\.(.+)")


def gdn_order(v_heads: int, k_heads: int) -> np.ndarray:
    """The checkpoint's value head for each GGUF position: the GGUF lists (ratio r, key head k) as r * k_heads + k,
    the checkpoint as k * ratio + r."""
    j = np.arange(v_heads)
    return (j % k_heads) * (v_heads // k_heads) + j // k_heads


def gdn_index(n: int, first: int, v_heads: int, k_heads: int) -> np.ndarray:
    """Checkpoint index of each of n GGUF positions: identity below `first`, the value heads reordered above."""
    idx = np.arange(n)
    blocks = (n - first) // GDN_HEAD
    idx[first:] = first + (gdn_order(blocks, k_heads)[:, None] * GDN_HEAD + np.arange(GDN_HEAD)).reshape(-1)
    return idx


def fp4_values(codes: np.ndarray, scales: np.ndarray, scale2: float) -> np.ndarray:
    """Model Optimizer's NVFP4 rows -> float32 [rows, cols]: `codes` [rows, cols/2] (value 2i in the low nibble),
    `scales` [rows, cols/16] UE4M3, times the per-tensor `scale2`."""
    rows, half = codes.shape
    nib = np.empty((rows, half * 2), np.uint8)
    nib[:, 0::2] = codes & 15
    nib[:, 1::2] = codes >> 4
    mag = E2M1[nib & 7] * np.where(nib & 8, np.float32(-1), np.float32(1))
    sc = S.fp8_e4m3_table()[scales]
    return (mag.reshape(rows, -1, 16) * sc[:, :, None] * np.float32(scale2)).reshape(rows, half * 2)


def _quant_job(args):
    """Rows [row0, row0 + rows) of a [ne1, ne0] matrix (rows or columns in the GGUF order) as Q8_0."""
    path, off, form, ne0, ne1, row0, rows, kind, first, heads, aux = args
    sel = None
    if kind == "rows":
        sel = gdn_index(ne1, first, *heads)[row0:row0 + rows]
    r = sel if sel is not None else slice(row0, row0 + rows)
    if form == "bf16":
        u = np.memmap(path, dtype="<u2", mode="r", offset=off, shape=(ne1, ne0))[r]
        x = S._bf16_to_f32(np.ascontiguousarray(u))
    elif form == "fp8":
        u = np.memmap(path, dtype=np.uint8, mode="r", offset=off, shape=(ne1, ne0))[r]
        scale = np.fromfile(aux[0][0], dtype="<f4", count=1, offset=aux[0][1])[0]
        x = S.fp8_e4m3_table()[np.ascontiguousarray(u)] * np.float32(scale)
    else:
        (wp, wo), (sp, so), (tp, to) = aux
        codes = np.memmap(wp, dtype=np.uint8, mode="r", offset=wo, shape=(ne1, ne0 // 2))[r]
        sc = np.memmap(sp, dtype=np.uint8, mode="r", offset=so, shape=(ne1, ne0 // 16))[r]
        x = fp4_values(np.ascontiguousarray(codes), np.ascontiguousarray(sc),
                       float(np.fromfile(tp, dtype="<f4", count=1, offset=to)[0]))
    if kind == "cols":
        x = x[:, gdn_index(ne0, 0, *heads)]
    if not np.isfinite(x).all():
        return "a non-finite value"
    return S.q8_0(x.reshape(-1))


class Qwen35Dense(C.CheckpointDense):
    """Every language-model tensor but the routed experts, with the interface model_sections.Dense reads."""

    def __init__(self, ck: C.Checkpoint, require_complete: bool = True):
        tc = ck.text
        self.ck = ck
        self.k_heads, self.v_heads = int(tc["linear_num_key_heads"]), int(tc["linear_num_value_heads"])
        if int(tc["linear_key_head_dim"]) != GDN_HEAD or int(tc["linear_value_head_dim"]) != GDN_HEAD \
                or self.v_heads % self.k_heads:
            raise S.ConvertError("the GDN head geometry differs from the GGUF head order this converter knows")
        self.qk = 2 * self.k_heads * GDN_HEAD
        self.tensors = {}
        unknown = []
        for k in ck.order:
            if SKIP.match(k) or AUX.search(k):
                continue
            _, _, dt, shape, _ = ck.t[k]
            m = LAYER_KEY.fullmatch(k)
            rule = LAYER.get(m.group(2)) if m else GLOBAL.get(k)
            if rule is None:
                unknown.append(k)
                continue
            gname, ty, form, how = rule
            want = {"bf16": "BF16", "fp8": "F8_E4M3", "fp4": "U8"}[form]
            if dt != want:
                raise S.ConvertError(f"{k} is {dt}, expected {want}")
            name = f"blk.{m.group(1)}.{gname}" if m else gname
            if how == "gconv":
                if len(shape) != 3 or shape[1] != 1:
                    raise S.ConvertError(f"{k}: a conv weight of shape {shape}")
                ne = [shape[2], shape[0]]
            else:
                ne = list(reversed(shape))
                if form == "fp4":
                    ne[0] *= 2
                if len(ne) == 2 and ne[1] == 1:
                    ne = ne[:1]
            base = k.rsplit(".", 1)[0]
            if form == "fp8":
                ck.need(base + ".weight_scale", "F32", ())
            elif form == "fp4":
                ck.need(base + ".weight_scale", "F8_E4M3", (shape[0], shape[1] // 8))
                ck.need(base + ".weight_scale_2", "F32", ())
            if ty in ("Q8_0",) and (len(ne) != 2 or ne[0] % 32):
                raise S.ConvertError(f"{k}: rows of {ne[0]} values cannot be {ty}")
            if name in self.tensors:
                raise S.ConvertError(f"two checkpoint tensors map to {name}")
            t = C.Tensor(name, ty, k, how, ne)
            t.form = form
            self.tensors[name] = t
        if unknown:
            raise S.ConvertError("checkpoint tensors with no GGUF counterpart: " + ", ".join(unknown[:8])
                                 + (f" ... and {len(unknown) - 8} more" if len(unknown) > 8 else ""))
        if require_complete:
            self._check_complete(tc)
        self.where = {n: (None, t, ck.t[t.src][0]) for n, t in self.tensors.items()}

    def _check_complete(self, tc):
        n_layers, kinds = int(tc["num_hidden_layers"]), tc["layer_types"]
        need = [r[0] for r in GLOBAL.values()]
        for l in range(n_layers):
            if kinds[l] not in ("linear_attention", "full_attention"):
                raise S.ConvertError(f"config.json: layer {l} is {kinds[l]}")
            for key, rule in LAYER.items():
                part = key.split(".")[0]
                if part == "linear_attn" and kinds[l] != "linear_attention" \
                        or part == "self_attn" and kinds[l] != "full_attention":
                    continue
                need.append(f"blk.{l}.{rule[0]}")
        missing = [n for n in need if n not in self.tensors]
        if missing:
            raise S.ConvertError(f"the checkpoint lacks {len(missing)} tensors the engine reads: " + ", ".join(missing[:8]))
        extra = [n for n in self.tensors if (m := re.match(r"blk\.(\d+)\.", n)) and int(m.group(1)) >= n_layers]
        if extra:
            raise S.ConvertError(f"the config has {n_layers} layers, the checkpoint has {extra[0]}")

    def values(self, name) -> np.ndarray:
        """A float tensor's values in the GGUF order, as float32."""
        t = self.tensors[name]
        x = self.ck.values(t.src)
        if t.how in ("conv", "gconv"):
            x = x[:, 0, :]
        if t.how == "norm":
            x = x + np.float32(1)
        if t.how == "negexp":
            x = (-np.exp(x.astype(np.float64))).astype(np.float32)
        if t.how in ("heads", "negexp"):
            x = x[gdn_order(self.v_heads, self.k_heads)]
        if t.how == "gconv":
            x = x[gdn_index(x.shape[0], self.qk, self.v_heads, self.k_heads)]
        return np.ascontiguousarray(x.reshape(t.ne[::-1]) if len(t.ne) == 1 else x, dtype=np.float32)

    def jobs(self, name):
        t = self.tensors[name]
        p, off, _, shape, _ = self.ck.t[t.src]
        base = t.src.rsplit(".", 1)[0]
        aux = []
        if t.form == "fp8":
            aux = [(str(self.ck.t[base + ".weight_scale"][0]), self.ck.t[base + ".weight_scale"][1])]
        elif t.form == "fp4":
            aux = [(str(self.ck.t[base + "." + a][0]), self.ck.t[base + "." + a][1])
                   for a in ("weight", "weight_scale", "weight_scale_2")]
            aux[0] = (str(p), off)
        ne0, ne1 = t.ne
        step = max(1, C.VALUES_PER_JOB // ne0)
        kind = {"vrows": "rows", "zrows": "rows", "vcols": "cols"}.get(t.how)
        for r in range(0, ne1, step):
            yield (str(p), off, t.form, ne0, ne1, r, min(step, ne1 - r), kind,
                   self.qk if t.how == "vrows" else 0, (self.v_heads, self.k_heads), aux)

    def chunks(self, name, pool, depth: int = 64):
        for b in S.bounded_imap(pool, _quant_job, self.jobs(name), depth):
            if isinstance(b, str):
                raise S.ConvertError(f"{name}: {b}")
            yield b


def model_kv(ck: C.Checkpoint, file_type: int, name: str | None) -> dict:
    """The qwen35moe GGUF metadata the engine reads, from config.json, generation_config.json and the model card."""
    c, tc = ck.config, ck.text
    if tc.get("model_type") != "qwen3_5_moe_text":
        raise S.ConvertError(f"config.json: model_type {tc.get('model_type')}, expected qwen3_5_moe_text")
    d = ck.dir
    gen = json.loads((d / "generation_config.json").read_text(encoding="utf-8")) if (d / "generation_config.json").is_file() else {}
    card = C.front_matter(d / "README.md")
    rope = tc.get("rope_parameters", {})
    head = int(tc["head_dim"])
    kv = {"general.architecture": ARCH[:-1], "general.type": "model"}
    for key, src in (("general.sampling.top_k", "top_k"), ("general.sampling.top_p", "top_p"),
                     ("general.sampling.temp", "temperature")):
        if src in gen:
            kv[key] = int(gen[src]) if src == "top_k" else C.f32(gen[src])
    if name:
        kv["general.name"] = name
    if "license" in card:
        kv["general.license"] = card["license"]
    kv.update({
        ARCH + "block_count": int(tc["num_hidden_layers"]),
        ARCH + "context_length": int(tc["max_position_embeddings"]),
        ARCH + "embedding_length": int(tc["hidden_size"]),
        ARCH + "attention.head_count": int(tc["num_attention_heads"]),
        ARCH + "attention.head_count_kv": int(tc["num_key_value_heads"]),
        ARCH + "rope.dimension_sections": (list(rope.get("mrope_section", [])) + [0, 0, 0, 0])[:4],
        ARCH + "rope.freq_base": C.f32(rope.get("rope_theta", tc.get("rope_theta"))),
        ARCH + "attention.layer_norm_rms_epsilon": C.f32(tc["rms_norm_eps"]),
        ARCH + "expert_count": int(tc["num_experts"]),
        ARCH + "expert_used_count": int(tc["num_experts_per_tok"]),
        ARCH + "attention.key_length": head,
        ARCH + "attention.value_length": head,
        "general.file_type": file_type,
        ARCH + "expert_feed_forward_length": int(tc["moe_intermediate_size"]),
        ARCH + "expert_shared_feed_forward_length": int(tc["shared_expert_intermediate_size"]),
        ARCH + "ssm.conv_kernel": int(tc["linear_conv_kernel_dim"]),
        ARCH + "ssm.state_size": int(tc["linear_key_head_dim"]),
        ARCH + "ssm.group_count": int(tc["linear_num_key_heads"]),
        ARCH + "ssm.time_step_rank": int(tc["linear_num_value_heads"]),
        ARCH + "ssm.inner_size": int(tc["linear_num_value_heads"]) * int(tc["linear_value_head_dim"]),
        ARCH + "full_attention_interval": int(tc["full_attention_interval"]),
        ARCH + "rope.dimension_count": int(head * float(rope.get("partial_rotary_factor", tc.get("partial_rotary_factor")))),
        "general.quantization_version": 2,
    })
    kv.update(C.tokenizer_kv(ck))
    return kv


def is_qwen35(checkpoint_dir) -> bool:
    cfg = pathlib.Path(checkpoint_dir) / "config.json"
    if not cfg.is_file():
        return False
    c = json.loads(cfg.read_text(encoding="utf-8"))
    return c.get("text_config", c).get("model_type") == "qwen3_5_moe_text"
