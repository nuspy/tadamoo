// include/basalt/core/lora.hpp - a LoRA adapter GGUF (tools/convert/lora.py's output) and the merge that fuses
// its delta into a dense tensor's own quantized blocks, matching the reader's contract in lora.py's docstring:
// per tensor, scale = adapter.lora.alpha / (rows of lora_a) * the caller's own scale.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

namespace basalt { class GgufFile; }

namespace basalt::core {

// One routed expert's lora_a/lora_b pair, read in place from the adapter's mapped file (not copied): the same
// row-major layout as LoraPair.
struct LoraExpertView {
    const float* a = nullptr;   // r * n_in: a[k * n_in + j]
    const float* b = nullptr;   // n_out * r: b[i * r + k]
    int64_t r = 0, n_in = 0, n_out = 0;
};

enum class ExpertRole { Gate = 0, Up = 1, Down = 2 };

// One lora_a/lora_b pair, read into f32, in the checkpoint's own row/column order (the order tools/convert/lora.py
// already reorders into before writing: the GDN value-head order and the indexer split are done there, not here).
struct LoraPair {
    std::vector<float> a;   // r * n_in, row-major: a[k * n_in + j]
    std::vector<float> b;   // n_out * r, row-major: b[i * r + k]
    int64_t r = 0, n_in = 0, n_out = 0;
};

// One opened adapter file. Only the ten native-matrix families (native_dense.cpp's `eligible()` suffixes) are
// kept in RAM, as float32 LoraPair values; the routed experts stay in the mapped file and are read through
// `expert()` (lora_experts.hpp fuses them); the router (ffn_gate_inp.weight) and everything else
// (hyper-connection, the indexer, the GDN gates, the output head) are counted separately, by name, never loaded.
// A routed expert's pair is named either per expert (blk.L.ffn_{gate,up,down}_exps.E.weight, 2D, what
// tools/convert/lora.py writes) or stacked over the experts (blk.L.ffn_{gate,up,down}_exps.weight, 3D: lora_a
// [n_in, r, n_expert], lora_b [r, n_out, n_expert], what llama.cpp's trainers write).
class LoraAdapter {
public:
    /// false (with `err`) on: a missing file, general.type != "adapter", adapter.type != "lora", an architecture
    /// that is not "qwen4exp", a missing adapter.lora.alpha, a lora_a/lora_b whose "<name>" differs in r, a
    /// tensor present as only one of the pair, or a tensor whose declared size reaches past the file.
    static bool open(const std::string& path, LoraAdapter& out, std::string& err);

