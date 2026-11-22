// LoRA adapter reading (LoraAdapter::open) and merge (lora_merge_q8_0): no GPU, no real checkpoint.
//
// 1. open(): a well-formed synthetic adapter opens, alpha and the pairs read back exactly; a tensor outside the
//    ten native families (native_dense.cpp's eligible()) is counted, not loaded; four malformed files are
//    rejected, each error naming the thing that is wrong.
// 2. lora_merge_q8_0(): for each of the dense types the real packs use (Q4_K, Q5_K, Q6_K, IQ4_XS, IQ4_NL, IQ3_S,
//    Q3_K, Q2_0, Q8_0), at the real geometries (2560 -> 10240, 6144 -> 2560), with one or two adapters and
//    varying rank, every merged value is within the Q8_0 block step (plus the scale's own fp16 rounding) of a
//    double-precision reference.
// 3. On a Q8_0 base specifically: a null delta is a byte copy of the base, even for a block whose code was 126
//    rather than 127 (re-quantizing such a block is not its own fixed point); the same adapter at scale +1 and
//    -1 together gives a net-zero delta and the same base bytes; two runs of the same input give the same bytes.
// 4. A microbench line: seconds to fuse one 2560 -> 10240 tensor (sizes the cost of re-fusing at runtime).
// 5. The committed sample (tools/convert/testdata/lora_sample_expected.gguf, a file lora.py actually wrote, not
//    a fixture this file built): alpha, both tensors' shapes and values, energy shares and router/expert/other
//    counts, all read back by this C++ reader. Closes the loop the rest of this file cannot: it never gives the
//    C++ reader a file the Python writer produced, and tools/convert/test_lora.py never gives the Python reader
//    a file this C++ writer produced (there is none) -- so nothing before this proved the two sides agree.
#include "basalt/core/lora.hpp"
#include "basalt/ggml/ggml.h"
#include "gguf_fixture.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <map>
#include <random>
#include <vector>

namespace {

int failures = 0;
void check(bool ok, const std::string& what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what.c_str());
        ++failures;
    }
}

struct Pair {
    std::vector<float> a, b;   // a[k * n_in + j], b[i * r + k] -- the checkpoint-order layout LoraAdapter reads
    int64_t r = 0, n_in = 0, n_out = 0;
};

std::vector<uint8_t> f32_bytes(const std::vector<float>& v) {
    std::vector<uint8_t> out(v.size() * 4);
    std::memcpy(out.data(), v.data(), out.size());
    return out;
}

fixture::Tensor lora_a_tensor(const std::string& base, const Pair& p) {
    return {base + ".lora_a", {(uint64_t) p.n_in, (uint64_t) p.r}, 0 /* F32 */, 1, f32_bytes(p.a)};
}
fixture::Tensor lora_b_tensor(const std::string& base, const Pair& p) {
    return {base + ".lora_b", {(uint64_t) p.r, (uint64_t) p.n_out}, 0 /* F32 */, 1, f32_bytes(p.b)};
}

std::filesystem::path write_adapter(const std::filesystem::path& dir, const std::string& name, float alpha,
                                    const std::map<std::string, Pair>& pairs,
                                    const std::vector<fixture::Kv>& extra_kv = {}) {
    std::vector<fixture::Kv> kv = {fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                                   fixture::str("general.architecture", "qwen4exp"),
                                   fixture::f32("adapter.lora.alpha", alpha)};
    kv.insert(kv.end(), extra_kv.begin(), extra_kv.end());
    std::vector<fixture::Tensor> ts;
    for (const auto& [base, p] : pairs) {
        ts.push_back(lora_a_tensor(base, p));
        ts.push_back(lora_b_tensor(base, p));
    }
    const auto path = dir / name;
    fixture::write(path, kv, ts);
    return path;
}

Pair random_pair(std::mt19937& rng, int64_t r, int64_t n_in, int64_t n_out, float sigma) {
    std::normal_distribution<float> dist(0.0f, sigma);
    Pair p;
    p.r = r;
    p.n_in = n_in;
    p.n_out = n_out;
    p.a.resize((size_t) (r * n_in));
    p.b.resize((size_t) (n_out * r));
    for (auto& v : p.a) v = dist(rng);
    for (auto& v : p.b) v = dist(rng);
    return p;
}

