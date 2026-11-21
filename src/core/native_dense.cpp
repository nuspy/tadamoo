#include "basalt/core/native_dense.hpp"
#include "basalt/core/direct_upload.hpp"
#include "basalt/core/load_progress.hpp"
#include <cstdlib>
#include "basalt/core/lora.hpp"
#include "basalt/core/native_matrix_family.hpp"
#include "basalt/core/weights.hpp"
#include "basalt/artifact/gguf_reader.hpp"
#include "basalt/kernels/native_mmvq.hpp"
#include "basalt/platform/memory.hpp"

#include <cuda_runtime.h>
#include <algorithm>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <limits>
#include <memory>
#include <set>

namespace basalt::core {
namespace {
int g_layer_lb = -1, g_layer_le = -1;   // set_layer_range; -1: every layer
bool in_range(const std::string& name) {
    if (g_layer_lb < 0 || name.rfind("blk.", 0) != 0) return true;
    const int l = std::atoi(name.c_str() + 4);
    return l >= g_layer_lb && l < g_layer_le;
}
bool eligible(const basalt::TensorInfo& tensor, bool include_ple_key) {
    const auto& name = tensor.name;
    if (name.rfind("blk.", 0) != 0) return false;
    // Match the native PLE kernel: Q2_0, IQ3_XXS, IQ4_XS and Q8_0 (UD-Q4_K_XL). Other keys retain the packed BF16
    // fallback.
    if (name == "blk.1.ple_key.weight")
        return include_ple_key && (tensor.type == 42 || tensor.type == 18 || tensor.type == 23 || tensor.type == 8);
    return is_native_matrix_family(name);
}
const LoraSet* g_lora_set = nullptr;   // set_lora; nullptr: fuse nothing (today's behaviour, unchanged)
constexpr int kQ8_0 = 8;
// The type NativeDense treats `tensor` as: Q8_0 (what the merge always produces) when `set_lora` covers it,
// its own stored type otherwise. false (with `err`) only on an adapter/base shape disagreement.
bool effective_type(const basalt::TensorInfo& tensor, int& type, bool& covered, std::string& err) {
    type = (int) tensor.type;
    covered = false;
    if (!g_lora_set || g_lora_set->empty() || tensor.shape.size() != 2) return true;
    bool found = false;
    if (!g_lora_set->covers(tensor.name, (int64_t) tensor.shape[0], (int64_t) tensor.shape[1], found, err)) return false;
    if (found) { type = kQ8_0; covered = true; }
    return true;
}
struct DeviceFree { void operator()(void* p) const { if (p) cudaFree(p); } };
using DevicePtr = std::unique_ptr<void, DeviceFree>;
struct Pending {
    WeightRef* ref;
    int type;
    uint64_t bytes;
    void* data;       // inside the slab, or the owned allocation below when the slab ran out
    DevicePtr owned;
};
}

bool NativeDense::served_names(const std::vector<std::string>& shards, bool include_ple_key,
                               std::set<std::string>& out, std::string& err) {
    try {
        for (const auto& path : shards) {
            basalt::GgufFile gguf(path);
            for (const auto& tensor : gguf.tensors()) {
                if (!eligible(tensor, include_ple_key) || tensor.shape.size() != 2) continue;
                int type = 0;
                bool covered = false;
                if (!effective_type(tensor, type, covered, err)) return false;
                if (basalt::kernels::native_mmvq_supported(type)) out.insert(tensor.name);
            }
        }
        return true;
    } catch (const std::exception& error) {
        err = std::string("native dense: ") + error.what();
        return false;
    }
}

bool NativeDense::plan_bytes(const std::vector<std::string>& shards, bool include_ple_key, int64_t layer_lo,
                             int64_t layer_hi, uint64_t& out, std::string& err) {
    auto outside = [&](const std::string& name) {   // a blk.<l>. tensor of another stage's layers
        if (layer_hi < 0 || name.rfind("blk.", 0) != 0) return false;
        const long l = std::strtol(name.c_str() + 4, nullptr, 10);
        return l < layer_lo || l >= layer_hi;
    };
    out = 0;
    try {
        for (const auto& path : shards) {
            basalt::GgufFile gguf(path);
            for (const auto& tensor : gguf.tensors()) {
                if (!eligible(tensor, include_ple_key) || outside(tensor.name)) continue;
                if (!in_range(tensor.name) && tensor.name.find("ple") == std::string::npos) continue;
                if (tensor.shape.size() != 2 || tensor.shape[0] == 0 || tensor.shape[0] > INT_MAX ||
                    tensor.shape[1] == 0 || tensor.shape[1] > INT_MAX)
                    continue;
                int type = 0;
                bool covered = false;
                if (!effective_type(tensor, type, covered, err)) return false;
                if (!basalt::kernels::native_mmvq_supported(type)) continue;
                out += basalt::kernels::native_mmvq_weight_bytes(type, (int) tensor.shape[0], (int) tensor.shape[1]);
            }
        }
        return true;
    } catch (const std::exception& error) {
        err = std::string("native dense: ") + error.what();
        return false;
    }
}

bool NativeDense::plan_bytes_by_layer(const std::vector<std::string>& shards, bool include_ple_key, int64_t n_layers,
                                      std::vector<uint64_t>& per_layer, uint64_t& shared, std::string& err) {
    per_layer.assign((size_t) n_layers, 0);
    shared = 0;
    try {
        for (const auto& path : shards) {
            basalt::GgufFile gguf(path);
            for (const auto& tensor : gguf.tensors()) {
                if (!eligible(tensor, include_ple_key) || tensor.shape.size() != 2 || tensor.shape[0] == 0 ||
                    tensor.shape[0] > INT_MAX || tensor.shape[1] == 0 || tensor.shape[1] > INT_MAX)
                    continue;
                int type = 0;
                bool covered = false;
                if (!effective_type(tensor, type, covered, err)) return false;
                if (!basalt::kernels::native_mmvq_supported(type)) continue;
                const uint64_t bytes = basalt::kernels::native_mmvq_weight_bytes(type, (int) tensor.shape[0],
                                                                                 (int) tensor.shape[1]);
                const long l = std::strtol(tensor.name.c_str() + 4, nullptr, 10);
                if (tensor.name.find("ple") == std::string::npos && l >= 0 && l < n_layers) per_layer[(size_t) l] += bytes;
                else shared += bytes;
            }
        }
        return true;
    } catch (const std::exception& error) {
        err = std::string("native dense: ") + error.what();
        return false;
    }
}

void NativeDense::set_layer_range(int lb, int le) { g_layer_lb = lb; g_layer_le = le; }

bool NativeDense::set_lora(const LoraSet* set, bool allow_partial, std::string& err) {
    if (set) {
        auto share_text = [](const LoraAdapter& a, const char* group) {
            const auto v = a.energy_share(group);
            if (!v) return std::string("not known");
            char buf[16];
            std::snprintf(buf, sizeof buf, "%.0f%%", (double) *v * 100.0);
            return std::string(buf);
        };
        for (const auto& entry : set->entries()) {
            const LoraAdapter& a = *entry.adapter;
            if (a.router_count() || a.expert_count() || a.other_count()) {
                const std::string detail = a.path() + ": touches " + std::to_string(a.router_count()) +
                      " router, " + std::to_string(a.expert_count()) + " expert and " +
                      std::to_string(a.other_count()) +
                      " other tensor(s) outside the ten families this engine fuses; energy share router=" +
                      share_text(a, "router") + " experts=" + share_text(a, "experts") + " other=" +
                      share_text(a, "other");
                if (!allow_partial) {
                    err = detail;
                    return false;
                }
                basalt::log::warn("basalt lora", "%s; fusing the ten covered families only, the rest of this "
                                     "adapter's update is not applied",
                             detail.c_str());
            }
        }
    }
    g_lora_set = set;
    return true;
}
bool NativeDense::keep_unquantized_ple_key(const PackFiles& pack, std::set<std::string>& skip, std::string& err) {
    const std::string key = "blk.1.ple_key.weight";
    if (!skip.count(key)) return true;
    int code_bits = -1;
    if (!WeightTable::index_code_bits(pack, key, code_bits, err)) return false;
    if (code_bits == 0) skip.erase(key);
    return true;
}

NativeDense::~NativeDense() {
    if (scratch_) cudaFree(scratch_);
    for (void* p : allocs_) cudaFree(p);
}

namespace {
constexpr uint64_t kSlabAlign = 256;   // matrices start at 256-byte offsets, as separate allocations do
}
uint64_t NativeDense::slab_bytes(uint64_t plan_total) { return plan_total + kSlabAlign * 1024; }

bool NativeDense::load(const std::vector<std::string>& shards, WeightTable& table, std::string& err,
                       bool include_ple_key, int64_t layer_lo, int64_t layer_hi, void* slab_at, uint64_t slab_cap) {
    auto outside = [&](const std::string& name) {   // a blk.<l>. tensor of another stage's layers
        if (layer_hi < 0 || name.rfind("blk.", 0) != 0) return false;
        const long l = std::strtol(name.c_str() + 4, nullptr, 10);
        return l < layer_lo || l >= layer_hi;
    };
    if (scratch_ || !weights_.empty()) { err = "native dense: already loaded"; return false; }
    if (shards.empty()) { err = "native dense: at least one GGUF shard is required"; return false; }
    try {
        std::vector<Pending> pending;
        std::set<std::string> seen;
        // the bytes the uploads below will write (for the progress lines)
        uint64_t plan_total = 0;
        if (!plan_bytes(shards, include_ple_key, layer_lo, layer_hi, plan_total, err)) return false;
        int load_dev = 0;
        if (cudaGetDevice(&load_dev) != cudaSuccess) load_dev = 0;
        LoadProgress progress(LoadProgress::cuda(load_dev), plan_total);
        DirectUploader uploader;   // unbuffered reads, overlapped with the copies
        // One allocation holds every matrix: separate cudaMalloc calls each round up to the allocator's granularity,
        // which costs about a MiB per matrix.  Matrices start at 256-byte offsets, as separate allocations do.
        constexpr uint64_t kAlign = kSlabAlign;
        const uint64_t slab_bytes = NativeDense::slab_bytes(plan_total);
        if (slab_at != nullptr && slab_cap < slab_bytes) {
            err = "native dense: the block given for the matrices is too small";
            return false;
        }
        DevicePtr slab;
        void* slab_base = slab_at;
        if (slab_base == nullptr) {
            void* a = nullptr;
            if (plan_total > 0 && cudaMalloc(&a, slab_bytes) == cudaSuccess) slab.reset(a);
            else (void) cudaGetLastError();
            slab_base = slab.get();
        }
        uint64_t slab_used = 0;
        int max_in = 0;
        uint64_t total = 0;
        uint64_t split_count = 0, split_tensors = 0;
        std::set<uint64_t> split_numbers;
        bool have_architecture = false;
        for (const auto& path : shards) {
            basalt::GgufFile gguf(path);
            const auto* count = gguf.get("split.count");
            const auto* number = gguf.get("split.no");
            const auto* tensors = gguf.get("split.tensors.count");
            if (gguf.get("general.architecture")) {
                err = basalt::check_architecture(gguf);
                if (!err.empty()) return false;
                have_architecture = true;
                if (count && number && tensors && number->u == 0 && count->u > 1) {
                    split_count = count->u;
                    split_tensors = tensors->u;
                }
            } else if (!have_architecture || !split_count || !count || !number || !tensors ||
                       count->u != split_count || number->u == 0 || number->u >= split_count ||
                       tensors->u != split_tensors) {
                err = "native dense: additional shard must match the architecture-validated first shard's split metadata";
                return false;
            }
            if (number && !split_numbers.insert(number->u).second) {
                err = "native dense: duplicate split shard number"; return false;
            }
            std::vector<uint64_t> offsets;
            for (const auto& tensor : gguf.tensors()) offsets.push_back(tensor.offset);
            std::sort(offsets.begin(), offsets.end());
            if (std::adjacent_find(offsets.begin(), offsets.end()) != offsets.end()) {
                err = "native dense: tensor payload offsets overlap"; return false;
            }
            // Validate every directory span, including tensors we do not upload:
            // an ignored tensor must not overlap the native matrix that follows it.
            const uint64_t payload = gguf.file_size() - gguf.data_start();
            for (const auto& tensor : gguf.tensors()) {
                int block_elements = 0, block_bytes = 0;
                uint64_t elements = 1;
                if (tensor.shape.empty() || !basalt::block_geometry(tensor.type, block_elements, block_bytes) ||
                    tensor.shape[0] % (uint64_t) block_elements != 0) {
                    err = "native dense: invalid block geometry " + tensor.name; return false;
                }
                for (uint64_t dimension : tensor.shape) {
                    if (!dimension || elements > (std::numeric_limits<uint64_t>::max)() / dimension) {
                        err = "native dense: invalid tensor extent " + tensor.name; return false;
                    }
                    elements *= dimension;
                }
                const uint64_t blocks = elements / (uint64_t) block_elements;
                if (blocks > (std::numeric_limits<uint64_t>::max)() / (uint64_t) block_bytes) {
                    err = "native dense: tensor byte count overflow " + tensor.name; return false;
                }
                const uint64_t bytes = blocks * (uint64_t) block_bytes;
                if (tensor.offset > payload || bytes > payload - tensor.offset) {
                    err = "native dense: truncated payload " + tensor.name; return false;
                }
                const auto next = std::upper_bound(offsets.begin(), offsets.end(), tensor.offset);
                if (next != offsets.end() && bytes > *next - tensor.offset) {
                    err = "native dense: overlapping payload " + tensor.name; return false;
                }
            }
            for (const auto& tensor : gguf.tensors()) {
                if (!eligible(tensor, include_ple_key) || outside(tensor.name)) continue;
                if (!in_range(tensor.name) && tensor.name.find("ple") == std::string::npos) continue;
                if (!seen.insert(tensor.name).second) {
                    err = "native dense: duplicate tensor " + tensor.name; return false;
                }
                auto found = table.table_.find(tensor.name);
                if (found == table.table_.end()) {
                    err = "native dense: tensor absent from canonical table: " + tensor.name; return false;
                }
                auto& ref = found->second;
                if (ref.native_data) { err = "native dense: override already attached"; return false; }
                int type = 0;
                bool lora_covered = false;
                if (!effective_type(tensor, type, lora_covered, err)) return false;
                if (lora_covered && !ref.quantized()) {
                    err = "native dense: " + tensor.name + " is covered by a LoRA adapter but is not quantized in "
                          "the pack (BF16 or F32), not handled";
                    return false;
                }
                if (!basalt::kernels::native_mmvq_supported(type)) continue;
                // the file keeps an unquantized key here, which the PLE reads from the arena
                if (tensor.name == "blk.1.ple_key.weight" && !ref.quantized()) continue;
                if (!ref.quantized() || tensor.shape.size() != 2 ||
                    ref.ne0 <= 0 || ref.ne0 > INT_MAX || ref.ne1 <= 0 || ref.ne1 > INT_MAX ||
                    tensor.shape[0] != (uint64_t) ref.ne0 || tensor.shape[1] != (uint64_t) ref.ne1) {
                    err = "native dense: incompatible matrix " + tensor.name; return false;
                }
                const auto bytes = basalt::kernels::native_mmvq_weight_bytes(type, (int) ref.ne0, (int) ref.ne1);
                void* where = nullptr;
                DevicePtr own;
                if (slab_base != nullptr && slab_used + bytes <= slab_bytes) {
                    where = (char*) slab_base + slab_used;
                    slab_used += (bytes + kAlign - 1) / kAlign * kAlign;
                } else if (slab_at != nullptr) {
                    err = "native dense: the matrices outgrow the block given for them";
                    return false;
                } else {
                    void* allocation = nullptr;
                    if (cudaMalloc(&allocation, bytes) == cudaSuccess) own.reset(allocation);
                    where = allocation;
                }
                if (where == nullptr) { err = "native dense upload " + tensor.name + ": no device memory"; return false; }
                if (lora_covered) {
                    // the base blocks come from the mapped file (tensor_data), not a device-side upload: the
                    // merge needs them on the host, and the result is already in RAM to copy from
                    const auto lora_t0 = std::chrono::steady_clock::now();
                    std::vector<uint8_t> fused;
                    std::string merge_err;
                    if (!lora_merge_q8_0((uint32_t) tensor.type, gguf.tensor_data(tensor), ref.ne0, ref.ne1,
                                         *g_lora_set, tensor.name, fused, merge_err)) {
                        err = "native dense lora merge " + tensor.name + ": " + merge_err;
                        return false;
                    }
                    if (fused.size() != bytes) {
                        err = "native dense lora merge " + tensor.name + ": fused " + std::to_string(fused.size()) +
                              " bytes, expected " + std::to_string(bytes);
                        return false;
                    }
                    const auto cuda_err = cudaMemcpy(where, fused.data(), bytes, cudaMemcpyHostToDevice);
                    if (cuda_err != cudaSuccess) {
                        err = "native dense lora upload " + tensor.name + ": " + cudaGetErrorString(cuda_err);
                        return false;
                    }
                    lora_seconds_ += std::chrono::duration<double>(std::chrono::steady_clock::now() - lora_t0).count();
                    ++lora_fused_count_;
                    int pack_be = 0, pack_bb = 0;
                    if (basalt::block_geometry(tensor.type, pack_be, pack_bb)) {
                        const uint64_t pack_bytes = (uint64_t) ref.ne0 * (uint64_t) ref.ne1 / (uint64_t) pack_be *
                                                    (uint64_t) pack_bb;
                        lora_extra_bytes_ += (int64_t) bytes - (int64_t) pack_bytes;
                    }
                } else {
                    std::string upload_err;
                    if (!uploader.upload(gguf.path(), gguf.data_start() + tensor.offset, bytes, where, upload_err)) {
                        err = "native dense upload " + tensor.name + ": " + upload_err;
                        return false;
                    }
                }
                max_in = (std::max)(max_in, (int) ref.ne0);
                total += bytes;
                progress.add(bytes);
                pending.push_back(Pending{&ref, type, bytes, where, std::move(own)});
            }
        }
        if (pending.empty()) { err = "native dense: no supported GDN/QSA matrices in supplied shards"; return false; }
        void* allocation = nullptr;
        const auto status = cudaMalloc(&allocation, basalt::kernels::native_q8_1_bytes(max_in));
        DevicePtr scratch(allocation);
        if (status != cudaSuccess) { err = std::string("native dense scratch: ") + cudaGetErrorString(status); return false; }
        // All checks and allocations finish before publishing any reference.
        weights_.reserve(pending.size());
        for (auto& item : pending) {
            item.ref->native_data = item.data;
            item.ref->native_type = item.type;
            item.ref->native_q8_1 = scratch.get();
            weights_.push_back(item.data);
            if (item.owned) allocs_.push_back(item.owned.release());
        }
        if (slab) allocs_.push_back(slab.release());
        scratch_ = scratch.release();
        bytes_ = total;
        progress.finish();
        return true;
    } catch (const std::exception& error) {
        err = std::string("native dense: ") + error.what();
        return false;
    }
}
} // namespace basalt::core
