// tests/models/qwen4exp/kernels/wscale_parity.cpp - the write scale of the hyper-connection kernels (GPU, synthetic, no model).
//
// Every kernel that writes a half's block output into the residual streams takes `wscale`, a factor on its weight
// 2*sigmoid(inj/hc).  For each of them, at wscale 1, 0.5 and 0: R_out against a double-precision CPU reference
//   R + bo * wscale * 2*sigmoid(inj / hc),
// and, at 0, R_out equal to R bit for bit.  Covered here: the decode gr_write, fused_gr_read (one token and a window),
// and the prefill gr_write and gr_write_norm_rs.  The fused hc read and the 8-bit path are held to the same by
// hc_fused_parity and hc8_parity run with --wscale.
//
//     build/wscale_parity
#include "basalt/models/qwen4exp/kernels/fused_gr.hpp"
#include "basalt/models/qwen4exp/kernels/gr.hpp"
#include "basalt/models/qwen4exp/prefill/kernels.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace K = basalt::kernels;
namespace P = basalt::prefill;

namespace {

constexpr int N = 2560, HC = 4, D = N * HC, LR = 320;
int g_bad = 0;

void ck(cudaError_t e, const char* what) {
    if (e != cudaSuccess) {
        std::printf("FAIL cuda %s: %s\n", what, cudaGetErrorString(e));
        std::exit(1);
    }
}
template <class T> T* dev(const std::vector<T>& v) {
    T* d = nullptr;
    ck(cudaMalloc((void**) &d, v.size() * sizeof(T)), "malloc");
    ck(cudaMemcpy(d, v.data(), v.size() * sizeof(T), cudaMemcpyHostToDevice), "copy in");
    return d;
}
template <class T> std::vector<T> host(const T* d, size_t n) {
    std::vector<T> v(n);
    ck(cudaMemcpy(v.data(), d, n * sizeof(T), cudaMemcpyDeviceToHost), "copy out");
    return v;
}
uint16_t bf16(float x) {
    uint32_t u;
    std::memcpy(&u, &x, 4);
    return (uint16_t) ((u + 0x7fff + ((u >> 16) & 1)) >> 16);
}

struct Case {
    std::vector<float> R, bo, inj;
    int T;
};
Case make(int T, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> n01(0.0f, 1.0f);
    Case c;
    c.T = T;
    c.R.resize((size_t) T * D);
    c.bo.resize((size_t) T * N);
    c.inj.resize((size_t) T * HC);
    for (auto& x : c.R) x = n01(rng);
    for (auto& x : c.bo) x = n01(rng);
    for (auto& x : c.inj) x = 3.0f * n01(rng);
    return c;
}

// R + bo * ws * 2 sigmoid(inj / hc), in double, for token t, stream c, column d
double want_at(const Case& k, int t, int c, int d, float ws) {
    const double w = 2.0 / (1.0 + std::exp(-(double) k.inj[(size_t) t * HC + c] / (double) HC));
    return (double) k.R[((size_t) t * HC + c) * N + d] + (double) k.bo[(size_t) t * N + d] * (double) ws * w;
}

void judge(const char* name, const Case& k, const std::vector<float>& got, float ws) {
    double worst = 0.0;
    bool exact = true;
    for (int t = 0; t < k.T; ++t)
        for (int c = 0; c < HC; ++c)
            for (int d = 0; d < N; ++d) {
                const size_t i = ((size_t) t * HC + c) * N + d;
                const double w = want_at(k, t, c, d, ws);
                worst = std::max(worst, std::fabs(w - (double) got[i]) / (std::fabs(w) + std::fabs((double) k.bo[(size_t) t * N + d]) + 1e-3));
                if (ws == 0.0f && std::memcmp(&got[i], &k.R[i], 4) != 0) exact = false;
            }
    const bool ok = worst < 3e-6 && exact;
    std::printf("  %-34s wscale %.1f  worst rel %.2e%s %s\n", name, ws, worst, ws == 0.0f ? (exact ? "  R unchanged" : "  R CHANGED") : "",
                ok ? "ok" : "*** FAIL ***");
    if (!ok) ++g_bad;
}

const float kScales[3] = {1.0f, 0.5f, 0.0f};

void decode_gr_write() {
    const Case k = make(1, 11);
    float *dR = dev(k.R), *dbo = dev(k.bo), *dinj = dev(k.inj), *dout = nullptr;
    ck(cudaMalloc((void**) &dout, (size_t) D * 4), "malloc out");
    K::GrShapes s{};
    s.n_embd = N;
    s.hc = HC;
    s.hc_lr = LR;
    for (const float ws : kScales) {
        K::gr_write(dR, dbo, dinj, s, dout, ws, nullptr);
        judge("decode gr_write", k, host(dout, D), ws);
    }
    cudaFree(dR); cudaFree(dbo); cudaFree(dinj); cudaFree(dout);
}

struct Weights {
    float* norm;
    uint16_t *down, *up, *inj;
};
Weights make_weights() {
    std::mt19937 rng(77);
    std::normal_distribution<float> n01(0.0f, 0.05f);
    std::vector<float> wn((size_t) D, 1.0f);
    std::vector<uint16_t> wd((size_t) LR * D), wu((size_t) D * LR), wi((size_t) HC * D);
    for (auto& x : wd) x = bf16(n01(rng));
    for (auto& x : wu) x = bf16(n01(rng));
    for (auto& x : wi) x = bf16(n01(rng));
    return {dev(wn), dev(wd), dev(wu), dev(wi)};
}

void fused_reads() {
    const Weights w = make_weights();
    for (const int T : {1, 3}) {
        const Case k = make(T, 21 + (uint32_t) T);
        float *dR = dev(k.R), *dbo = dev(k.bo), *dinj = dev(k.inj);
        float *dout = nullptr, *dlo = nullptr, *drs = nullptr, *dio = nullptr, *dmix = nullptr, *dxn = nullptr;
        ck(cudaMalloc((void**) &dout, (size_t) T * D * 4), "malloc");
        ck(cudaMalloc((void**) &dlo, (size_t) T * LR * 4), "malloc");
        ck(cudaMalloc((void**) &drs, (size_t) T * HC * 4), "malloc");
        ck(cudaMalloc((void**) &dio, (size_t) T * HC * 4), "malloc");
        ck(cudaMalloc((void**) &dmix, (size_t) T * N * 4), "malloc");
        ck(cudaMalloc((void**) &dxn, (size_t) T * D * 4), "malloc");
        for (const float ws : kScales) {
            std::vector<K::FusedGrArgs> a((size_t) T);
            for (int t = 0; t < T; ++t) {
                K::FusedGrArgs& x = a[(size_t) t];
                x.R = dR + (size_t) t * D;
                x.R_out = dout + (size_t) t * D;
                x.apply = true;
                x.bo_prev = dbo + (size_t) t * N;
                x.inj_prev = dinj + (size_t) t * HC;
                x.wscale = ws;
                x.w_norm = w.norm;
                x.w_down = w.down;
                x.w_up = w.up;
                x.w_inject = w.inj;
                x.lo = dlo + (size_t) t * LR;
                x.rs = drs + (size_t) t * HC;
                x.inject_out = dio + (size_t) t * HC;
                x.mixed = dmix + (size_t) t * N;
            }
            ck(cudaMemset(dout, 0xff, (size_t) T * D * 4), "memset");
            if (T == 1) K::fused_gr_read(a[0], nullptr);
            else K::fused_gr_read_multi(a.data(), T, dxn, nullptr);
            ck(cudaDeviceSynchronize(), "sync");
            judge(T == 1 ? "fused_gr_read, one token" : "fused_gr_read_multi, 3 tokens", k, host(dout, (size_t) T * D), ws);
        }
        cudaFree(dR); cudaFree(dbo); cudaFree(dinj); cudaFree(dout); cudaFree(dlo); cudaFree(drs); cudaFree(dio);
        cudaFree(dmix); cudaFree(dxn);
    }
}

void prefill_writes() {
    const int T = 5;
    const Case k = make(T, 31);
    float *dbo = dev(k.bo), *dinj = dev(k.inj);
    std::vector<float> wn((size_t) D, 1.0f);
    float* dwn = dev(wn);
    float *dR = nullptr, *drs = nullptr;
    uint16_t* dxn16 = nullptr;
    ck(cudaMalloc((void**) &dR, (size_t) T * D * 4), "malloc");
    ck(cudaMalloc((void**) &drs, (size_t) T * HC * 4), "malloc");
    ck(cudaMalloc((void**) &dxn16, (size_t) T * D * 2), "malloc");
    for (const float ws : kScales) {
        ck(cudaMemcpy(dR, k.R.data(), k.R.size() * 4, cudaMemcpyHostToDevice), "copy R");
        P::gr_write(dR, dbo, dinj, HC, T, ws, nullptr);
        ck(cudaDeviceSynchronize(), "sync");
        judge("prefill gr_write", k, host(dR, (size_t) T * D), ws);
        ck(cudaMemcpy(dR, k.R.data(), k.R.size() * 4, cudaMemcpyHostToDevice), "copy R");
        P::gr_write_norm_rs(dR, dbo, dinj, HC, dwn, 1e-6f, drs, dxn16, T, ws, nullptr);
        ck(cudaDeviceSynchronize(), "sync");
        judge("prefill gr_write_norm_rs", k, host(dR, (size_t) T * D), ws);
    }
    cudaFree(dbo); cudaFree(dinj); cudaFree(dwn); cudaFree(dR); cudaFree(drs); cudaFree(dxn16);
}

}  // namespace

int main() {
    int n = 0;
    if (cudaGetDeviceCount(&n) != cudaSuccess || n == 0) {
        std::printf("wscale_parity: no CUDA device\n");
        return 77;
    }
    std::printf("wscale_parity\n");
    decode_gr_write();
    fused_reads();
    prefill_writes();
    std::printf(g_bad == 0 ? "wscale_parity OK\n" : "wscale_parity FAILED (%d)\n", g_bad);
    return g_bad == 0 ? 0 : 1;
}