// The byte offset(s) of `type`'s own fp16 scale field(s) within one block, read from ggml's real block structs
// (block_q4_K/block_q5_K: d then dmin at bytes 0-3; block_q6_K: d at 208-209; block_q3_K: d at 108-109; the
// legacy/IQ formats: d at 0-1). Fixing these to a small value keeps every random block finite and of a known
// order of magnitude, regardless of what the rest of the block's (otherwise random) bytes decode to.
void fix_scale_field(uint32_t type, uint8_t* block) {
    const uint16_t d = ggml_fp32_to_fp16(0.03f);
    auto put = [&](size_t off) { std::memcpy(block + off, &d, 2); };
    switch (type) {
    case GGML_TYPE_Q4_K:
    case GGML_TYPE_Q5_K:
        put(0);
        put(2);
        break;
    case GGML_TYPE_Q6_K:
        put(208);
        break;
    case GGML_TYPE_Q3_K:
        put(108);
        break;
    case GGML_TYPE_IQ4_XS:
    case GGML_TYPE_IQ4_NL:
    case GGML_TYPE_IQ3_S:
    case GGML_TYPE_Q2_0:
    case GGML_TYPE_Q8_0:
        put(0);
        break;
    default:
        break;
    }
}

// `n_out` rows of `type`: random bytes, except each block's own scale field is fixed small (see fix_scale_field),
// so every decoded value is finite and of order 1 -- comparable to random_pair's delta, so a wrong or missing
// delta actually shows up (a value of order 1e-5 to 1e6, as unconstrained random bytes give, would hide it).
std::vector<uint8_t> random_base(std::mt19937& rng, uint32_t type, const char* type_name, int64_t n_in, int64_t n_out,
                                 std::vector<std::vector<float>>& decoded) {
    const auto* t = ggml_get_type_traits((enum ggml_type) type);
    const size_t row_bytes = ggml_row_size((enum ggml_type) type, n_in);
    const size_t block_bytes = t->type_size;
    const int64_t n_blocks = n_in / t->blck_size;
    std::vector<uint8_t> data((size_t) n_out * row_bytes);
    decoded.assign((size_t) n_out, std::vector<float>((size_t) n_in));
    std::uniform_int_distribution<int> byte_dist(0, 255);
    for (int64_t i = 0; i < n_out; ++i) {
        uint8_t* row = data.data() + (size_t) i * row_bytes;
        for (size_t b = 0; b < row_bytes; ++b) row[b] = (uint8_t) byte_dist(rng);
        for (int64_t blk = 0; blk < n_blocks; ++blk) fix_scale_field(type, row + (size_t) blk * block_bytes);
        t->to_float(row, decoded[(size_t) i].data(), n_in);
        for (float v : decoded[(size_t) i])
            check(std::isfinite(v), std::string(type_name) + ": a controlled-scale random block decoded to a non-finite value");
    }
    return data;
}

