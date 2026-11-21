#include "basalt/core/lora.hpp"

#include "basalt/artifact/gguf_reader.hpp"
#include "basalt/ggml/ggml.h"

#include <algorithm>
#include <cstring>
#include <vector>

namespace basalt::core {

namespace {

// Must stay equal to the suffix list `eligible()` in src/core/native_dense.cpp: those are the only tensors the
// engine's merge-at-load fusion can apply.
bool eligible_suffix(const std::string& name) {
    static const char* const suffixes[] = {".attn_qkv.weight", ".attn_gate.weight", ".ssm_out.weight",
        ".attn_q.weight", ".attn_k.weight", ".attn_v.weight", ".attn_output.weight",
        ".ffn_gate_shexp.weight", ".ffn_up_shexp.weight", ".ffn_down_shexp.weight"};
    for (const char* s : suffixes) {
        const size_t sl = std::strlen(s);
        if (name.size() >= sl && name.compare(name.size() - sl, sl, s) == 0) return true;
    }
    return false;
}

const char* const ENERGY_GROUPS[] = {"projections", "router", "experts", "other"};

}  // namespace

bool LoraAdapter::open(const std::string& path, LoraAdapter& out, std::string& err) {
    try {
        basalt::GgufFile g(path);
        const basalt::MetaValue* type = g.get("general.type");
        if (!type || type->s != "adapter") { err = path + ": general.type is not \"adapter\""; return false; }
        const basalt::MetaValue* atype = g.get("adapter.type");
        if (!atype || atype->s != "lora") { err = path + ": adapter.type is not \"lora\""; return false; }
        const basalt::MetaValue* arch = g.get("general.architecture");
        if (!arch || arch->s != "qwen4exp") {
            err = path + ": general.architecture is " + (arch ? "'" + arch->s + "'" : "missing") + ", not 'qwen4exp'";
            return false;
        }
        const basalt::MetaValue* alpha = g.get("adapter.lora.alpha");
        if (!alpha || !alpha->is_num()) { err = path + ": missing or non-numeric adapter.lora.alpha"; return false; }

        std::map<std::string, const basalt::TensorInfo*> a_of, b_of;
        for (const auto& t : g.tensors()) {
            const bool is_a = t.name.size() > 7 && t.name.compare(t.name.size() - 7, 7, ".lora_a") == 0;
            const bool is_b = t.name.size() > 7 && t.name.compare(t.name.size() - 7, 7, ".lora_b") == 0;
            if (!is_a && !is_b) { err = path + ": " + t.name + " is neither .lora_a nor .lora_b"; return false; }
            (is_a ? a_of : b_of)[t.name.substr(0, t.name.size() - 7)] = &t;
        }
        for (const auto& [base, ta] : a_of)
            if (b_of.find(base) == b_of.end()) { err = path + ": " + base + " has lora_a but no lora_b"; return false; }
        for (const auto& [base, tb] : b_of)
            if (a_of.find(base) == a_of.end()) { err = path + ": " + base + " has lora_b but no lora_a"; return false; }

        LoraAdapter result;
        result.path_ = path;
        result.alpha_ = (float) alpha->num();
        for (const auto& [base, ta] : a_of) {
            const basalt::TensorInfo& A = *ta;
            const basalt::TensorInfo& B = *b_of.at(base);
            if (A.type != 0 || B.type != 0) { err = path + ": " + base + " is not F32"; return false; }  // 0 = F32
            if (A.shape.size() != 2 || B.shape.size() != 2) { err = path + ": " + base + " is not a 2D matrix"; return false; }
            const int64_t n_in = (int64_t) A.shape[0], r_a = (int64_t) A.shape[1];
            const int64_t r_b = (int64_t) B.shape[0], n_out = (int64_t) B.shape[1];
            if (r_a != r_b) {
                err = path + ": " + base + ": lora_a rank " + std::to_string(r_a) + " != lora_b rank " + std::to_string(r_b);
                return false;
            }
            if (!eligible_suffix(base)) { ++result.other_count_; continue; }
            LoraPair p;
            p.r = r_a;
            p.n_in = n_in;
            p.n_out = n_out;
            const float* a_data = reinterpret_cast<const float*>(g.tensor_data(A));
            const float* b_data = reinterpret_cast<const float*>(g.tensor_data(B));
            p.a.assign(a_data, a_data + n_in * r_a);
            p.b.assign(b_data, b_data + n_out * r_b);
            result.pairs_.emplace(base, std::move(p));
        }
        for (const char* grp : ENERGY_GROUPS) {
            const basalt::MetaValue* v = g.get(std::string("adapter.basalt.energy_share.") + grp);
            if (v && v->is_num()) result.energy_share_[grp] = (float) v->num();
        }
        out = std::move(result);
        return true;
    } catch (const std::exception& e) {
        err = e.what();
        return false;
    }
}

const LoraPair* LoraAdapter::find(const std::string& gguf_name) const {
    const auto it = pairs_.find(gguf_name);
    return it == pairs_.end() ? nullptr : &it->second;
}

float LoraAdapter::energy_share(const std::string& group) const {
    const auto it = energy_share_.find(group);
    return it == energy_share_.end() ? 0.0f : it->second;
}

bool LoraSet::covers(const std::string& gguf_name, int64_t n_in, int64_t n_out, bool& found, std::string& err) const {
    found = false;
    for (const auto& e : entries_) {
        const LoraPair* p = e.adapter->find(gguf_name);
        if (!p) continue;
        found = true;
        if (p->n_in != n_in || p->n_out != n_out) {
            err = gguf_name + ": " + e.adapter->path() + " covers it as " + std::to_string(p->n_out) + "x" +
                  std::to_string(p->n_in) + ", the base tensor is " + std::to_string(n_out) + "x" + std::to_string(n_in);
            return false;
        }
    }
    return true;
}

bool lora_merge_q8_0(uint32_t base_type, const uint8_t* base_data, int64_t n_in, int64_t n_out, const LoraSet& set,
                     const std::string& gguf_name, std::vector<uint8_t>& out, std::string& err) {
    const struct ggml_type_traits* bt = ggml_get_type_traits((enum ggml_type) base_type);
    if (!bt || !bt->to_float) { err = gguf_name + ": base type has no to_float, cannot merge a LoRA into it"; return false; }
    const struct ggml_type_traits* q8 = ggml_get_type_traits(GGML_TYPE_Q8_0);
    const int64_t QK = q8->blck_size;   // 32
    if (n_in % QK) { err = gguf_name + ": " + std::to_string(n_in) + " columns is not a whole number of Q8_0 blocks"; return false; }
    const size_t base_row_bytes = ggml_row_size((enum ggml_type) base_type, n_in);
    const size_t q8_row_bytes = ggml_row_size(GGML_TYPE_Q8_0, n_in);
    const bool base_is_q8_0 = base_type == (uint32_t) GGML_TYPE_Q8_0;
    const int64_t n_blocks = n_in / QK;

    out.assign((size_t) n_out * q8_row_bytes, 0);
    std::vector<float> base_row(n_in);
    std::vector<double> delta(n_in), entry_delta(n_in);
    std::vector<float> merged_block(QK);

    for (int64_t i = 0; i < n_out; ++i) {
        const uint8_t* base_row_ptr = base_data + (size_t) i * base_row_bytes;
        bt->to_float(base_row_ptr, base_row.data(), n_in);
        std::fill(delta.begin(), delta.end(), 0.0);

        // Each entry's own sum is built up in its own buffer, starting from 0, and only ADDED to `delta` once
        // complete. Two entries that are each other's exact negation (the same adapter at scale +1 and -1) then
        // give two sums that are exact negations of each other (IEEE 754 round-to-nearest is anti-symmetric:
        // negating every input negates every rounded partial sum), so their ONE addition into `delta` is exactly
        // 0 -- which continuing to accumulate the second entry on top of the first's running total would not
        // guarantee, since the rounding at each step then depends on an already-nonzero starting point.
        for (const auto& e : set.entries()) {
            const LoraPair* p = e.adapter->find(gguf_name);
            if (!p || p->r == 0) continue;
            std::fill(entry_delta.begin(), entry_delta.end(), 0.0);
            const double coef0 = (double) e.scale * ((double) e.adapter->alpha() / (double) p->r);
            const float* b_row = p->b.data() + (size_t) i * p->r;   // b[i, :], this adapter's own rank
            for (int64_t k = 0; k < p->r; ++k) {
                const double coef = coef0 * (double) b_row[k];
                if (coef == 0.0) continue;
                const float* a_row = p->a.data() + (size_t) k * p->n_in;   // a[k, :]
                for (int64_t j = 0; j < n_in; ++j) entry_delta[(size_t) j] += coef * (double) a_row[j];
            }
            for (int64_t j = 0; j < n_in; ++j) delta[(size_t) j] += entry_delta[(size_t) j];
        }

        uint8_t* out_row = out.data() + (size_t) i * q8_row_bytes;
        for (int64_t blk = 0; blk < n_blocks; ++blk) {
            bool zero = true;
            for (int64_t j = blk * QK; j < blk * QK + QK && zero; ++j) zero = delta[(size_t) j] == 0.0;
            uint8_t* out_block = out_row + (size_t) blk * q8->type_size;
            if (zero && base_is_q8_0) {
                // re-quantizing an already-Q8_0 block is not always its own fixed point (the max code can be 126
                // instead of 127), so a null delta is only exact if the block's bytes are copied, not recomputed
                std::memcpy(out_block, base_row_ptr + (size_t) blk * q8->type_size, q8->type_size);
                continue;
            }
            for (int64_t j = 0; j < QK; ++j) merged_block[(size_t) j] = (float) ((double) base_row[blk * QK + j] + delta[(size_t) (blk * QK + j)]);
            q8->from_float_ref(merged_block.data(), out_block, QK);
        }
    }
    return true;
}

}  // namespace basalt::core
