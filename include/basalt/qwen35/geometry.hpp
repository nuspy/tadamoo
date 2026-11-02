// include/basalt/qwen35/geometry.hpp - the geometry of a qwen35moe model, read from the GGUF's own metadata.
//
// The qwen35 engine path takes every dimension from this struct; none is compiled in. The GGUF key each field is
// read from is named beside it.
#pragma once

#include "basalt/artifact/model_family.hpp"

#include <cstdint>

namespace basalt::qwen35 {

struct ModelGeometry {
    int64_t n_embd = 0;             ///< embedding_length
    int64_t n_layers = 0;           ///< block_count less the in-file MTP blocks
    int64_t qsa_interval = 4;       ///< full_attention_interval: layer % interval == interval - 1 is full attention
    int64_t n_vocab = 0;            ///< the length of tokenizer.ggml.tokens

    // Gated DeltaNet layers
    int64_t ssm_state_size = 0;     ///< ssm.state_size: the key head width, and the value head width
    int64_t ssm_k_heads = 0;        ///< ssm.group_count
    int64_t ssm_v_heads = 0;        ///< ssm.time_step_rank
    int64_t ssm_d_conv = 0;         ///< ssm.conv_kernel
    int64_t ssm_conv_channels = 0;  ///< 2 * state * k_heads + value_dim, derived
    int64_t ssm_value_dim = 0;      ///< ssm.inner_size, state * v_heads

    // full-attention layers
    int64_t n_head = 0;             ///< attention.head_count
    int64_t n_head_kv = 0;          ///< attention.head_count_kv
    int64_t head_dim = 0;           ///< attention.key_length (= attention.value_length)

    // MoE, on every layer
    int64_t n_expert = 0;           ///< expert_count
    int64_t n_expert_used = 0;      ///< expert_used_count
    int64_t n_ff = 0;               ///< expert_feed_forward_length
    int64_t n_ff_shexp = 0;         ///< expert_shared_feed_forward_length

    int64_t n_mtp_layers = 0;       ///< nextn_predict_layers: MTP blocks after the trunk
    basalt::ModelFamily family = basalt::ModelFamily::Qwen35Moe;

    int64_t n_qsa_layers() const { return n_layers / qsa_interval; }
    int64_t n_gdn_layers() const { return n_layers - n_qsa_layers(); }
};

/// True for the full-attention layers of the trunk.
inline bool is_qsa_layer(const ModelGeometry& g, int64_t layer) {
    return layer % g.qsa_interval == g.qsa_interval - 1;
}

}  // namespace basalt::qwen35