    const std::string& path() const { return path_; }
    float alpha() const { return alpha_; }
    const LoraPair* find(const std::string& gguf_name) const;
    int covered_count() const { return (int) pairs_.size(); }
    /// Tensors this adapter has outside the ten native families and the routed experts (not loaded), by what they are.
    int router_count() const { return router_count_; }
    int other_count() const { return other_count_; }
    /// The routed-expert tensor pairs (per expert or stacked), which the expert fusion applies.
    int expert_count() const { return expert_count_; }
    /// One past the highest layer that has expert tensors; 0 when none do.
    int64_t expert_layers() const { return experts_.empty() ? 0 : std::get<0>(experts_.rbegin()->first) + 1; }
    /// Whether any layer's `role` matrices are in this adapter.
    bool has_experts(int64_t layer, ExpertRole role) const;
    /// Expert `expert` of `layer`'s `role` matrix; false when this adapter has none for it.
    bool expert(int64_t layer, ExpertRole role, int64_t expert, LoraExpertView& out) const;
    /// What share of the adapter's total energy (tools/convert/lora.py's adapter.basalt.energy_share.*) each
    /// group carries, or std::nullopt when that converter did not write the metadata (an adapter from a
    /// different converter, say) -- never 0 standing in for "not written".
    std::optional<float> energy_share(const std::string& group) const;   // "projections"/"router"/"experts"/"other"

private:
    // Where one expert matrix pair sits in the mapped file; n_expert is 0 for a per-expert pair.
    struct ExpertTensors {
        const uint8_t* a = nullptr;
        const uint8_t* b = nullptr;
        int64_t r = 0, n_in = 0, n_out = 0, n_expert = 0;
    };
    std::string path_;
    float alpha_ = 0;
    int router_count_ = 0, expert_count_ = 0, other_count_ = 0;
    std::map<std::string, float> energy_share_;
    std::map<std::string, LoraPair> pairs_;
    std::shared_ptr<basalt::GgufFile> file_;                      // keeps the mapping `experts_` points into
    std::map<std::tuple<int64_t, int, int64_t>, ExpertTensors> experts_;   // (layer, role, expert or -1 stacked)
};

// One adapter at the caller's chosen scale (--lora-scaled's SCALE; --lora alone means 1.0).
struct LoraEntry {
    const LoraAdapter* adapter = nullptr;
    float scale = 1.0f;
};

// The adapters applied to one load, in the fixed order the caller listed them in — fusion is not associative in
// floating point, so the order is part of the result, the same as the control vectors' file order.
class LoraSet {
public:
    LoraSet() = default;
    explicit LoraSet(std::vector<LoraEntry> entries) : entries_(std::move(entries)) {}
    bool empty() const { return entries_.empty(); }
    const std::vector<LoraEntry>& entries() const { return entries_; }
    /// Whether any entry carries routed-expert tensors.
    bool touches_experts() const {
        for (const auto& e : entries_)
            if (e.adapter->expert_count() > 0) return true;
        return false;
    }

    /// Whether any entry covers `gguf_name`, and whether every entry that does agrees with (n_in, n_out). false
    /// (with `err`) on a shape mismatch; true with `found` set to whether anything in fact covers the tensor.
    bool covers(const std::string& gguf_name, int64_t n_in, int64_t n_out, bool& found, std::string& err) const;

private:
    std::vector<LoraEntry> entries_;
};

// Fuses `set`'s delta for `gguf_name` into a dense tensor, row by row, always producing Q8_0 (every native dense
// family the engine loads can be dequantized; not all of them can be losslessly requantized back to their own
// type after an arbitrary delta, so a covered tensor is Q8_0 in VRAM regardless of its type in the pack):
//   1. dequantize the row with `base_type`'s to_float;
//   2. sum each covering entry's delta into its own vector first (entry scale * (alpha / r) * B[row, :] . A),
//      THEN add that entry's vector to the row's running delta, THEN add the running delta to the base row —
//      so two entries whose deltas are exact negations of each other (e.g. the same adapter at +1 and -1) give
//      a net-zero delta exactly (see lora.cpp for why that needs this specific order);
//   3. requantize the row with Q8_0's own from_float_ref — EXCEPT a 32-value block whose delta is all zero: that
//      block's 34 bytes are copied from the base unchanged (re-quantizing an already-Q8_0 block is not always the
//      identity, since the block's max code can be 126 instead of 127; this keeps "no net delta" exact even then).
//
// Row r of `base_data` is `n_in` values of `base_type` (the tensor's GGUF ne1 rows, each of ne0 = n_in values);
// `out` is resized to n_out rows of Q8_0 (n_in / 32 * 34 bytes each). false (with `err`) when `base_type` has no
// to_float, or `n_in` is not a whole number of 32-value blocks. Each entry in `set` uses its own adapter's rank;
// different entries covering the same tensor need not agree with each other, only with the base tensor's shape
// (checked by LoraSet::covers, not by this function).
bool lora_merge_q8_0(uint32_t base_type, const uint8_t* base_data, int64_t n_in, int64_t n_out, const LoraSet& set,
                     const std::string& gguf_name, std::vector<uint8_t>& out, std::string& err);

}  // namespace basalt::core
