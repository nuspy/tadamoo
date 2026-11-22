// include/basalt/core/lora_experts.hpp - a LoRA adapter's routed-expert deltas fused into the experts section.
//
// The experts keep their own quantized format (the layout says which, per layer), so the fused file has the same
// blob sizes and offsets as the section it comes from: each touched row is dequantized, gets the adapter's delta
// (scale * alpha / r * B[row, :] . A) and is quantized again in the same type. A blob the adapter does not touch
// is copied byte for byte. Nothing downstream changes: the result is another `experts` range with the same layout.
#pragma once

#include "basalt/core/lora.hpp"
#include "basalt/core/model_file.hpp"
#include "basalt/kernels/cpu/expert_layout.hpp"

#include <cstdint>
#include <string>

namespace basalt::core {

struct ExpertFuseStats {
    uint64_t blobs_fused = 0;   ///< experts with at least one row changed
    uint64_t bytes = 0;         ///< the fused section's size
    int layers = 0;             ///< layers the adapters carry experts for
    double seconds = 0;
    bool reused = false;        ///< the file was already there from an earlier load
};

/// Whether this engine can quantize a row of `type` (`cols` values) back after adding a delta.
bool lora_experts_type_ok(int type, int64_t cols);

/// The file the fused experts for these inputs are kept in: beside the first adapter that carries experts, named
/// from a fingerprint of the experts section, its layout and every adapter with its scale.
std::string lora_experts_path(const LoraSet& set, const FileRange& experts, const kernels::cpu::ExpertLayout& layout);

/// Writes the fused experts section to `path` (a whole file, `experts.bytes` long). False with `err` when a layer an
/// adapter touches is not in a format this engine can quantize, when an adapter's shapes disagree with the layout,
/// or when the files cannot be read or written.
bool lora_fuse_experts(const LoraSet& set, const FileRange& experts, const kernels::cpu::ExpertLayout& layout,
                       int threads, const std::string& path, ExpertFuseStats& stats, std::string& err);

/// The experts range to load: `experts` itself when no adapter carries routed experts, else the fused file for these
/// inputs (written now, or kept from an earlier load). Fused files of the same adapter left from other inputs are removed.
bool lora_prepare_experts(const LoraSet& set, const FileRange& experts, const kernels::cpu::ExpertLayout& layout,
                          int threads, FileRange& out, ExpertFuseStats& stats, std::string& err);

}  // namespace basalt::core
