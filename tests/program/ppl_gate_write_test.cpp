// tests/program/ppl_gate_write_test.cpp - the quality gate in write mode: it fills a base file's rows from head logits, at the offset the
// reader takes them from, and its output lines compare the logits with the row it just wrote (the quantization error only).
#include "basalt/platform/system.hpp"
#include "basalt/program/ppl_gate.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using basalt::program::PplGate;

static int g_fail = 0;
#define CHECK(c) do { if (!(c)) { std::printf("FAIL line %d: %s\n", __LINE__, #c); ++g_fail; } } while (0)

// An odd vocabulary, so a row carries its padding.
constexpr int kCtx = 8, kVocab = 7, kChunks = 2;
constexpr int kRows = kCtx - 1 - kCtx / 2;
constexpr int kRowU16 = 2 * ((kVocab + 1) / 2) + 4;

static std::vector<float> logits_of(int chunk, int row) {
    std::vector<float> lg(kVocab);
    for (int i = 0; i < kVocab; ++i) lg[(size_t) i] = 0.9f * (float) ((i * 5 + chunk * 3 + row) % kVocab) - 2.0f;
    lg[(size_t) ((row + 2) % kVocab)] -= 30.0f;   // below the -16 floor: its code is clamped to 0
    return lg;
}

int main() {
    const auto dir = std::filesystem::temp_directory_path() / ("basalt-ppl-write-" + std::to_string((long long) basalt::platform::process_id()));
    std::filesystem::create_directories(dir);
    const std::string base = (dir / "base.kld").string(), out = (dir / "rows.tsv").string();
    {
        std::ofstream f(base, std::ios::binary);
        f.write("_logits_", 8);
        const uint32_t ctx = kCtx;
        const int32_t v = kVocab, c = kChunks;
        f.write((const char*) &ctx, 4);
        f.write((const char*) &v, 4);
        f.write((const char*) &c, 4);
        for (int k = 0; k < kChunks; ++k)
            for (int i = 0; i < kCtx; ++i) {
                const int32_t t = (k * 3 + i) % kVocab;
                f.write((const char*) &t, 4);
            }
        const std::vector<char> rows((size_t) kChunks * kRows * kRowU16 * 2, 0);   // the space the engine fills
        f.write(rows.data(), (std::streamsize) rows.size());
    }
    PplGate::configure(base, out, true);
    PplGate& g = PplGate::get();
    CHECK(g.available());

    std::vector<int64_t> prompt;
    for (int i = 0; i <= kCtx / 2; ++i) prompt.push_back((3 + i) % kVocab);   // chunk 1's first half
    std::string err;
    CHECK(g.begin(1, prompt.data(), (int64_t) prompt.size(), kVocab, err));
    for (int r = 0; r < kRows; ++r) CHECK(g.score(g.first() + r, logits_of(1, r), err));
    CHECK(g.finish(err));

    // the bytes of chunk 1's rows, against the log-softmax of the logits that were scored
    std::ifstream f(base, std::ios::binary);
    for (int r = 0; r < kRows; ++r) {
        const std::streamoff off = 20 + (std::streamoff) kChunks * kCtx * 4 + ((std::streamoff) 1 * kRows + r) * kRowU16 * 2;
        std::vector<uint16_t> row(kRowU16);
        f.seekg(off);
        f.read((char*) row.data(), (std::streamsize) row.size() * 2);
        float scale, lo;
        std::memcpy(&scale, row.data(), 4);
        std::memcpy(&lo, row.data() + 2, 4);
        const std::vector<float> lg = logits_of(1, r);
        float mx = lg[0];
        for (float x : lg) mx = std::max(mx, x);
        double sum = 0.0;
        for (float x : lg) sum += std::exp(x - mx);
        const float lse = mx + (float) std::log(sum);
        CHECK(scale > 0.0f);
        CHECK(lo >= -16.0f - 1e-4f);
        for (int i = 0; i < kVocab; ++i) {
            const float lp = lg[(size_t) i] - lse;
            const float back = scale * row[(size_t) (4 + i)] + lo;
            if (lp >= lo) CHECK(std::fabs(back - lp) <= 0.51f * scale);
            else CHECK(row[(size_t) (4 + i)] == 0);
        }
        CHECK(row[(size_t) (4 + kVocab)] == 0);                            // the padding
    }
    // the other chunk's rows were not touched
    {
        std::vector<char> zero((size_t) kRows * kRowU16 * 2);
        f.seekg(20 + (std::streamoff) kChunks * kCtx * 4);
        f.read(zero.data(), (std::streamsize) zero.size());
        bool all = true;
        for (char c : zero) all = all && c == 0;
        CHECK(all);
    }

    // one output line per row; the logits against their own quantized row: the same top token, a KLD of rounding only
    std::ifstream rows(out);
    std::string line;
    int n = 0;
    while (std::getline(rows, line)) {
        std::istringstream in(line);
        std::vector<std::string> c;
        for (std::string x; std::getline(in, x, '\t');) c.push_back(x);
        CHECK(c.size() == 10);
        if (c.size() == 10) {
            CHECK(c[0] == "1");
            CHECK(c[1] == std::to_string(kCtx / 2 + n));
            CHECK(std::fabs(std::atof(c[3].c_str()) - std::atof(c[4].c_str())) < 2e-3);
            CHECK(std::atof(c[5].c_str()) < 1e-3);
            CHECK(c[6] == "1");
        }
        ++n;
    }
    CHECK(n == kRows);
    std::error_code ec;
    std::filesystem::remove_all(dir, ec);
    if (g_fail == 0) std::printf("ppl_gate_write_test OK\n");
    return g_fail == 0 ? 0 : 1;
}