// ------------------------------------------------------------------ 1. open()
void test_open() {
    const auto tmp = std::filesystem::temp_directory_path() / "lora_merge_test_open";
    std::filesystem::create_directories(tmp);

    Pair proj;   // r=2, n_in=8, n_out=4
    proj.r = 2;
    proj.n_in = 8;
    proj.n_out = 4;
    proj.a = {1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0};   // r=2 rows of n_in=8
    proj.b = {1, 2, 3, 4, 5, 6, 7, 8};                            // n_out=4 rows of r=2

    {
        auto path = write_adapter(tmp, "ok.gguf", 16.0f, {{"blk.0.attn_q.weight", proj}});
        basalt::core::LoraAdapter ad;
        std::string err;
        check(basalt::core::LoraAdapter::open(path.string(), ad, err), "a well-formed adapter opens: " + err);
        check(ad.alpha() == 16.0f, "alpha read back");
        check(ad.covered_count() == 1, "one covered tensor");
        check(ad.router_count() == 0 && ad.expert_count() == 0 && ad.other_count() == 0, "no other tensors");
        const auto* p = ad.find("blk.0.attn_q.weight");
        check(p != nullptr, "find() returns the pair");
        if (p) {
            check(p->r == 2 && p->n_in == 8 && p->n_out == 4, "pair geometry read back");
            check(p->a == proj.a, "lora_a values read back exactly");
            check(p->b == proj.b, "lora_b values read back exactly");
        }
    }
    {   // a router, an expert and an "other" tensor (none of the ten native families) are each counted apart
        std::map<std::string, Pair> pairs = {
            {"blk.0.attn_q.weight", proj}, {"blk.0.ffn_gate_inp.weight", proj},
            {"blk.0.ffn_gate_exps.3.weight", proj}, {"blk.0.hc_attn_norm.weight", proj},
        };
        auto path = write_adapter(tmp, "other.gguf", 16.0f, pairs);
        basalt::core::LoraAdapter ad;
        std::string err;
        check(basalt::core::LoraAdapter::open(path.string(), ad, err), "an adapter with other tensors opens: " + err);
        check(ad.covered_count() == 1, "only the projection tensor is covered");
        check(ad.router_count() == 1, "the router tensor is counted as \"router\"");
        check(ad.expert_count() == 1, "the expert tensor is counted as \"expert\"");
        check(ad.other_count() == 1, "the hyper-connection tensor is counted as \"other\"");
    }
    {   // a routed expert's lora_a/lora_b are 3D (stacked per expert): counted as "expert", not a hard error,
        // because it is classified by name before its shape is ever checked
        std::vector<float> a2(proj.a), b2(proj.b);   // two experts' worth, to match the 3D shape below
        a2.insert(a2.end(), proj.a.begin(), proj.a.end());
        b2.insert(b2.end(), proj.b.begin(), proj.b.end());
        std::vector<fixture::Tensor> ts = {
            {"blk.0.ffn_down_exps.weight.lora_a", {(uint64_t) proj.n_in, (uint64_t) proj.r, 2}, 0, 1, f32_bytes(a2)},
            {"blk.0.ffn_down_exps.weight.lora_b", {(uint64_t) proj.r, (uint64_t) proj.n_out, 2}, 0, 1, f32_bytes(b2)},
        };
        const auto path = tmp / "expert_3d.gguf";
        fixture::write(path, {fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                              fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", 16.0f)}, ts);
        basalt::core::LoraAdapter ad;
        std::string err;
        check(basalt::core::LoraAdapter::open(path.string(), ad, err), "a 3D routed-expert tensor opens: " + err);
        check(ad.covered_count() == 0, "a 3D expert tensor is not fused");
        check(ad.expert_count() == 1, "a 3D expert tensor is still counted as \"expert\", not rejected");
    }
    {   // energy_share is std::nullopt, not 0, when the file never wrote that metadata
        auto path = write_adapter(tmp, "no_share.gguf", 16.0f, {{"blk.0.attn_q.weight", proj}});
        basalt::core::LoraAdapter ad;
        std::string err;
        check(basalt::core::LoraAdapter::open(path.string(), ad, err), "opens without energy_share metadata: " + err);
        check(!ad.energy_share("projections").has_value(), "energy_share is nullopt, not 0, when absent");
    }
    {   // a lora_a with no matching lora_b is rejected
        std::vector<fixture::Tensor> ts = {lora_a_tensor("blk.0.attn_q.weight", proj)};
        const auto path = tmp / "unpaired.gguf";
        fixture::write(path, {fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                              fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", 16.0f)}, ts);
        basalt::core::LoraAdapter bad;
        std::string e;
        check(!basalt::core::LoraAdapter::open(path.string(), bad, e), "a lora_a without a lora_b is rejected");
        check(e.find("lora_a") != std::string::npos || e.find("lora_b") != std::string::npos,
             "error names the unpaired tensor (got: " + e + ")");
    }
    {   // a truncated file (the fixture's own `cut` parameter) is rejected, not read past its mapping
        std::vector<fixture::Kv> kv = {fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                                       fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", 16.0f)};
        std::vector<fixture::Tensor> ts = {lora_a_tensor("blk.0.attn_q.weight", proj), lora_b_tensor("blk.0.attn_q.weight", proj)};
        const auto path = tmp / "truncated.gguf";
        fixture::write(path, kv, ts, /* cut */ 16);
        basalt::core::LoraAdapter bad;
        std::string e;
        check(!basalt::core::LoraAdapter::open(path.string(), bad, e), "a truncated adapter is rejected");
        check(e.find("truncated") != std::string::npos, "error names the truncation (got: " + e + ")");
    }

    auto expect_fail = [&](const std::vector<fixture::Kv>& kv, const Pair& ab, const std::string& needle) {
        std::vector<fixture::Tensor> ts = {lora_a_tensor("blk.0.attn_q.weight", proj), lora_b_tensor("blk.0.attn_q.weight", ab)};
        const auto path = tmp / "bad.gguf";
        fixture::write(path, kv, ts);
        basalt::core::LoraAdapter bad;
        std::string e;
        check(!basalt::core::LoraAdapter::open(path.string(), bad, e), "rejects (" + needle + ")");
        check(e.find(needle) != std::string::npos, "error names \"" + needle + "\" (got: " + e + ")");
    };
    expect_fail({fixture::str("general.type", "model"), fixture::str("adapter.type", "lora"),
                fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", 16.0f)},
               proj, "general.type");
    expect_fail({fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                fixture::str("general.architecture", "qwen35moe"), fixture::f32("adapter.lora.alpha", 16.0f)},
               proj, "architecture");
    expect_fail({fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                fixture::str("general.architecture", "qwen4exp")},
               proj, "alpha");
    Pair bad_rank = proj;
    bad_rank.r = 3;
    bad_rank.b.assign((size_t) (proj.n_out * 3), 0.0f);
    expect_fail({fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", 16.0f)},
               bad_rank, "rank");
}

// ------------------------------------------------------------------ 2. merge parity against a double reference
// `scale_factor`, `base_scale_factor` and `rank_cut` exist only to let the test sabotage its own reference (see
// check_parity_case): 1.0/1.0/0 is the real reference; anything else is a deliberately wrong one the test must
// be able to reject -- scale_factor on the delta term, base_scale_factor on the base term, so a test that only
// ever gets the delta wrong could still miss a bug that only breaks the base term.
void double_reference_row(const std::vector<float>& base_row, const std::vector<Pair>& pairs,
                          const std::vector<float>& adapter_alpha, const std::vector<float>& user_scale,
                          int64_t row, int64_t n_in, double scale_factor, double base_scale_factor, int64_t rank_cut,
                          std::vector<double>& out) {
    out.resize((size_t) n_in);
    for (int64_t j = 0; j < n_in; ++j) out[j] = base_scale_factor * (double) base_row[j];
    for (size_t ai = 0; ai < pairs.size(); ++ai) {
        const Pair& p = pairs[ai];
        const int64_t r_used = std::max<int64_t>(0, p.r - rank_cut);
        const double coef0 = scale_factor * (double) user_scale[ai] * ((double) adapter_alpha[ai] / (double) p.r);
        for (int64_t k = 0; k < r_used; ++k) {
            const double coef = coef0 * (double) p.b[(size_t) row * p.r + k];
            if (coef == 0.0) continue;
            for (int64_t j = 0; j < n_in; ++j) out[j] += coef * (double) p.a[(size_t) k * n_in + j];
        }
    }
}

struct ViolationStats {
    int64_t blocks = 0, violated = 0;
    double worst_ratio = 0;
    int64_t worst_i = -1, worst_j = -1;
    double worst_got = 0, worst_ideal = 0, worst_tol = 0;
};

// Compares the merged output `out` against the reference built with `scale_factor`/`rank_cut` (1.0/0 for the
// real reference), per 32-value block: a block is "violated" if any of its values misses the tolerance. The
// tolerance is exact, not padded: 0.5 step for the code's own rounding (computed from the block's own stored,
// already-rounded scale) plus the scale's own fp16 rounding error amplified by the largest code it multiplies
// (127), with a 0.1% margin for using the stored rather than the exact scale.
ViolationStats compare_to_reference(const std::vector<uint8_t>& out, const std::vector<std::vector<float>>& base_f32,
                                    const std::vector<Pair>& pairs, const std::vector<float>& alpha,
                                    const std::vector<float>& user_scale, int64_t n_in, int64_t n_out,
                                    double scale_factor, double base_scale_factor, int64_t rank_cut) {
    ViolationStats s;
    const auto* q8 = ggml_get_type_traits(GGML_TYPE_Q8_0);
    const size_t row_bytes_q8 = (size_t) ggml_row_size(GGML_TYPE_Q8_0, n_in);
    std::vector<double> ideal;
    std::vector<float> got((size_t) n_in);
    for (int64_t i = 0; i < n_out; ++i) {
        double_reference_row(base_f32[(size_t) i], pairs, alpha, user_scale, i, n_in, scale_factor, base_scale_factor,
                             rank_cut, ideal);
        q8->to_float(out.data() + (size_t) i * row_bytes_q8, got.data(), n_in);
        for (int64_t blk = 0; blk * 32 < n_in; ++blk) {
            uint16_t d16;
            std::memcpy(&d16, out.data() + (size_t) i * row_bytes_q8 + (size_t) blk * q8->type_size, 2);
            const double step = (double) ggml_fp16_to_fp32(d16);
            const double tol = 1.001 * (0.5 + 127.0 / 2048.0) * step + 1e-30;
            const int64_t hi = std::min(blk * 32 + 32, n_in);
            bool violated = false;
            for (int64_t j = blk * 32; j < hi; ++j) {
                const double diff = std::fabs((double) got[(size_t) j] - ideal[(size_t) j]);
                if (diff > tol) violated = true;
                const double ratio = diff / tol;
                if (ratio > s.worst_ratio) {
                    s.worst_ratio = ratio; s.worst_i = i; s.worst_j = j;
                    s.worst_got = got[(size_t) j]; s.worst_ideal = ideal[(size_t) j]; s.worst_tol = tol;
                }
            }
            ++s.blocks;
            if (violated) ++s.violated;
        }
    }
    return s;
}

void check_parity_case(std::mt19937& rng, const std::filesystem::path& tmp, uint32_t type, const char* type_name,
                       int64_t n_in, int64_t n_out, const std::vector<int64_t>& ranks) {
    std::vector<std::vector<float>> base_f32;
    const auto base_bytes = random_base(rng, type, type_name, n_in, n_out, base_f32);

    // Fixing the scale field keeps every block finite, but a K-quant's own per-sub-block scale byte is still
    // random and can push the decoded magnitude well past "order 1". delta_j is a sum of r products of two
    // N(0, sigma^2) values, each scaled by coef0 = user_scale * alpha / r: its stddev is coef0 * sigma^2 *
    // sqrt(r) -- quadratic in sigma, not linear -- so sigma is solved for from the base's own observed RMS and
    // the first adapter's coef0, instead of being set straight from the base's magnitude as if it were linear.
    double sum_sq = 0.0;
    int64_t n_values = 0;
    for (const auto& row : base_f32) for (float v : row) { sum_sq += (double) v * v; ++n_values; }
    const double rms_base = std::sqrt(sum_sq / (double) n_values);
    const double alpha0 = 8.0, user_scale0 = 0.5;   // ai=0 of the alpha[ai]/user_scale[ai] formulas just below
    const double coef0_first = user_scale0 * (alpha0 / (double) ranks[0]);
    const float sigma = (float) std::sqrt(rms_base / (coef0_first * std::sqrt((double) ranks[0])));

    const std::string gguf_name = "blk.0.attn_q.weight";
    std::vector<basalt::core::LoraAdapter> adapters(ranks.size());
    std::vector<Pair> pairs(ranks.size());
    std::vector<float> alpha(ranks.size()), user_scale(ranks.size());
    for (size_t ai = 0; ai < ranks.size(); ++ai) {
        pairs[ai] = random_pair(rng, ranks[ai], n_in, n_out, sigma);
        alpha[ai] = 8.0f + 4.0f * (float) ai;
        user_scale[ai] = 0.5f + 0.4f * (float) ai;
        const auto path = write_adapter(tmp, std::string(type_name) + "_a" + std::to_string(ai) + ".gguf", alpha[ai],
                                        {{gguf_name, pairs[ai]}});
        std::string err;
        check(basalt::core::LoraAdapter::open(path.string(), adapters[ai], err), std::string(type_name) + ": adapter opens: " + err);
    }
    std::vector<basalt::core::LoraEntry> entries;
    for (size_t ai = 0; ai < ranks.size(); ++ai) entries.push_back({&adapters[ai], user_scale[ai]});
    basalt::core::LoraSet set(entries);

    std::vector<uint8_t> out;
    std::string err;
    const bool ok = basalt::core::lora_merge_q8_0(type, base_bytes.data(), n_in, n_out, set, gguf_name, out, err);
    check(ok, std::string("merge succeeds for ") + type_name + ": " + err);
    if (!ok) return;

    const auto real = compare_to_reference(out, base_f32, pairs, alpha, user_scale, n_in, n_out, 1.0, 1.0, 0);
    if (real.violated) {
        std::fprintf(stderr, "  %s worst: row %lld col %lld got=%.6g ideal=%.6g tol=%.6g ratio=%.3g\n", type_name,
                     (long long) real.worst_i, (long long) real.worst_j, real.worst_got, real.worst_ideal, real.worst_tol,
                     real.worst_ratio);
    }
    check(real.violated == 0, std::string(type_name) + ": every merged block is within tolerance of the double reference");

    // the test must have teeth: a deliberately wrong reference (5% off delta scale, 5% off the base term, or
    // one rank term missing per adapter) has to fail this same comparison in most blocks, or the comparison
    // above is not actually checking anything -- the base and the delta are checked separately, since a test
    // that only ever perturbs the delta could still miss a bug that only breaks the base term
    const auto wrong_scale = compare_to_reference(out, base_f32, pairs, alpha, user_scale, n_in, n_out, 1.05, 1.0, 0);
    check(wrong_scale.violated > wrong_scale.blocks / 2,
         std::string(type_name) + ": the test detects a 5% wrong delta scale (sanity check on the test itself)");
    const auto wrong_base = compare_to_reference(out, base_f32, pairs, alpha, user_scale, n_in, n_out, 1.0, 1.05, 0);
    check(wrong_base.violated > wrong_base.blocks / 2,
         std::string(type_name) + ": the test detects a 5% wrong base (sanity check on the test itself)");
    const auto wrong_rank = compare_to_reference(out, base_f32, pairs, alpha, user_scale, n_in, n_out, 1.0, 1.0, 1);
    check(wrong_rank.violated > wrong_rank.blocks / 2,
         std::string(type_name) + ": the test detects a missing rank term (sanity check on the test itself)");
}

// ------------------------------------------------------------------ 3. Q8_0-specific identity properties
std::vector<uint8_t> make_q8_0_block(float d, const std::array<int8_t, 32>& codes) {
    std::vector<uint8_t> blk(34);
    const uint16_t d16 = ggml_fp32_to_fp16(d);
    std::memcpy(blk.data(), &d16, 2);
    for (int i = 0; i < 32; ++i) blk[2 + (size_t) i] = (uint8_t) codes[(size_t) i];
    return blk;
}

void test_q8_0_identities(std::mt19937& rng) {
    // block 0: codes in [-126, 126] with the max exactly 126 -- requantizing it (not copying it) gives 127, as
    // the comment in lora.cpp explains; block 1: ordinary random Q8_0 bytes.
    std::array<int8_t, 32> codes0{};
    codes0[0] = -126;
    for (int i = 1; i < 32; ++i) codes0[(size_t) i] = 100;
    const auto block0 = make_q8_0_block(1.0f, codes0);
    std::uniform_int_distribution<int> byte_dist(0, 255);
    std::vector<uint8_t> block1(34);
    for (auto& b : block1) b = (uint8_t) byte_dist(rng);
    fix_scale_field(GGML_TYPE_Q8_0, block1.data());   // a random fp16 scale can itself decode to inf/NaN
    std::vector<uint8_t> base_bytes;
    base_bytes.insert(base_bytes.end(), block0.begin(), block0.end());
    base_bytes.insert(base_bytes.end(), block1.begin(), block1.end());
    const int64_t n_in = 64, n_out = 1;

    // sanity: re-quantizing block0's own dequantized values does NOT reproduce it -- a null delta must be a
    // byte copy instead, not a requantize, or this exact block would silently change under --lora
    const auto* q8 = ggml_get_type_traits(GGML_TYPE_Q8_0);
    std::vector<float> decoded(32);
    q8->to_float(block0.data(), decoded.data(), 32);
    std::vector<uint8_t> requantized(34);
    q8->from_float_ref(decoded.data(), requantized.data(), 32);
    check(requantized != block0, "requantizing a max-code-126 Q8_0 block changes its bytes");

    Pair zero_pair = random_pair(rng, 4, n_in, n_out, 0.02f);
    std::fill(zero_pair.b.begin(), zero_pair.b.end(), 0.0f);   // B = 0: a null adapter

    const auto tmp = std::filesystem::temp_directory_path() / "lora_merge_test_q8";
    std::filesystem::create_directories(tmp);
    const std::string gguf_name = "blk.0.attn_q.weight";
    basalt::core::LoraAdapter zero_adapter;
    {
        std::string err;
        const auto path = write_adapter(tmp, "zero.gguf", 16.0f, {{gguf_name, zero_pair}});
        check(basalt::core::LoraAdapter::open(path.string(), zero_adapter, err), "null adapter opens: " + err);
    }
    {
        basalt::core::LoraSet set({{&zero_adapter, 1.0f}});
        std::vector<uint8_t> out;
        std::string err;
        check(basalt::core::lora_merge_q8_0(8 /* Q8_0 */, base_bytes.data(), n_in, n_out, set, gguf_name, out, err),
             "merge with a null adapter succeeds: " + err);
        check(out == base_bytes, "a null adapter reproduces the Q8_0 base exactly, max-code-126 block included");
    }

    Pair real_pair = random_pair(rng, 4, n_in, n_out, 0.05f);
    basalt::core::LoraAdapter plus, minus;
    {
        std::string err;
        auto path_p = write_adapter(tmp, "plusminus.gguf", 16.0f, {{gguf_name, real_pair}});
        check(basalt::core::LoraAdapter::open(path_p.string(), plus, err), "+1 adapter opens: " + err);
        check(basalt::core::LoraAdapter::open(path_p.string(), minus, err), "-1 adapter opens: " + err);
    }
    {
        basalt::core::LoraSet set({{&plus, 1.0f}, {&minus, -1.0f}});
        std::vector<uint8_t> out;
        std::string err;
        check(basalt::core::lora_merge_q8_0(8, base_bytes.data(), n_in, n_out, set, gguf_name, out, err),
             "merge with +1/-1 of the same adapter succeeds: " + err);
        check(out == base_bytes, "the same adapter at scale +1 and -1 cancels to the exact Q8_0 base");
    }
    {   // determinism: the same call twice gives the same bytes
        basalt::core::LoraSet set({{&plus, 0.7f}});
        std::vector<uint8_t> out1, out2;
        std::string err;
        basalt::core::lora_merge_q8_0(8, base_bytes.data(), n_in, n_out, set, gguf_name, out1, err);
        basalt::core::lora_merge_q8_0(8, base_bytes.data(), n_in, n_out, set, gguf_name, out2, err);
        check(out1 == out2, "merging the same input twice gives the same bytes");
    }
}

// ------------------------------------------------------------------ 4. microbench
void microbench(std::mt19937& rng, const std::filesystem::path& tmp) {
    const int64_t n_in = 2560, n_out = 10240;
    std::vector<std::vector<float>> decoded;
    const auto base_bytes = random_base(rng, GGML_TYPE_Q6_K, "Q6_K", n_in, n_out, decoded);
    const Pair p = random_pair(rng, 8, n_in, n_out, 0.4f);
    basalt::core::LoraAdapter ad;
    std::string err;
    const auto path = write_adapter(tmp, "bench.gguf", 16.0f, {{"blk.0.attn_qkv.weight", p}});
    basalt::core::LoraAdapter::open(path.string(), ad, err);
    basalt::core::LoraSet set({{&ad, 1.0f}});

    std::vector<uint8_t> out;
    const auto t0 = std::chrono::steady_clock::now();
    const bool ok = basalt::core::lora_merge_q8_0(GGML_TYPE_Q6_K, base_bytes.data(), n_in, n_out, set,
                                                  "blk.0.attn_qkv.weight", out, err);
    const auto t1 = std::chrono::steady_clock::now();
    check(ok, "microbench merge succeeds: " + err);
    const double seconds = std::chrono::duration<double>(t1 - t0).count();
    std::printf("lora_merge_test: fusing one %lld -> %lld tensor (Q6_K base, rank 8) took %.3f s\n",
               (long long) n_in, (long long) n_out, seconds);
}

void test_committed_sample(const std::string& path) {
    basalt::core::LoraAdapter ad;
    std::string err;
    check(basalt::core::LoraAdapter::open(path, ad, err), "the committed sample opens: " + err);
    check(ad.alpha() == 4.0f, "the committed sample's alpha is 4 (no rank/alpha_pattern, no rslora: no correction)");
    check(ad.covered_count() == 1, "one tensor of the ten native families (attn_q); mlp.gate is the router");
    check(ad.router_count() == 1 && ad.expert_count() == 0 && ad.other_count() == 0,
          "one router tensor, no expert, no other");

    const auto* p = ad.find("blk.0.attn_q.weight");
    check(p != nullptr, "attn_q.weight is covered");
    if (p) {
        check(p->r == 2 && p->n_in == 8 && p->n_out == 4, "attn_q geometry");
        const std::vector<float> want_a = {0.5f, 1.0f, -0.5f, 0.25f, 0.0f, 0.75f, -1.0f, 0.125f,
                                            1.0f, -0.25f, 0.5f, -0.5f, 0.25f, 0.0f, 0.75f, -0.125f};
        const std::vector<float> want_b = {0.5f, -0.25f, 0.25f, 0.5f, -0.5f, 0.125f, 0.125f, -0.5f};
        check(p->a == want_a, "attn_q lora_a values, exactly as lora.py wrote them");
        check(p->b == want_b, "attn_q lora_b values, exactly as lora.py wrote them (no rank correction applied)");
    }

    const auto share_proj = ad.energy_share("projections");
    const auto share_router = ad.energy_share("router");
    check(share_proj.has_value() && std::fabs(*share_proj - 0.2549329f) < 1e-4f, "projections energy share");
    check(share_router.has_value() && std::fabs(*share_router - 0.7450671f) < 1e-4f, "router energy share");
}

}  // namespace

int run(const std::string& sample_path) {
    std::mt19937 rng(7);
    test_open();
    if (!sample_path.empty()) test_committed_sample(sample_path);

    const auto tmp = std::filesystem::temp_directory_path() / "lora_merge_test_parity";
    std::filesystem::create_directories(tmp);
    check_parity_case(rng, tmp, GGML_TYPE_Q4_K, "Q4_K", 2560, 10240, {8});
    check_parity_case(rng, tmp, GGML_TYPE_Q5_K, "Q5_K", 6144, 2560, {5});
    check_parity_case(rng, tmp, GGML_TYPE_Q6_K, "Q6_K", 2560, 10240, {8, 8});   // two adapters together
    check_parity_case(rng, tmp, GGML_TYPE_IQ4_XS, "IQ4_XS", 6144, 2560, {8});
    check_parity_case(rng, tmp, GGML_TYPE_IQ4_NL, "IQ4_NL", 2560, 10240, {8});
    check_parity_case(rng, tmp, GGML_TYPE_IQ3_S, "IQ3_S", 2560, 10240, {5});
    check_parity_case(rng, tmp, GGML_TYPE_Q3_K, "Q3_K", 6144, 2560, {8});
    check_parity_case(rng, tmp, GGML_TYPE_Q2_0, "Q2_0", 2560, 10240, {8});
    check_parity_case(rng, tmp, GGML_TYPE_Q8_0, "Q8_0", 6144, 2560, {5, 8});   // two adapters, different rank

    test_q8_0_identities(rng);
    microbench(rng, tmp);

    if (failures) {
        std::fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("lora_merge_test: ok\n");
    return 0;
}

int main(int argc, char** argv) {
    try {
        return run(argc > 1 ? argv[1] : "");
    } catch (const std::exception& e) {
        std::fprintf(stderr, "uncaught exception: %s\n", e.what());
        return 1;
    }
}
