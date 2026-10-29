// src/core/layout.cpp - the shape checks.  See the header for why this exists.
#include "basalt/core/layout.hpp"

#include <cstdio>

namespace basalt::core {
namespace {

/// Which residual stream a tensor belongs to: the gated one (hyper-connections and an indexer) or the plain one.
enum class Residual { Any, Gated, Plain };

bool in_scope(const ModelGeometry& g, Residual r) {
    return r == Residual::Any || (r == Residual::Gated) == g.gated_residual();
}

/// A required 2-D shape.  `ne0` is the CONTIGUOUS axis, matching the manifest and `s_gemv`'s convention
/// (`y[o] = sum_i x[i]*W[i][o]`, row o contiguous of length ne0).
struct Want2 {
    const char* suffix;
    int64_t ne0;
    int64_t ne1;
    bool qsa_only;
    bool gdn_only;
    Residual residual = Residual::Any;
};

/// A required element count and ENGINE FORM for a 1-D tensor.
///
/// `kind` is not decoration.  The engine form of a 1-D tensor is whatever `WeightKind` the loader applied -
/// `F32` copies 4 B/elem, `Bf16InF32` re-rounds to 2 - and a consumer that casts either one to `const float*`
/// reads 2x its length.  **THAT IS NOT HYPOTHETICAL: `ffn_gate_inp_shexp.weight` is BF16, so the arena holds
/// 5120 B for 2560 elements, and `shared_expert` read it as f32.  Every one of the 2560 MoE outputs came out
/// non-finite** - a wrong answer loud enough to notice, which is the lucky version.  Reading 5120 B past the
/// end of a tensor inside a 4.5 GiB arena does not fault, so the same mistake on a tensor followed by
/// plausible bytes would have produced plausible logits.
struct Want1 {
    const char* suffix;
    int64_t elements;
    WeightKind kind;
    bool qsa_only;
    bool gdn_only;
    Residual residual = Residual::Any;
};

bool fail(std::string& err, const LayerView& v, const char* suffix, const char* what, int64_t got,
          int64_t want) {
    char buf[512];
    std::snprintf(buf, sizeof buf, "layer %lld: %s %s is %lld, the kernels require %lld",
                  (long long) v.layer(), v.name(suffix).c_str(), what, (long long) got, (long long) want);
    err = buf;
    return false;
}

bool check_one(const WeightTable& t, const ModelGeometry& g, int64_t layer, std::string& err) {
    const LayerView v(t, layer);
    const bool qsa = is_qsa_layer(g, layer);

    // ---- the 2-D tensor set, per layer family
    const Want2 want2[] = {
        // gated residual, EVERY layer - `gr_read` takes w_down (hc_lr, hc_dim) and w_up (hc_lr, hc_dim)
        // after its own transpose, so the pack's orientation is [hc_dim, hc_lr] and [hc_lr, hc_dim].
        {"hc_attn_down.weight", g.hc_dim(), g.hc_lr, false, false, Residual::Gated},
        {"hc_attn_up.weight", g.hc_lr, g.hc_dim(), false, false, Residual::Gated},
        {"hc_attn_inject.weight", g.hc_dim(), g.hc, false, false, Residual::Gated},
        {"hc_ffn_down.weight", g.hc_dim(), g.hc_lr, false, false, Residual::Gated},
        {"hc_ffn_up.weight", g.hc_lr, g.hc_dim(), false, false, Residual::Gated},
        {"hc_ffn_inject.weight", g.hc_dim(), g.hc, false, false, Residual::Gated},
        // MoE, EVERY layer
        {"ffn_gate_inp.weight", g.n_embd, g.n_expert, false, false},
        {"ffn_gate_shexp.weight", g.n_embd, g.n_ff, false, false},
        {"ffn_up_shexp.weight", g.n_embd, g.n_ff, false, false},
        {"ffn_down_shexp.weight", g.n_ff, g.n_embd, false, false},
        // GDN only
        {"attn_qkv.weight", g.n_embd, g.ssm_conv_channels, false, true},
        {"attn_gate.weight", g.n_embd, g.ssm_value_dim, false, true},
        {"ssm_out.weight", g.ssm_value_dim, g.n_embd, false, true},
        {"ssm_conv1d.weight", g.ssm_d_conv, g.ssm_conv_channels, false, true},
        {"ssm_alpha.weight", g.n_embd, g.ssm_v_heads, false, true},
        {"ssm_beta.weight", g.n_embd, g.ssm_v_heads, false, true},
        // QSA only
        {"attn_q.weight", g.n_embd, 2 * g.n_head * g.head_dim, true, false},
        {"attn_k.weight", g.n_embd, g.n_head_kv * g.head_dim, true, false},
        {"attn_v.weight", g.n_embd, g.n_head_kv * g.head_dim, true, false},
        {"attn_output.weight", g.n_head * g.head_dim, g.n_embd, true, false},
        // THE INDEXER.  `q_proj` is the QUERY count and `k_proj` is the KEY width, and they are different
        // numbers - conflating them makes the planner's indexer term 4x too big.
        {"indexer.q_proj.weight", g.n_embd, g.idx_q_heads * g.idx_key_dim, true, false, Residual::Gated},
        {"indexer.k_proj.weight", g.n_embd, g.idx_key_dim, true, false, Residual::Gated},
    };
    for (const Want2& w : want2) {
        if (!in_scope(g, w.residual)) continue;
        if (w.qsa_only && !qsa) continue;
        if (w.gdn_only && qsa) continue;
        const WeightRef* r = v.get(w.suffix);
        if (!r) {
            err = "layer " + std::to_string(layer) + ": missing " + v.name(w.suffix);
            return false;
        }
        if (r->ne0 != w.ne0) return fail(err, v, w.suffix, "ne0", r->ne0, w.ne0);
        if (r->ne1 != w.ne1) return fail(err, v, w.suffix, "ne1", r->ne1, w.ne1);
    }

    // ---- the 1-D set.  EVERY ONE OF THESE IS `F32` EXCEPT the shared expert's scalar gate, which is BF16 -
    // and that single exception is the one a reader would not guess, so it is written down rather than
    // inferred from the tensor count.
    const Want1 want1[] = {
        {"hc_attn_norm.weight", g.hc_dim(), WeightKind::F32, false, false, Residual::Gated},
        {"hc_ffn_norm.weight", g.hc_dim(), WeightKind::F32, false, false, Residual::Gated},
        {"attn_norm.weight", g.n_embd, WeightKind::F32, false, false, Residual::Plain},
        {"post_attention_norm.weight", g.n_embd, WeightKind::F32, false, false, Residual::Plain},
        {"ffn_gate_inp_shexp.weight", g.n_embd, WeightKind::Bf16InF32, false, false},
        {"ssm_a", g.ssm_v_heads, WeightKind::F32, false, true},
        {"ssm_dt.bias", g.ssm_v_heads, WeightKind::F32, false, true},
        {"ssm_norm.weight", g.ssm_state_size, WeightKind::F32, false, true},
        {"attn_q_norm.weight", g.head_dim, WeightKind::F32, true, false},
        {"attn_k_norm.weight", g.head_dim, WeightKind::F32, true, false},
        {"indexer.q_norm.weight", g.idx_key_dim, WeightKind::F32, true, false, Residual::Gated},
        {"indexer.k_norm.weight", g.idx_key_dim, WeightKind::F32, true, false, Residual::Gated},
    };
    for (const Want1& w : want1) {
        if (!in_scope(g, w.residual)) continue;
        if (w.qsa_only && !qsa) continue;
        if (w.gdn_only && qsa) continue;
        const WeightRef* r = v.get(w.suffix);
        if (!r) {
            err = "layer " + std::to_string(layer) + ": missing " + v.name(w.suffix);
            return false;
        }
        if (r->elements != w.elements) return fail(err, v, w.suffix, "elements", r->elements, w.elements);
        if (r->kind != w.kind) {
            // The byte count is what makes this a REAL check and not a label: a 4 B/elem tensor holds
            // `elements * 4`, a 2 B/elem one holds `elements * 2`, and a consumer reading it as the wrong one
            // walks off the end.
            const uint64_t want_bytes = (uint64_t) w.elements *
                                        (w.kind == WeightKind::Bf16InF32 ? 2u : 4u);
            char buf[512];
            std::snprintf(buf, sizeof buf,
                          "layer %lld: %s is engine form %d (%llu B), the kernels read it as form %d (%llu B)",
                          (long long) layer, v.name(w.suffix).c_str(), (int) r->kind,
                          (unsigned long long) r->bytes, (int) w.kind, (unsigned long long) want_bytes);
            err = buf;
            return false;
        }
    }
    return true;
}

bool kv_int(const Json& kv, const std::string& key, int64_t& out, std::string& err) {
    const Json* v = kv.get(key);
    if (v == nullptr || v->type != Json::Type::Int) {
        err = "meta.gguf_kv has no integer " + key;
        return false;
    }
    out = v->i64();
    return true;
}

}  // namespace

ModelGeometry geometry_for_family(ModelFamily family) {
    ModelGeometry g;
    g.family = family;
    if (family == ModelFamily::Qwen35Moe) {
        g.n_embd = 2048;
        g.n_layers = 40;
        g.ssm_v_heads = 32;
        g.ssm_conv_channels = 2 * g.ssm_state_size * g.ssm_k_heads + g.ssm_state_size * g.ssm_v_heads;
        g.ssm_value_dim = g.ssm_state_size * g.ssm_v_heads;
        g.n_head = 16;
        g.idx_q_heads = 0;
        g.idx_key_dim = 0;
        g.hc = 0;
        g.hc_lr = 0;
        g.n_expert = 256;
        g.n_expert_used = 8;
        g.n_ff = 512;
    }
    return g;
}

bool geometry_from_model(const ModelFile& model, ModelGeometry& g, std::string& err) {
    const ModelFamily family = model.family();
    if (family == ModelFamily::Unknown) {
        err = model.path() + ": meta.family names a model family Basalt does not know";
        return false;
    }
    g = geometry_for_family(family);
    const Json* kv = model.meta().get("gguf_kv");
    if (kv == nullptr || !kv->is_object()) {
        err = model.path() + ": meta has no gguf_kv";
        return false;
    }
    const Json* arch = kv->get("general.architecture");
    if (arch == nullptr || !arch->is_string() || model_family_from_arch(arch->s) != family) {
        err = model.path() + ": meta.family and general.architecture disagree";
        return false;
    }
    if (family != ModelFamily::Qwen35Moe) return true;   // qwen4exp: the compiled-in geometry, adjusted by its caller
    const std::string p = std::string(model_family_arch(family)) + ".";
    int64_t inner = 0, interval = 0;
    if (!kv_int(*kv, p + "block_count", g.n_layers, err) || !kv_int(*kv, p + "embedding_length", g.n_embd, err) ||
        !kv_int(*kv, p + "attention.head_count", g.n_head, err) ||
        !kv_int(*kv, p + "attention.head_count_kv", g.n_head_kv, err) ||
        !kv_int(*kv, p + "attention.key_length", g.head_dim, err) ||
        !kv_int(*kv, p + "expert_count", g.n_expert, err) || !kv_int(*kv, p + "expert_used_count", g.n_expert_used, err) ||
        !kv_int(*kv, p + "expert_feed_forward_length", g.n_ff, err) ||
        !kv_int(*kv, p + "ssm.conv_kernel", g.ssm_d_conv, err) || !kv_int(*kv, p + "ssm.state_size", g.ssm_state_size, err) ||
        !kv_int(*kv, p + "ssm.group_count", g.ssm_k_heads, err) ||
        !kv_int(*kv, p + "ssm.time_step_rank", g.ssm_v_heads, err) || !kv_int(*kv, p + "ssm.inner_size", inner, err) ||
        !kv_int(*kv, p + "full_attention_interval", interval, err))
        return false;
    g.qsa_interval = interval;
    g.ssm_value_dim = g.ssm_state_size * g.ssm_v_heads;
    g.ssm_conv_channels = 2 * g.ssm_state_size * g.ssm_k_heads + g.ssm_value_dim;
    if (inner != g.ssm_value_dim || g.qsa_interval < 1 || g.n_layers % g.qsa_interval != 0) {
        err = model.path() + ": the ssm and layer keys of meta.gguf_kv are inconsistent";
        return false;
    }
    return true;
}

std::string LayerView::name(const char* suffix) const {
    return "blk." + std::to_string(layer_) + "." + suffix;
}

bool check_layer(const WeightTable& table, const ModelGeometry& g, int64_t layer, std::string& err) {
    if (layer < 0 || layer >= g.n_layers) {
        err = "layer " + std::to_string(layer) + " is outside 0.." + std::to_string(g.n_layers - 1);
        return false;
    }
    return check_one(table, g, layer, err);
}

bool check_all(const WeightTable& table, const ModelGeometry& g, std::string& err) {
    int64_t n_qsa = 0, n_gdn = 0;
    for (int64_t l = 0; l < g.n_layers; ++l) {
        if (!check_one(table, g, l, err)) return false;
        if (is_qsa_layer(g, l)) {
            ++n_qsa;
        } else {
            ++n_gdn;
        }
    }
    // The split is derived twice over in the model definition - the interval and an explicit ratio array -
    // and both give 36 GDN and 12 QSA.  Counting them here is the check that the LAYER TYPE PREDICATE and
    // the pack agree, which no per-tensor shape check can see.
    if (n_qsa != g.n_qsa_layers() || n_gdn != g.n_gdn_layers()) {
        char buf[256];
        std::snprintf(buf, sizeof buf, "layer split is %lld QSA / %lld GDN, the geometry says %lld / %lld",
                      (long long) n_qsa, (long long) n_gdn, (long long) g.n_qsa_layers(),
                      (long long) g.n_gdn_layers());
        err = buf;
        return false;
    }
    return true;
}

}  // namespace basalt::core
