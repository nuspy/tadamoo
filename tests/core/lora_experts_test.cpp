// A LoRA adapter's routed-expert deltas fused into an experts section (lora_experts.cpp): host only, no model, no GPU.
//
// 1. The IQ4_XS, IQ4_NL, IQ3_XXS, IQ3_S, IQ2_S and Q5_0 quantizers against llama.cpp's own bytes
//    (testdata/quantizer_parity.bin: rows made by llama.cpp's ggml_quantize_chunk, with the float rows they came
//    from, edge rows included; it holds one case per type, made by a program linked to llama.cpp's ggml).
// 2. An adapter written by llama.cpp's trainer (testdata/qvac_toy_adapter.gguf, stacked over the experts) read by
//    LoraAdapter, and an adapter with one pair per expert (the converter's naming) read the same way.
// 3. The fusion at the real pack's geometry (n_embd 2560, n_ff 640, IQ4_XS gate/up, IQ4_NL down, 2662400-byte
//    blobs): an expert the adapter leaves alone is byte-identical, a row whose B row is zero is byte-identical, a
//    changed row is within quantization error of base + delta in double precision, a layer the adapter does not
//    carry is byte-identical, the same input gives the same bytes on one thread and on several.
// 4. What is refused: a layer in a format that cannot be quantized again, an adapter whose shapes disagree with the
//    layout, an adapter with more layers than the model.
// 5. The file the engine loads: written once, reused by the next load, replaced (and the old one removed) when the
//    adapter changes.
// 6. A microbench line: seconds to fuse one expert's three matrices.
#include "basalt/core/lora.hpp"
#include "basalt/core/lora_experts.hpp"
#include "basalt/ggml/ggml.h"
#include "gguf_fixture.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <vector>

namespace {

using basalt::core::ExpertFuseStats;
using basalt::core::ExpertRole;
using basalt::core::FileRange;
using basalt::core::LoraAdapter;
using basalt::core::LoraEntry;
using basalt::core::LoraExpertView;
using basalt::core::LoraSet;
using basalt::kernels::cpu::ExpertLayout;
using basalt::kernels::cpu::NativeFmt;
namespace fs = std::filesystem;

int failures = 0;
void check(bool ok, const std::string& what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what.c_str());
        ++failures;
    }
}

constexpr int kIq4Nl = (int) GGML_TYPE_IQ4_NL, kIq4Xs = (int) GGML_TYPE_IQ4_XS;
constexpr int kNEmbd = 2560, kNFf = 640, kNExpert = 6, kNLayers = 2;

