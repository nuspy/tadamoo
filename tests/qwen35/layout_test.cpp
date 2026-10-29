// tests/qwen35/layout_test.cpp - the qwen35moe family check on synthetic GGUFs: no model, no GPU.
//
//   * model_family names qwen35moe, qwen4exp and an unknown architecture; the qwen35moe guard names a missing key;
//   * geometry_from_gguf reads Qwen3.6-35B-A3B's metadata values (hidden 2048, 40 + 1 MTP layers, 32 GDN V heads,
//     conv 8192, 256 experts top-8) and refuses an inconsistent ssm.inner_size or another architecture;
//   * the layout table, on a tiny file: a complete file passes, a wrong shape, a missing tensor and a tensor the
//     table does not name are refused, naming the tensor.
//
// `layout_test --real SHARD`: the same view of a real model (headers only): family, geometry and the layout check
// over every shard.
#include "../core/gguf_fixture.hpp"

#include "basalt/qwen35/layout.hpp"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {
int g_fail = 0;
void check(bool ok, const std::string& what) {
    std::printf("  %-90s %s\n", what.c_str(), ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}

struct TempDir {
    fs::path path;
    TempDir() {
        path = fs::temp_directory_path() /
               ("basalt-qwen35-layout-test-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        fs::create_directories(path);
    }
    ~TempDir() {
        std::error_code ignored;
        fs::remove_all(path, ignored);
    }
};

using fixture::Kv;
using fixture::Tensor;
const uint32_t F32 = 0;

struct Dims {
    uint64_t block_count, embd, heads, heads_kv, key_len, experts, used, ff, ff_sh, conv, state, groups, v_heads,
        inner, interval, nextn;
};
// Qwen3.6-35B-A3B as its GGUF says, and a tiny model of the same shape family (every tensor F32, a few KB)
const Dims kQwen36 = {41, 2048, 16, 2, 256, 256, 8, 512, 512, 4, 128, 16, 32, 4096, 4, 1};
const Dims kTiny36 = {5, 64, 2, 1, 16, 4, 2, 32, 32, 4, 8, 2, 4, 32, 4, 1};

std::vector<Kv> metadata(const char* arch, const Dims& d, uint64_t vocab) {
    const std::string p = std::string(arch) + ".";
    std::vector<Kv> kv = {fixture::str("general.architecture", arch),
                          fixture::u32(p + "block_count", d.block_count),
                          fixture::u32(p + "embedding_length", d.embd),
                          fixture::u32(p + "attention.head_count", d.heads),
                          fixture::u32(p + "attention.head_count_kv", d.heads_kv),
                          fixture::u32(p + "attention.key_length", d.key_len),
                          fixture::u32(p + "attention.value_length", d.key_len),
                          fixture::u32(p + "expert_count", d.experts),
                          fixture::u32(p + "expert_used_count", d.used),
                          fixture::u32(p + "expert_feed_forward_length", d.ff),
                          fixture::u32(p + "expert_shared_feed_forward_length", d.ff_sh),
                          fixture::u32(p + "ssm.conv_kernel", d.conv),
                          fixture::u32(p + "ssm.state_size", d.state),
                          fixture::u32(p + "ssm.group_count", d.groups),
                          fixture::u32(p + "ssm.time_step_rank", d.v_heads),
                          fixture::u32(p + "ssm.inner_size", d.inner),
                          fixture::u32(p + "full_attention_interval", d.interval),
                          fixture::u32(p + "nextn_predict_layers", d.nextn)};
    std::vector<std::string> tokens;
    for (uint64_t i = 0; i < vocab; ++i) tokens.push_back("t" + std::to_string(i));
    kv.push_back(fixture::str_array("tokenizer.ggml.tokens", tokens));
    return kv;
}

/// The tensors the layout table of `g` names, as F32 fixtures.
std::vector<Tensor> tensors_of(const basalt::qwen35::ModelGeometry& g) {
    std::vector<Tensor> ts;
    uint8_t seed = 1;
    auto add = [&](const std::string& name, const std::vector<int64_t>& shape) {
        std::vector<uint64_t> s(shape.begin(), shape.end());
        ts.push_back({name, s, F32, seed++});
    };
    for (const auto& w : basalt::qwen35::global_tensors(g)) add(w.name, w.shape);
    for (int64_t l = 0; l < g.n_layers + g.n_mtp_layers; ++l)
        for (const auto& w : basalt::qwen35::layer_tensors(g, l)) add("blk." + std::to_string(l) + "." + w.name, w.shape);
    return ts;
}

bool geometry_of(const fs::path& p, basalt::qwen35::ModelGeometry& g, int64_t& k, std::string& err) {
    const basalt::GgufFile f(p.string());
    return basalt::qwen35::geometry_from_gguf(f, g, k, err);
}

std::string layout_error(const fs::path& dir, const Dims& d, uint64_t vocab, void (*mutate)(std::vector<Tensor>&)) {
    const fs::path meta = dir / "meta.gguf";
    fixture::write(meta, metadata("qwen35moe", d, vocab), {});
    basalt::qwen35::ModelGeometry g;
    int64_t k = 0;
    std::string err;
    if (!geometry_of(meta, g, k, err)) return "geometry: " + err;
    std::vector<Tensor> ts = tensors_of(g);
    if (mutate) mutate(ts);
    const fs::path model = dir / "qwen35moe-tiny.gguf";
    fixture::write(model, metadata("qwen35moe", d, vocab), ts);
    const basalt::GgufModel m({model.string()});
    size_t n = 0;
    if (!basalt::qwen35::check_gguf_layout(m, g, err, &n)) return err;
    return {};
}

int real_model(const std::string& any) {
    try {
        const basalt::GgufModel m = basalt::GgufModel::open(any);
        basalt::qwen35::ModelGeometry g;
        int64_t k = 0;
        std::string err;
        if (!basalt::qwen35::geometry_from_gguf(m.meta(), g, k, err)) {
            std::printf("  geometry: %s\n", err.c_str());
            return 1;
        }
        std::printf("  %zu shard(s), family %s: layers %lld (+%lld MTP), hidden %lld, attention %lld/%lld x %lld every %lld,"
                    " GDN K %lld V %lld x %lld conv %lld (%lld), MoE %lld experts top-%lld FF %lld shared %lld, vocab %lld\n",
                    m.size(), basalt::model_family_title(g.family), (long long) g.n_layers, (long long) g.n_mtp_layers,
                    (long long) g.n_embd, (long long) g.n_head, (long long) g.n_head_kv, (long long) g.head_dim,
                    (long long) g.qsa_interval, (long long) g.ssm_k_heads, (long long) g.ssm_v_heads,
                    (long long) g.ssm_state_size, (long long) g.ssm_conv_channels, (long long) g.ssm_d_conv,
                    (long long) g.n_expert, (long long) k, (long long) g.n_ff, (long long) g.n_ff_shexp,
                    (long long) g.n_vocab);
        size_t n = 0;
        const bool ok = basalt::qwen35::check_gguf_layout(m, g, err, &n);
        std::printf("  layout: %s (%zu tensors checked)\n", ok ? "ok" : err.c_str(), n);
        return ok ? 0 : 1;
    } catch (const std::exception& e) {
        std::printf("  refused: %s\n", e.what());
        return 1;
    }
}
}  // namespace

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--real") return real_model(argv[2]);
    std::printf("qwen35 layout_test\n");
    TempDir d;
    {
        const fs::path q36 = d.path / "q36.gguf", fn = d.path / "fn.gguf", other = d.path / "other.gguf";
        fixture::write(q36, metadata("qwen35moe", kQwen36, 8), {});
        fixture::write(fn, metadata("qwen4exp", kQwen36, 8), {});
        fixture::write(other, {fixture::str("general.architecture", "llama")}, {});
        const basalt::GgufFile a(q36.string()), b(fn.string()), c(other.string());
        check(basalt::model_family(a) == basalt::ModelFamily::Qwen35Moe &&
                  basalt::model_family(b) == basalt::ModelFamily::Qwen4Exp &&
                  basalt::model_family(c) == basalt::ModelFamily::Unknown,
              "model_family: qwen35moe, qwen4exp, unknown");
        check(basalt::check_architecture_qwen35moe(a).empty(), "the qwen35moe guard accepts the metadata");
        basalt::qwen35::ModelGeometry g;
        int64_t k = 0;
        std::string err;
        check(!basalt::qwen35::geometry_from_gguf(c, g, k, err) && err.find("'llama'") != std::string::npos,
              "another architecture is refused, naming it");
        check(!basalt::qwen35::geometry_from_gguf(b, g, k, err) && err.find("'qwen4exp'") != std::string::npos,
              "qwen4exp is not this engine path's family");
        std::vector<Kv> kv = metadata("qwen35moe", kQwen36, 8);
        for (auto it = kv.begin(); it != kv.end(); ++it)
            if (it->key == "qwen35moe.ssm.time_step_rank") { kv.erase(it); break; }
        const fs::path miss = d.path / "miss.gguf";
        fixture::write(miss, kv, {});
        check(basalt::check_architecture_qwen35moe(basalt::GgufFile(miss.string())).find("ssm.time_step_rank") !=
                  std::string::npos,
              "a file without ssm.time_step_rank is refused, naming the key");
        Dims plain = kQwen36;
        plain.block_count = 40;
        kv = metadata("qwen35moe", plain, 8);
        for (auto it = kv.begin(); it != kv.end(); ++it)
            if (it->key == "qwen35moe.nextn_predict_layers") { kv.erase(it); break; }
        const fs::path nomtp = d.path / "nomtp.gguf";
        fixture::write(nomtp, kv, {});
        check(geometry_of(nomtp, g, k, err) && g.n_mtp_layers == 0 && g.n_layers == 40,
              "a file without nextn_predict_layers has no MTP block");
    }
    {
        const fs::path q36 = d.path / "q36g.gguf";
        fixture::write(q36, metadata("qwen35moe", kQwen36, 248320), {});
        basalt::qwen35::ModelGeometry g;
        int64_t k = 0;
        std::string err;
        const bool ok = geometry_of(q36, g, k, err);
        check(ok, "Qwen3.6 geometry reads" + (ok ? std::string() : ": " + err));
        check(g.family == basalt::ModelFamily::Qwen35Moe && g.n_layers == 40 && g.n_mtp_layers == 1 && g.n_embd == 2048,
              "Qwen3.6: 40 trunk layers + 1 MTP block, hidden 2048");
        check(g.n_qsa_layers() == 10 && g.n_gdn_layers() == 30 &&
                  basalt::qwen35::layer_kind(g, 3) == basalt::qwen35::LayerKind::Attention &&
                  basalt::qwen35::layer_kind(g, 40) == basalt::qwen35::LayerKind::Mtp,
              "Qwen3.6: 10 attention / 30 GDN layers, layer 40 is the MTP block");
        check(g.ssm_k_heads == 16 && g.ssm_v_heads == 32 && g.ssm_conv_channels == 8192 && g.ssm_value_dim == 4096,
              "Qwen3.6 GDN: 16 K groups, 32 V heads, conv 8192, inner 4096");
        check(g.n_head == 16 && g.n_head_kv == 2 && g.head_dim == 256, "Qwen3.6 attention: 16 Q / 2 KV x 256");
        check(g.n_expert == 256 && k == 8 && g.n_ff == 512 && g.n_ff_shexp == 512 && g.n_vocab == 248320,
              "Qwen3.6 MoE: 256 experts top-8, FF 512, shared 512; vocab 248320");
        Dims wrong = kQwen36;
        wrong.inner = 4000;
        const fs::path wp = d.path / "wrong.gguf";
        fixture::write(wp, metadata("qwen35moe", wrong, 8), {});
        check(!geometry_of(wp, g, k, err) && err.find("ssm.inner_size") != std::string::npos,
              "an inconsistent ssm.inner_size is refused");
    }
    {
        check(layout_error(d.path, kTiny36, 16, nullptr).empty(),
              "a complete qwen35moe file passes its layout (incl. the nextn MTP block)");
        std::string e = layout_error(d.path, kTiny36, 16, [](std::vector<Tensor>& ts) {
            for (auto& t : ts)
                if (t.name == "blk.1.ssm_conv1d.weight") t.shape[1] += 1;
        });
        check(e.find("blk.1.ssm_conv1d.weight") != std::string::npos && e.find("wants") != std::string::npos,
              "a wrong shape is refused, naming the tensor and both shapes");
        e = layout_error(d.path, kTiny36, 16, [](std::vector<Tensor>& ts) {
            for (auto it = ts.begin(); it != ts.end(); ++it)
                if (it->name == "blk.4.nextn.eh_proj.weight") { ts.erase(it); break; }
        });
        check(e == "missing blk.4.nextn.eh_proj.weight", "a missing MTP tensor is refused");
        e = layout_error(d.path, kTiny36, 16, [](std::vector<Tensor>& ts) {
            ts.push_back({"blk.0.hc_attn_norm.weight", {64}, F32, 99});
        });
        check(e.find("blk.0.hc_attn_norm.weight") != std::string::npos && e.find("not in the") != std::string::npos,
              "a tensor the table does not name is refused");
    }
    std::printf(g_fail ? "FAILED (%d)\n" : "ALL PASSED\n", g_fail);
    return g_fail ? 1 : 0;
}
