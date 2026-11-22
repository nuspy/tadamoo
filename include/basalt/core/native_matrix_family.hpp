// include/basalt/core/native_matrix_family.hpp - the ten per-layer projection names NativeDense loads as native
// matrices (native_dense.cpp's own `eligible()` calls this for everything but the PLE key, which has its own
// rule), and the only names a LoRA merge can apply (lora.cpp). One list, no CUDA dependency, so both can use it.
#pragma once

#include <cstring>
#include <string>

namespace basalt::core {

inline bool is_native_matrix_family(const std::string& name) {
    static const char* const suffixes[] = {".attn_qkv.weight", ".attn_gate.weight", ".ssm_out.weight",
        ".attn_q.weight", ".attn_k.weight", ".attn_v.weight", ".attn_output.weight",
        ".ffn_gate_shexp.weight", ".ffn_up_shexp.weight", ".ffn_down_shexp.weight"};
    for (const char* suffix : suffixes) {
        const size_t sl = std::strlen(suffix);
        if (name.size() >= sl && name.compare(name.size() - sl, sl, suffix) == 0) return true;
    }
    return false;
}

}  // namespace basalt::core
