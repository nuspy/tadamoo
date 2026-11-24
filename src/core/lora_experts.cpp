#include "basalt/core/lora_experts.hpp"

#include "basalt/ggml/ggml.h"
#include "basalt/kernels/cpu/native_expert.hpp"
#include "basalt/log.hpp"
#include "basalt/platform/file.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

namespace basalt::core {

namespace {

using kernels::cpu::ExpertLayout;
using kernels::cpu::NativeFmt;

constexpr ExpertRole kRoles[] = {ExpertRole::Gate, ExpertRole::Up, ExpertRole::Down};
const char* const kRoleNames[] = {"gate", "up", "down"};

// One matrix of an expert blob: its ggml type, shape (rows = outputs, cols = inputs), bytes per row and place.
struct Geometry {
    int type = 0;
    int64_t rows = 0, cols = 0;
    size_t row_bytes = 0, offset = 0;
};

Geometry geometry(const NativeFmt& f, ExpertRole role) {
    switch (role) {
        case ExpertRole::Gate: return {f.gu_type, f.n_ff, f.n_embd, f.gu_row, 0};
        case ExpertRole::Up:   return {f.up_type >= 0 ? f.up_type : f.gu_type, f.n_ff, f.n_embd, f.up_row != 0 ? f.up_row : f.gu_row, f.up_off};
        default:               return {f.d_type, f.n_embd, f.n_ff, f.d_row, f.down_off};
    }
}

// One adapter's pair for one expert matrix and the coefficient it enters with.
struct Term {
    LoraExpertView v;
    float coef = 0;
};

struct Scratch {
    std::vector<float> delta, row;
};

// Adds the terms' deltas to every row of the matrix at `base` that they change, quantizing each such row again.
// False when the quantizer wrote an unexpected number of bytes.
bool fuse_matrix(const Geometry& g, uint8_t* base, const std::vector<Term>& terms, Scratch& s, bool& changed) {
    const struct ggml_type_traits* traits = ggml_get_type_traits((enum ggml_type) g.type);
    s.delta.resize((size_t) g.cols);
    s.row.resize((size_t) g.cols);
    for (int64_t i = 0; i < g.rows; ++i) {
        bool any = false;
        std::fill(s.delta.begin(), s.delta.end(), 0.0f);
        for (const Term& t : terms) {
            const float* b_row = t.v.b + (size_t) i * (size_t) t.v.r;
            for (int64_t k = 0; k < t.v.r; ++k) {
                const float c = t.coef * b_row[k];
                if (c == 0.0f) continue;
                any = true;
                const float* a_row = t.v.a + (size_t) k * (size_t) t.v.n_in;
                for (int64_t j = 0; j < g.cols; ++j) s.delta[(size_t) j] += c * a_row[j];
            }
        }
        if (!any) continue;
        uint8_t* row = base + (size_t) i * g.row_bytes;
        traits->to_float(row, s.row.data(), g.cols);
        for (int64_t j = 0; j < g.cols; ++j) s.row[(size_t) j] += s.delta[(size_t) j];
        if (ggml_quantize_chunk((enum ggml_type) g.type, s.row.data(), row, 0, 1, g.cols) != g.row_bytes) return false;
        changed = true;
    }
    return true;
}

uint64_t fnv(uint64_t h, const void* data, size_t n) {
    const uint8_t* p = (const uint8_t*) data;
    for (size_t i = 0; i < n; ++i) h = (h ^ p[i]) * 1099511628211ull;
    return h;
}
uint64_t fnv(uint64_t h, const std::string& s) { return fnv(fnv(h, s.data(), s.size()), "\0", 1); }
uint64_t fnv(uint64_t h, uint64_t v) { return fnv(h, &v, sizeof v); }

void split_path(const std::string& path, std::string& dir, std::string& name) {
    const size_t cut = path.find_last_of("/\\");
    dir = cut == std::string::npos ? "." : path.substr(0, cut);
    name = cut == std::string::npos ? path : path.substr(cut + 1);
}

// A file's size and last write time, as the directory lists them: what tells a replaced file from the one before.
std::string identity(const std::string& path) {
    std::string dir, name;
    split_path(path, dir, name);
    std::vector<platform::DirFile> files;
    if (platform::list_files(dir, files))
        for (const auto& f : files)
            if (f.name == name) return std::to_string(f.bytes) + ":" + std::to_string(f.mtime);
    return "?";
}

const LoraEntry* first_with_experts(const LoraSet& set) {
    for (const auto& e : set.entries())
        if (e.adapter->expert_count() > 0) return &e;
    return nullptr;
}

std::string hex16(uint64_t v) {
    char buf[17];
    std::snprintf(buf, sizeof buf, "%016llx", (unsigned long long) v);
    return buf;
}

}  // namespace

bool lora_experts_type_ok(int type, int64_t cols) {
    const struct ggml_type_traits* t = ggml_get_type_traits((enum ggml_type) type);
    if (!t || !t->to_float || t->blck_size <= 0 || cols % t->blck_size != 0) return false;
    return type == (int) GGML_TYPE_IQ4_NL || type == (int) GGML_TYPE_IQ4_XS || type == (int) GGML_TYPE_IQ2_S ||
           type == (int) GGML_TYPE_IQ3_XXS || type == (int) GGML_TYPE_IQ3_S || t->from_float_ref != nullptr;
}

std::string lora_experts_path(const LoraSet& set, const FileRange& experts, const ExpertLayout& layout) {
    const LoraEntry* first = first_with_experts(set);
    if (!first) return std::string();
    uint64_t h = 1469598103934665603ull;
    h = fnv(h, std::string("lora experts v1"));
    h = fnv(h, experts.path);
    h = fnv(h, experts.offset);
    h = fnv(h, experts.bytes);
    h = fnv(h, identity(experts.path));
    for (size_t l = 0; l < layout.fmt.size(); ++l) {
        h = fnv(h, (uint64_t) layout.fmt[l].gu_type);
        h = fnv(h, (uint64_t) layout.fmt[l].up_type);
        h = fnv(h, (uint64_t) layout.fmt[l].d_type);
        h = fnv(h, layout.bytes[l]);
    }
    for (const auto& e : set.entries()) {
        h = fnv(h, e.adapter->path());
        h = fnv(h, identity(e.adapter->path()));
        uint32_t bits = 0;
        std::memcpy(&bits, &e.scale, sizeof bits);
        h = fnv(h, (uint64_t) bits);
    }
    std::string dir, name;
    split_path(first->adapter->path(), dir, name);
    return dir + "/" + name + "." + hex16(h) + ".experts";
}

bool lora_fuse_experts(const LoraSet& set, const FileRange& experts, const ExpertLayout& layout, int threads,
                       const std::string& path, ExpertFuseStats& stats, std::string& err) {
    const auto t0 = std::chrono::steady_clock::now();
    stats = ExpertFuseStats();
    if (!layout.native || layout.fmt.empty()) {
        err = "the experts are not in the native layout, which expert LoRA fusion needs";
        return false;
    }
    const int64_t n_layers = (int64_t) layout.fmt.size(), n_expert = layout.n_expert;
    if (experts.bytes != layout.total) {
        err = "the experts section is " + std::to_string(experts.bytes) + " bytes, its layout says " +
              std::to_string(layout.total);
        return false;
    }

    // Which layers the adapters touch, and whether this engine can write each of their matrices back.
    std::vector<uint8_t> touched((size_t) n_layers, 0);
    for (int64_t l = 0; l < n_layers; ++l) {
        for (const auto& e : set.entries()) {
            for (int r = 0; r < 3; ++r) {
                if (!e.adapter->has_experts(l, kRoles[r])) continue;
                touched[(size_t) l] = 1;
                const Geometry g = geometry(layout.fmt[(size_t) l], kRoles[r]);
                if (layout.fmt[(size_t) l].tail != 0 || !lora_experts_type_ok(g.type, g.cols)) {
                    const char* tn = ggml_type_name((enum ggml_type) g.type);
                    err = "layer " + std::to_string(l) + "'s " + kRoleNames[r] + " experts are " + (tn ? tn : "an unknown type") +
                          ": this engine cannot quantize that type again after a LoRA delta";
                    return false;
                }
                ggml_quantize_init((enum ggml_type) g.type);   // before any worker quantizes
            }
        }
    }
    for (int64_t l = 0; l < n_layers; ++l) stats.layers += touched[(size_t) l];
    for (const auto& e : set.entries())
        if (e.adapter->expert_layers() > n_layers) {
            err = e.adapter->path() + " has experts for layer " + std::to_string(e.adapter->expert_layers() - 1) +
                  ", the model has " + std::to_string(n_layers) + " layers";
            return false;
        }

    const std::string part = path + ".part";
    const int src = platform::open_fd_utf8(experts.path);
    if (src < 0) { err = "cannot open " + experts.path; return false; }
    const int dst = platform::open_write_utf8(part);
    if (dst < 0) {
        platform::close_fd(src);
        err = "cannot write " + part;
        return false;
    }

    const int64_t total = n_layers * n_expert;
    std::atomic<int64_t> next{0}, done{0}, fused{0};
    std::atomic<int> last_pct{0};
    std::atomic<bool> failed{false};
    std::mutex err_mu;
    std::string first_err;
    auto fail = [&](const std::string& what) {
        std::lock_guard<std::mutex> lock(err_mu);
        if (!failed.exchange(true)) first_err = what;
    };

    auto worker = [&]() {
        std::vector<uint8_t> blob;
        Scratch scratch;
        std::vector<Term> terms;
        for (;;) {
            const int64_t item = next.fetch_add(1);
            if (item >= total || failed.load()) return;
            const int64_t l = item / n_expert, e = item % n_expert;
            const NativeFmt& f = layout.fmt[(size_t) l];
            const uint64_t bytes = layout.blob_bytes(l), at = layout.blob_offset(l, e);
            blob.resize((size_t) bytes);
            if (!platform::pread_all(src, blob.data(), bytes, experts.offset + at)) {
                fail("cannot read expert " + std::to_string(e) + " of layer " + std::to_string(l) + " from " + experts.path);
                return;
            }
            if (touched[(size_t) l]) {
                bool changed = false;
                for (int r = 0; r < 3; ++r) {
                    const Geometry g = geometry(f, kRoles[r]);
                    terms.clear();
                    for (const auto& entry : set.entries()) {
                        Term t;
                        if (!entry.adapter->expert(l, kRoles[r], e, t.v)) continue;
                        if (t.v.n_in != g.cols || t.v.n_out != g.rows) {
                            fail(entry.adapter->path() + ": layer " + std::to_string(l) + " " + kRoleNames[r] +
                                 " experts are " + std::to_string(t.v.n_out) + "x" + std::to_string(t.v.n_in) +
                                 ", the model's are " + std::to_string(g.rows) + "x" + std::to_string(g.cols));
                            return;
                        }
                        t.coef = entry.scale * (entry.adapter->alpha() / (float) t.v.r);
                        terms.push_back(t);
                    }
                    if (terms.empty()) continue;
                    if (!fuse_matrix(g, blob.data() + g.offset, terms, scratch, changed)) {
                        fail("layer " + std::to_string(l) + " expert " + std::to_string(e) + ": the quantizer wrote an unexpected size");
                        return;
                    }
                }
                if (changed) fused.fetch_add(1);
            }
            if (!platform::pwrite_all(dst, blob.data(), bytes, at)) {
                fail("cannot write " + part);
                return;
            }
            const int64_t n = done.fetch_add(1) + 1;
            const int pct = (int) (n * 100 / total);
            int seen = last_pct.load();
            while (pct >= seen + 10 && !last_pct.compare_exchange_weak(seen, pct / 10 * 10)) {}
            if (pct >= seen + 10) {
                basalt::log::info("basalt lora", "experts %d%% (%.0f s)", pct / 10 * 10,
                                  std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count());
            }
        }
    };

    std::vector<std::thread> pool;
    const int n_threads = std::max(1, threads);
    for (int i = 1; i < n_threads; ++i) pool.emplace_back(worker);
    worker();
    for (auto& t : pool) t.join();
    platform::close_fd(src);
    const bool synced = !failed.load() && platform::sync_fd(dst);
    platform::close_fd(dst);
    if (failed.load()) {
        platform::remove_file(part);
        err = first_err;
        return false;
    }
    if (!synced || !platform::rename_replace(part, path)) {
        platform::remove_file(part);
        err = "cannot finish " + path;
        return false;
    }
    stats.blobs_fused = (uint64_t) fused.load();
    stats.bytes = experts.bytes;
    stats.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    return true;
}

bool lora_prepare_experts(const LoraSet& set, const FileRange& experts, const ExpertLayout& layout, int threads,
                          FileRange& out, ExpertFuseStats& stats, std::string& err) {
    stats = ExpertFuseStats();
    out = experts;
    if (!set.touches_experts()) return true;
    const std::string path = lora_experts_path(set, experts, layout);
    std::string dir, name;
    split_path(path, dir, name);
    uint64_t have = 0;
    if (platform::file_size(path, have) && have == experts.bytes) {
        stats.reused = true;
        stats.bytes = have;
    } else {
        const uint64_t free_bytes = platform::disk_free_bytes(dir);
        if (free_bytes != 0 && free_bytes < experts.bytes) {
            err = "the fused experts need " + std::to_string(experts.bytes >> 30) + " GiB in " + dir +
                  ", the disk has " + std::to_string(free_bytes >> 30) + " GiB free";
            return false;
        }
        if (!lora_fuse_experts(set, experts, layout, threads, path, stats, err)) return false;
        // The fused files this adapter left from other inputs (an older version of it, another scale) are of no use.
        const std::string stem = name.substr(0, name.size() - 16 - 8);   // "<adapter file>." without hash and ".experts"
        auto ends = [](const std::string& s, const char* suffix) {
            const size_t n = std::strlen(suffix);
            return s.size() >= n && s.compare(s.size() - n, n, suffix) == 0;
        };
        std::vector<platform::DirFile> files;
        if (platform::list_files(dir, files))
            for (const auto& f : files)
                if (f.name != name && f.name.rfind(stem, 0) == 0 && (ends(f.name, ".experts") || ends(f.name, ".experts.part")))
                    platform::remove_file(dir + "/" + f.name);
    }
    out = FileRange(path, 0, experts.bytes);
    return true;
}

}  // namespace basalt::core