std::vector<uint8_t> read_all(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

// ---- 1. the IQ4 quantizers against llama.cpp
void parity(const fs::path& dir) {
    const auto bytes = read_all(dir / "quantizer_parity.bin");
    check(!bytes.empty(), "quantizer_parity.bin is readable");
    size_t at = 0;
    int cases = 0;
    while (at + 12 <= bytes.size()) {
        int32_t head[3];
        std::memcpy(head, bytes.data() + at, 12);
        at += 12;
        const enum ggml_type type = (enum ggml_type) head[0];
        const int nrows = head[1], n_per_row = head[2];
        const size_t row_bytes = ggml_row_size(type, n_per_row);
        std::vector<float> src((size_t) nrows * n_per_row);
        std::memcpy(src.data(), bytes.data() + at, src.size() * 4);
        at += src.size() * 4;
        const uint8_t* expected = bytes.data() + at;
        at += (size_t) nrows * row_bytes;
        ggml_quantize_init(type);
        std::vector<uint8_t> got((size_t) nrows * row_bytes);
        const size_t n = ggml_quantize_chunk(type, src.data(), got.data(), 0, nrows, n_per_row);
        check(n == got.size(), "ggml_quantize_chunk writes whole rows for " + std::string(ggml_type_name(type)));
        int differing = 0;
        for (int r = 0; r < nrows; ++r)
            if (std::memcmp(got.data() + (size_t) r * row_bytes, expected + (size_t) r * row_bytes, row_bytes) != 0) ++differing;
        check(differing == 0, std::string(ggml_type_name(type)) + ": " + std::to_string(differing) + " of " +
                                  std::to_string(nrows) + " rows differ from llama.cpp's bytes");
        // starting in the middle of a chunk lands on the right row
        std::vector<uint8_t> tail((size_t) nrows * row_bytes, 0);
        ggml_quantize_chunk(type, src.data(), tail.data(), (int64_t) 5 * n_per_row, 2, n_per_row);
        check(std::memcmp(tail.data() + (size_t) 5 * row_bytes, expected + (size_t) 5 * row_bytes, 2 * row_bytes) == 0,
              std::string(ggml_type_name(type)) + ": a chunk that starts at row 5 lands on row 5");
        ++cases;
    }
    check(cases == 6, "the parity file holds one case for each of the six types");
}

// ---- adapter files
struct Stacked {
    std::vector<float> a, b;   // (n_in, r, E) and (r, n_out, E), E slowest
    int64_t r = 0, n_in = 0, n_out = 0, n_expert = 0;
};

std::vector<uint8_t> f32_bytes(const std::vector<float>& v) {
    std::vector<uint8_t> out(v.size() * 4);
    std::memcpy(out.data(), v.data(), out.size());
    return out;
}

// B stays zero for experts >= n_touched, and for rows >= rows_touched of expert 1.
Stacked make_stacked(std::mt19937& rng, int64_t r, int64_t n_in, int64_t n_out, int n_touched, int64_t rows_touched) {
    std::normal_distribution<float> dist(0.0f, 0.02f);
    Stacked s;
    s.r = r; s.n_in = n_in; s.n_out = n_out; s.n_expert = kNExpert;
    s.a.resize((size_t) (n_in * r * kNExpert));
    s.b.assign((size_t) (r * n_out * kNExpert), 0.0f);
    for (auto& v : s.a) v = dist(rng);
    for (int64_t e = 0; e < n_touched; ++e)
        for (int64_t i = 0; i < n_out; ++i) {
            if (e == 1 && i >= rows_touched) continue;
            for (int64_t k = 0; k < r; ++k) s.b[(size_t) (e * n_out * r + i * r + k)] = dist(rng);
        }
    return s;
}

void add_stacked(std::vector<fixture::Tensor>& ts, int layer, const char* role, const Stacked& s) {
    const std::string base = "blk." + std::to_string(layer) + ".ffn_" + role + "_exps.weight";
    ts.push_back({base + ".lora_a", {(uint64_t) s.n_in, (uint64_t) s.r, (uint64_t) s.n_expert}, 0, 1, f32_bytes(s.a)});
    ts.push_back({base + ".lora_b", {(uint64_t) s.r, (uint64_t) s.n_out, (uint64_t) s.n_expert}, 0, 1, f32_bytes(s.b)});
}

void write_adapter(const fs::path& path, const std::vector<fixture::Tensor>& ts, float alpha = 8.0f) {
    fixture::write(path, {fixture::str("general.type", "adapter"), fixture::str("adapter.type", "lora"),
                          fixture::str("general.architecture", "qwen4exp"), fixture::f32("adapter.lora.alpha", alpha)},
                   ts);
}

// ---- 2. readers
void readers(const fs::path& dir, const fs::path& tmp) {
    LoraAdapter a;
    std::string err;
    check(LoraAdapter::open((dir / "qvac_toy_adapter.gguf").string(), a, err), "the trainer's adapter opens: " + err);
    check(a.alpha() == 8.0f, "alpha is read from the trainer's file");
    check(a.expert_count() == 12, "four layers x gate/up/down stacked pairs are counted as experts");
    check(a.router_count() == 0 && a.other_count() == 0, "nothing else is left over");
    check(a.expert_layers() == 4, "the highest expert layer is 3");
    check(a.find("blk.0.attn_qkv.weight") != nullptr && a.find("blk.0.ffn_up_shexp.weight") != nullptr &&
              a.find("blk.3.attn_q.weight") != nullptr,
          "the dense families the trainer wrote are read too");
    for (int l = 0; l < 4; ++l) {
        LoraExpertView g, u, d;
        const bool ok = a.has_experts(l, ExpertRole::Gate) && a.expert(l, ExpertRole::Gate, 3, g) &&
                        a.expert(l, ExpertRole::Up, 0, u) && a.expert(l, ExpertRole::Down, 2, d);
        check(ok, "layer " + std::to_string(l) + " has its gate, up and down experts");
        if (!ok) continue;
        check(g.r == 4 && u.r == 4 && d.r == 4, "rank 4");
        check(g.n_in == 256 && g.n_out == u.n_out && d.n_in == g.n_out && d.n_out == 256,
              "gate/up map n_embd to n_ff and down maps back");
        double sum = 0;
        for (int64_t i = 0; i < g.n_out * g.r; ++i) sum += std::fabs((double) g.b[i]);
        check(sum > 0, "the trained B of expert 3 is not zero");
    }
    LoraExpertView v;
    check(!a.expert(0, ExpertRole::Gate, 4, v), "an expert past the stacked count is not there");
    check(!a.expert(9, ExpertRole::Gate, 0, v), "a layer the adapter lacks is not there");

    // one pair per expert, the converter's naming
    std::mt19937 rng(7);
    std::normal_distribution<float> dist(0.0f, 0.02f);
    std::vector<float> pa(8 * 32), pb(16 * 8);
    for (auto& x : pa) x = dist(rng);
    for (auto& x : pb) x = dist(rng);
    std::vector<fixture::Tensor> ts;
    ts.push_back({"blk.1.ffn_up_exps.2.weight.lora_a", {32, 8}, 0, 1, f32_bytes(pa)});
    ts.push_back({"blk.1.ffn_up_exps.2.weight.lora_b", {8, 16}, 0, 1, f32_bytes(pb)});
    write_adapter(tmp / "per_expert.gguf", ts);
    LoraAdapter p;
    check(LoraAdapter::open((tmp / "per_expert.gguf").string(), p, err), "a per-expert adapter opens: " + err);
    check(p.expert_count() == 1 && p.has_experts(1, ExpertRole::Up) && !p.has_experts(1, ExpertRole::Gate),
          "one per-expert pair is one expert tensor, for up only");
    check(p.expert(1, ExpertRole::Up, 2, v) && v.r == 8 && v.n_in == 32 && v.n_out == 16 &&
              std::memcmp(v.a, pa.data(), pa.size() * 4) == 0 && std::memcmp(v.b, pb.data(), pb.size() * 4) == 0,
          "its values read back exactly");
    check(!p.expert(1, ExpertRole::Up, 3, v), "the other experts of that layer are not there");
}

// ---- 3. the fusion
struct Model {
    ExpertLayout lay;
    std::vector<uint8_t> experts;   // the section's bytes
};

NativeFmt iq4_fmt() {
    NativeFmt f;
    f.gu_type = kIq4Xs;
    f.d_type = kIq4Nl;
    f.n_embd = kNEmbd;
    f.n_ff = kNFf;
    f.gu_row = ggml_row_size(GGML_TYPE_IQ4_XS, kNEmbd);
    f.d_row = ggml_row_size(GGML_TYPE_IQ4_NL, kNFf);
    f.up_off = (size_t) kNFf * f.gu_row;
    f.down_off = 2 * f.up_off;
    f.bytes = f.down_off + (size_t) kNEmbd * f.d_row;
    return f;
}

Model make_model(std::mt19937& rng, int gu_type_override = 0) {
    Model m;
    NativeFmt f = iq4_fmt();
    if (gu_type_override) f.gu_type = gu_type_override;
    m.lay.native = true;
    m.lay.n_layers = kNLayers;
    m.lay.n_expert = kNExpert;
    for (int l = 0; l < kNLayers; ++l) {
        m.lay.fmt.push_back(f);
        m.lay.offset.push_back((uint64_t) l * kNExpert * f.bytes);
        m.lay.bytes.push_back(f.bytes);
    }
    m.lay.total = (uint64_t) kNLayers * kNExpert * f.bytes;
    m.experts.assign((size_t) m.lay.total, 0);
    if (gu_type_override) return m;
    std::normal_distribution<float> dist(0.0f, 0.02f);
    std::vector<float> row(kNEmbd);
    auto fill = [&](uint8_t* base, int type, int64_t rows, int64_t cols, size_t row_bytes) {
        for (int64_t i = 0; i < rows; ++i) {
            row.resize((size_t) cols);
            for (auto& v : row) v = dist(rng);
            ggml_quantize_chunk((enum ggml_type) type, row.data(), base + (size_t) i * row_bytes, 0, 1, cols);
        }
    };
    for (int l = 0; l < kNLayers; ++l)
        for (int e = 0; e < kNExpert; ++e) {
            uint8_t* blob = m.experts.data() + m.lay.blob_offset(l, e);
            fill(blob, f.gu_type, kNFf, kNEmbd, f.gu_row);
            fill(blob + f.up_off, f.gu_type, kNFf, kNEmbd, f.gu_row);
            fill(blob + f.down_off, f.d_type, kNEmbd, kNFf, f.d_row);
        }
    return m;
}

double rel_rmse(const std::vector<float>& got, const std::vector<double>& want) {
    double err = 0, ref = 0;
    for (size_t i = 0; i < got.size(); ++i) {
        err += (got[i] - want[i]) * (got[i] - want[i]);
        ref += want[i] * want[i];
    }
    return ref > 0 ? std::sqrt(err / ref) : std::sqrt(err);
}

void fusion(const fs::path& tmp) {
    std::mt19937 rng(2024);
    Model m = make_model(rng);
    const fs::path experts_path = tmp / "base.experts";
    {
        std::ofstream f(experts_path, std::ios::binary | std::ios::trunc);
        const std::vector<uint8_t> pad(4096, 0xAB);   // the section starts past the file's start
        f.write((const char*) pad.data(), (std::streamsize) pad.size());
        f.write((const char*) m.experts.data(), (std::streamsize) m.experts.size());
    }
    const FileRange experts(experts_path.string(), 4096, m.lay.total);

    // layer 0 only: experts 0 and 1 changed, expert 1 only in its first 10 rows
    const Stacked gate = make_stacked(rng, 4, kNEmbd, kNFf, 2, 10);
    const Stacked up = make_stacked(rng, 4, kNEmbd, kNFf, 2, 10);
    const Stacked down = make_stacked(rng, 4, kNFf, kNEmbd, 2, 10);
    std::vector<fixture::Tensor> ts;
    add_stacked(ts, 0, "gate", gate);
    add_stacked(ts, 0, "up", up);
    add_stacked(ts, 0, "down", down);
    const fs::path adapter_path = tmp / "experts_adapter.gguf";
    write_adapter(adapter_path, ts);
    LoraAdapter adapter;
    std::string err;
    check(LoraAdapter::open(adapter_path.string(), adapter, err), "the synthetic adapter opens: " + err);
    LoraSet set({LoraEntry{&adapter, 1.0f}});
    check(set.touches_experts(), "the set carries experts");

    const fs::path out1 = tmp / "fused1.experts", out4 = tmp / "fused4.experts";
    ExpertFuseStats s1, s4;
    const auto t0 = std::chrono::steady_clock::now();
    check(basalt::core::lora_fuse_experts(set, experts, m.lay, 1, out1.string(), s1, err), "fuses on one thread: " + err);
    const double one_thread = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    check(basalt::core::lora_fuse_experts(set, experts, m.lay, 4, out4.string(), s4, err), "fuses on four threads: " + err);
    const auto b1 = read_all(out1), b4 = read_all(out4);
    check(b1.size() == m.experts.size(), "the fused section has the section's size");
    check(b1 == b4, "one thread and four threads give the same bytes");
    check(s1.blobs_fused == 2 && s1.layers == 1, "two experts of one layer are fused");
    if (b1.size() != m.experts.size()) return;

    const NativeFmt f = iq4_fmt();
    const struct ggml_type_traits* xs = ggml_get_type_traits(GGML_TYPE_IQ4_XS);
    const struct ggml_type_traits* nl = ggml_get_type_traits(GGML_TYPE_IQ4_NL);
    // every blob outside layer 0 experts 0 and 1 is the base's
    int unchanged_blobs = 0;
    for (int l = 0; l < kNLayers; ++l)
        for (int e = 0; e < kNExpert; ++e) {
            const uint64_t at = m.lay.blob_offset(l, e);
            const bool same = std::memcmp(b1.data() + at, m.experts.data() + at, f.bytes) == 0;
            if (l == 0 && e < 2) check(!same, "layer 0 expert " + std::to_string(e) + " changed");
            else { check(same, "layer " + std::to_string(l) + " expert " + std::to_string(e) + " is byte-identical"); ++unchanged_blobs; }
        }
    check(unchanged_blobs == 10, "ten of twelve blobs are untouched");

    // a changed row is base + delta within quantization error; a row whose B row is zero is the base's bytes
    const Stacked* mats[3] = {&gate, &up, &down};
    const int types[3] = {kIq4Xs, kIq4Xs, kIq4Nl};
    const int64_t rows[3] = {kNFf, kNFf, kNEmbd}, cols[3] = {kNEmbd, kNEmbd, kNFf};
    const size_t row_bytes[3] = {f.gu_row, f.gu_row, f.d_row}, offsets[3] = {0, f.up_off, f.down_off};
    const float coef = 8.0f / 4.0f;
    double worst = 0;
    int rows_checked = 0;
    for (int e = 0; e < 2; ++e)
        for (int mat = 0; mat < 3; ++mat) {
            const Stacked& s = *mats[mat];
            const auto* tr = types[mat] == kIq4Xs ? xs : nl;
            for (int64_t i = 0; i < rows[mat]; ++i) {
                const uint64_t row_at = m.lay.blob_offset(0, e) + offsets[mat] + (size_t) i * row_bytes[mat];
                if (e == 1 && i >= 10) {
                    check(std::memcmp(b1.data() + row_at, m.experts.data() + row_at, row_bytes[mat]) == 0,
                          "a row whose B row is zero is the base's bytes");
                    continue;
                }
                if (i % 7 != 0 && i >= 10) continue;   // a sample of the rows keeps the reference cheap
                std::vector<float> base((size_t) cols[mat]), got((size_t) cols[mat]);
                tr->to_float(m.experts.data() + row_at, base.data(), cols[mat]);
                tr->to_float(b1.data() + row_at, got.data(), cols[mat]);
                std::vector<double> want((size_t) cols[mat]);
                for (int64_t j = 0; j < cols[mat]; ++j) {
                    double d = 0;
                    for (int64_t k = 0; k < s.r; ++k)
                        d += (double) s.b[(size_t) (e * s.n_out * s.r + i * s.r + k)] *
                             (double) s.a[(size_t) (e * s.r * s.n_in + k * s.n_in + j)];
                    want[(size_t) j] = (double) base[(size_t) j] + (double) coef * d;
                }
                const double err_rel = rel_rmse(got, want);
                worst = std::max(worst, err_rel);
                ++rows_checked;
            }
        }
    check(rows_checked > 200, "enough changed rows were compared");
    check(worst < 0.12, "changed rows are within 4-bit quantization error of base + delta (worst " + std::to_string(worst) + ")");

    // the bench: one expert's three matrices
    std::printf("lora experts: %.3f s per fused expert blob (1 thread, %d rows changed each)\n",
                one_thread / (double) s1.blobs_fused, (int) (2 * kNFf + kNEmbd));

    // ---- 4. refusals
    {
        Model bad = make_model(rng, 16 /* iq2_xxs: read, never written */);
        ExpertFuseStats st;
        const bool ok = basalt::core::lora_fuse_experts(set, experts, bad.lay, 1, (tmp / "refused.experts").string(), st, err);
        check(!ok && err.find("layer 0") != std::string::npos && err.find("iq2_xxs") != std::string::npos,
              "a layer in a type that cannot be quantized again is refused by name: " + err);
        check(!fs::exists(tmp / "refused.experts") && !fs::exists(tmp / "refused.experts.part"), "a refusal leaves no file");
    }
    {
        std::vector<fixture::Tensor> wrong;
        add_stacked(wrong, 0, "gate", make_stacked(rng, 4, kNEmbd, kNFf + 32, 2, 10));
        write_adapter(tmp / "wrong_shape.gguf", wrong);
        LoraAdapter w;
        LoraAdapter::open((tmp / "wrong_shape.gguf").string(), w, err);
        LoraSet ws({LoraEntry{&w, 1.0f}});
        ExpertFuseStats st;
        const bool ok = basalt::core::lora_fuse_experts(ws, experts, m.lay, 2, (tmp / "wrong.experts").string(), st, err);
        check(!ok && err.find("gate") != std::string::npos && err.find("layer 0") != std::string::npos,
              "an adapter whose shapes disagree with the layout is refused by name: " + err);
        check(!fs::exists(tmp / "wrong.experts.part"), "a failed fusion removes its partial file");
    }
    {
        std::vector<fixture::Tensor> deep;
        add_stacked(deep, 2, "gate", make_stacked(rng, 4, kNEmbd, kNFf, 2, 10));
        write_adapter(tmp / "deep.gguf", deep);
        LoraAdapter d;
        LoraAdapter::open((tmp / "deep.gguf").string(), d, err);
        LoraSet ds({LoraEntry{&d, 1.0f}});
        ExpertFuseStats st;
        const bool ok = basalt::core::lora_fuse_experts(ds, experts, m.lay, 2, (tmp / "deep.experts").string(), st, err);
        check(!ok && err.find("layer 2") != std::string::npos, "an adapter with more layers than the model is refused: " + err);
    }

    // ---- 5. the file the engine loads
    {
        const fs::path cached_adapter = tmp / "cached_adapter.gguf";
        write_adapter(cached_adapter, ts);
        FileRange out;
        {
            LoraAdapter a1;
            check(LoraAdapter::open(cached_adapter.string(), a1, err), "the adapter opens: " + err);
            LoraSet s1set({LoraEntry{&a1, 1.0f}});
            ExpertFuseStats st;
            check(basalt::core::lora_prepare_experts(s1set, experts, m.lay, 2, out, st, err), "prepare writes the fused file: " + err);
            check(!st.reused && out.path != experts.path && out.offset == 0 && out.bytes == m.lay.total,
                  "the fused file is a whole file of the section's size");
            check(fs::path(out.path).parent_path() == tmp, "it is written in the adapter's directory");
            check(read_all(out.path) == b1, "and holds the same bytes as a direct fusion");
            FileRange again;
            ExpertFuseStats st2;
            check(basalt::core::lora_prepare_experts(s1set, experts, m.lay, 2, again, st2, err) && st2.reused && again.path == out.path,
                  "the next load reuses it");
        }
        // the adapter changes (a different rank, so a different size): a new file, and the old one is gone
        std::vector<fixture::Tensor> v2;
        add_stacked(v2, 0, "gate", make_stacked(rng, 2, kNEmbd, kNFf, 2, 10));
        write_adapter(cached_adapter, v2);
        LoraAdapter a2;
        check(LoraAdapter::open(cached_adapter.string(), a2, err), "the changed adapter opens: " + err);
        LoraSet s2set({LoraEntry{&a2, 1.0f}});
        FileRange out2;
        ExpertFuseStats st3;
        check(basalt::core::lora_prepare_experts(s2set, experts, m.lay, 2, out2, st3, err), "prepare for the changed adapter: " + err);
        check(!st3.reused && out2.path != out.path, "a changed adapter gets another file");
        check(!fs::exists(out.path), "the file of the earlier adapter is removed");

        // no adapter with experts: the section itself
        LoraSet none;
        FileRange same;
        ExpertFuseStats st4;
        check(basalt::core::lora_prepare_experts(none, experts, m.lay, 2, same, st4, err) && same.path == experts.path,
              "without expert tensors the experts range is the section itself");
    }
}

}  // namespace

int main(int argc, char** argv) {
    const fs::path dir = argc > 1 ? fs::path(argv[1]) : fs::path("tools/lora_training/testdata");
    const fs::path tmp = fs::temp_directory_path() / "basalt_lora_experts_test";
    fs::remove_all(tmp);
    fs::create_directories(tmp);
    try {
        parity(dir);
        readers(dir, tmp);
        fusion(tmp);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: exception: %s\n", e.what());
        ++failures;
    }
    fs::remove_all(tmp);
    if (failures) {
        std::fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    std::printf("lora_experts_test: ok\n");
    return 0;
}
