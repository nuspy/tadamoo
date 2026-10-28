// include/basalt/qwen35/layout.hpp - a qwen35moe model: the geometry read from a GGUF's metadata, and the tensor
// layout table (every tensor of a layer, by name and GGUF shape) checked against a model file's directory.
//
// Header-only and GPU-free: the engine and the unit tests share it.
//
//   * geometry_from_gguf: fills ModelGeometry from the "qwen35moe.*" keys; every key is required
//     (check_architecture_qwen35moe).
//   * layer_tensors / global_tensors: the layout tables. Names are GGUF names (blk.N.<suffix>), shapes are in GGUF
//     order (dim 0 contiguous).
//   * check_gguf_layout: every table entry present with its shape, inside its file, of a type with a known block
//     size, and no blk.* tensor the table does not name.
#pragma once

#include "basalt/artifact/gguf_reader.hpp"
#include "basalt/artifact/model_family.hpp"
#include "basalt/qwen35/geometry.hpp"

#include <cstdint>
#include <cstdio>
#include <set>
#include <string>
#include <vector>

namespace basalt::qwen35 {

/// Fills `g` (and `top_k`, the experts per token) from the metadata shard `meta`. False with `err` when the file is
/// not a qwen35moe model or a key has an impossible value.
inline bool geometry_from_gguf(const basalt::GgufFile& meta, ModelGeometry& g, int64_t& top_k, std::string& err) {
    if (basalt::model_family(meta) != basalt::ModelFamily::Qwen35Moe) {
        const basalt::MetaValue* arch = meta.get("general.architecture");
        err = arch ? "architecture is '" + arch->s + "', this engine path requires 'qwen35moe'"
                   : std::string("missing general.architecture");
        return false;
    }
    err = basalt::check_architecture_qwen35moe(meta);
    if (!err.empty()) return false;
    const std::string p = "qwen35moe.";
    g = ModelGeometry{};
    top_k = 0;
    auto num = [&](const char* key, int64_t& dst) {
        if (const basalt::MetaValue* v = meta.get(p + key))
            if (v->is_num()) dst = (int64_t) v->u;
    };
    int64_t block_count = 0, key_length = 0, value_length = -1;
    num("block_count", block_count);
    num("embedding_length", g.n_embd);
    num("attention.head_count", g.n_head);
    num("attention.head_count_kv", g.n_head_kv);
    num("attention.key_length", key_length);
    num("attention.value_length", value_length);
    num("expert_count", g.n_expert);
    num("expert_used_count", top_k);
    g.n_expert_used = top_k;
    num("expert_feed_forward_length", g.n_ff);
    num("expert_shared_feed_forward_length", g.n_ff_shexp);
    num("ssm.conv_kernel", g.ssm_d_conv);
    num("ssm.state_size", g.ssm_state_size);
    num("ssm.group_count", g.ssm_k_heads);
    num("ssm.time_step_rank", g.ssm_v_heads);
    num("ssm.inner_size", g.ssm_value_dim);
    num("full_attention_interval", g.qsa_interval);
    num("nextn_predict_layers", g.n_mtp_layers);
    g.head_dim = key_length;
    g.n_layers = block_count - g.n_mtp_layers;
    g.ssm_conv_channels = 2 * g.ssm_state_size * g.ssm_k_heads + g.ssm_value_dim;
    if (const basalt::MetaValue* v = meta.get("tokenizer.ggml.tokens"))
        if (v->type == basalt::MetaType::ARRAY && v->count > 0) g.n_vocab = (int64_t) v->count;

    char buf[256];
    auto bad = [&](const char* what) {
        std::snprintf(buf, sizeof buf, "%s: %s", meta.path().c_str(), what);
        err = buf;
        return false;
    };
    if (g.n_layers <= 0 || g.n_embd <= 0 || g.n_head <= 0 || g.n_head_kv <= 0 || g.head_dim <= 0 || g.n_expert <= 0 ||
        top_k <= 0 || top_k > g.n_expert || g.n_ff <= 0 || g.ssm_state_size <= 0 || g.ssm_k_heads <= 0 ||
        g.ssm_v_heads <= 0 || g.ssm_d_conv <= 0 || g.qsa_interval < 2)
        return bad("a geometry key is zero or out of range");
    if (value_length >= 0 && value_length != key_length) return bad("attention.key_length != attention.value_length");
    if (g.ssm_value_dim != g.ssm_state_size * g.ssm_v_heads) return bad("ssm.inner_size != ssm.state_size * ssm.time_step_rank");
    if (g.ssm_v_heads % g.ssm_k_heads) return bad("ssm.time_step_rank is not a multiple of ssm.group_count");
    if (g.n_head % g.n_head_kv) return bad("attention.head_count is not a multiple of attention.head_count_kv");
    if (g.n_layers % g.qsa_interval) return bad("the layer count is not a multiple of full_attention_interval");
    return true;
}

enum class LayerKind { Gdn, Attention, Mtp };

inline LayerKind layer_kind(const ModelGeometry& g, int64_t layer) {
    if (layer >= g.n_layers) return LayerKind::Mtp;
    return is_qsa_layer(g, layer) ? LayerKind::Attention : LayerKind::Gdn;
}

/// One tensor of the layout table: its name (a blk.N. suffix for a layer's, the full name for a global one) and
/// its GGUF shape.
struct TensorWant {
    std::string name;
    std::vector<int64_t> shape;
};

/// Every tensor of layer `layer` (kind from `layer_kind`).
inline std::vector<TensorWant> layer_tensors(const ModelGeometry& g, int64_t layer) {
    const LayerKind kind = layer_kind(g, layer);
    const int64_t N = g.n_embd, NE = g.n_expert, FF = g.n_ff, FS = g.n_ff_shexp;
    std::vector<TensorWant> t;
    auto add = [&](const char* s, std::vector<int64_t> shape) { t.push_back({s, std::move(shape)}); };
    // MoE, every layer (the MTP block's too)
    add("ffn_gate_inp.weight", {N, NE});
    add("ffn_gate_inp_shexp.weight", {N});
    add("ffn_gate_shexp.weight", {N, FS});
    add("ffn_up_shexp.weight", {N, FS});
    add("ffn_down_shexp.weight", {FS, N});
    add("ffn_gate_exps.weight", {N, FF, NE});
    add("ffn_up_exps.weight", {N, FF, NE});
    add("ffn_down_exps.weight", {FF, N, NE});
    // plain pre-norm residual: x += mixer(rms(x)); x += moe(rms(x))
    add("attn_norm.weight", {N});
    add("post_attention_norm.weight", {N});
    if (kind == LayerKind::Gdn) {
        add("attn_qkv.weight", {N, g.ssm_conv_channels});
        add("attn_gate.weight", {N, g.ssm_value_dim});
        add("ssm_out.weight", {g.ssm_value_dim, N});
        add("ssm_conv1d.weight", {g.ssm_d_conv, g.ssm_conv_channels});
        add("ssm_alpha.weight", {N, g.ssm_v_heads});
        add("ssm_beta.weight", {N, g.ssm_v_heads});
        add("ssm_a", {g.ssm_v_heads});
        add("ssm_dt.bias", {g.ssm_v_heads});
        add("ssm_norm.weight", {g.ssm_state_size});
    } else {
        // attention (query + output gate in attn_q), also the MTP block's
        add("attn_q.weight", {N, 2 * g.n_head * g.head_dim});
        add("attn_k.weight", {N, g.n_head_kv * g.head_dim});
        add("attn_v.weight", {N, g.n_head_kv * g.head_dim});
        add("attn_output.weight", {g.n_head * g.head_dim, N});
        add("attn_q_norm.weight", {g.head_dim});
        add("attn_k_norm.weight", {g.head_dim});
    }
    if (kind == LayerKind::Mtp) {
        // the GGUF-native MTP block: eh_proj over concat(enorm(embedding), hnorm(hidden))
        add("nextn.eh_proj.weight", {2 * N, N});
        add("nextn.enorm.weight", {N});
        add("nextn.hnorm.weight", {N});
        add("nextn.shared_head_norm.weight", {N});
    }
    return t;
}

/// The tensors outside the layers.
inline std::vector<TensorWant> global_tensors(const ModelGeometry& g) {
    return {{"token_embd.weight", {g.n_embd, g.n_vocab}},
            {"output.weight", {g.n_embd, g.n_vocab}},
            {"output_norm.weight", {g.n_embd}}};
}

/// Checks the model `m` (all shards) against the layout table of `g`. `checked` counts the tensors found.
inline bool check_gguf_layout(const basalt::GgufModel& m, const ModelGeometry& g, std::string& err,
                              size_t* checked = nullptr) {
    std::set<std::string> named;
    size_t n = 0;
    auto check = [&](const std::string& name, const std::vector<int64_t>& shape) {
        named.insert(name);
        size_t s = 0;
        const basalt::TensorInfo* t = m.find(name, &s);
        if (!t) {
            err = "missing " + name;
            return false;
        }
        bool same = t->shape.size() == shape.size();
        for (size_t i = 0; same && i < shape.size(); ++i) same = t->shape[i] == (uint64_t) shape[i];
        if (!same) {
            std::string got, want;
            for (uint64_t d : t->shape) got += (got.empty() ? "" : ", ") + std::to_string(d);
            for (int64_t d : shape) want += (want.empty() ? "" : ", ") + std::to_string(d);
            err = name + " is [" + got + "], the " + basalt::model_family_title(g.family) + " layout wants [" + want + "]";
            return false;
        }
        if (!m.in_bounds(*t, s)) {
            err = name + " (" + t->type_name() + ") has no known size inside " + m.shard(s).path();
            return false;
        }
        ++n;
        return true;
    };
    for (const TensorWant& w : global_tensors(g))
        if (!check(w.name, w.shape)) return false;
    for (int64_t l = 0; l < g.n_layers + g.n_mtp_layers; ++l)
        for (const TensorWant& w : layer_tensors(g, l))
            if (!check("blk." + std::to_string(l) + "." + w.name, w.shape)) return false;
    for (size_t s = 0; s < m.size(); ++s)
        for (const basalt::TensorInfo& t : m.shard(s).tensors())
            if (!named.count(t.name)) {
                err = t.name + " is not in the " + std::string(basalt::model_family_title(g.family)) + " layout";
                return false;
            }
    if (checked) *checked = n;
    return true;
}

}  // namespace basalt::qwen35
