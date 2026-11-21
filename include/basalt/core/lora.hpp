// include/basalt/core/lora.hpp - a LoRA adapter GGUF (tools/convert/lora.py's output) and the merge that fuses
// its delta into a dense tensor's own quantized blocks, matching the reader's contract in lora.py's docstring:
// per tensor, scale = adapter.lora.alpha / (rows of lora_a) * the caller's own scale.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace basalt { class GgufFile; }

namespace basalt::core {

// One lora_a/lora_b pair, read into f32, in the checkpoint's own row/column order (the order tools/convert/lora.py
// already reorders into before writing: the GDN value-head order and the indexer split are done there, not here).
struct LoraPair {
    std::vector<float> a;   // r * n_in, row-major: a[k * n_in + j]
    std::vector<float> b;   // n_out * r, row-major: b[i * r + k]
    int64_t r = 0, n_in = 0, n_out = 0;
};

// One opened adapter file. Only the ten native-matrix families (native_dense.cpp's `eligible()` suffixes) are
// kept in RAM; every other tensor (the router, the routed experts, hyper-connection, the indexer, the GDN gates,
// the output head) is counted but not loaded — this engine does not apply them yet.
class LoraAdapter {
public:
    /// false (with `err`) on: a missing file, general.type != "adapter", adapter.type != "lora", an architecture
    /// that is not "qwen4exp", a missing adapter.lora.alpha, a lora_a/lora_b whose "<name>" differs in r, or a
    /// tensor present as only one of the pair.
    static bool open(const std::string& path, LoraAdapter& out, std::string& err);

    const std::string& path() const { return path_; }
    float alpha() const { return alpha_; }
    const LoraPair* find(const std::string& gguf_name) const;
    int covered_count() const { return (int) pairs_.size(); }
    /// Tensors this adapter has outside the ten native families (not loaded): the count, and what share of the
    /// adapter's total energy (tools/convert/lora.py's adapter.basalt.energy_share.*) each group carries.
    int other_tensor_count() const { return other_count_; }
    float energy_share(const std::string& group) const;   // "projections" / "router" / "experts" / "other"

private:
    std::string path_;
    float alpha_ = 0;
    int other_count_ = 0;
    std::map<std::string, float> energy_share_;
    std::map<std::string, LoraPair> pairs_;
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
//      a net-zero delta exactly, not a base value perturbed twice and rounded twice;
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
