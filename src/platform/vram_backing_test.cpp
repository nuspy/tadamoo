// src/platform/vram_backing_test.cpp - release_vram_backing on real device allocations: the commit it frees, the
// allocation still read back intact, a region judged only once, a freed address reused by a new allocation,
// restore_vram_backing, cudaFree afterwards.
#include "basalt/platform/vram_backing.hpp"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstdint>
#include <vector>
#if defined(_WIN32)
#include <windows.h>
#include <psapi.h>
#endif

namespace {
int fail = 0;
void check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    fail += ok ? 0 : 1;
}
#if defined(_WIN32)
uint64_t private_bytes() {
    PROCESS_MEMORY_COUNTERS_EX pm{};
    pm.cb = sizeof pm;
    GetProcessMemoryInfo(GetCurrentProcess(), (PROCESS_MEMORY_COUNTERS*) &pm, sizeof pm);
    return pm.PrivateUsage;
}
#endif
}  // namespace

int main() {
    using namespace basalt::platform;
    if (!vram_backing_supported()) {
        const BackingSweep s = release_vram_backing();
        check(s.released_bytes == 0 && s.total_released == 0, "no backing off Windows: a sweep releases nothing");
        return fail ? 1 : 0;
    }
#if defined(_WIN32)
    if (cudaFree(0) != cudaSuccess) {
        std::printf("skip: no CUDA device\n");
        return 0;
    }
    release_vram_backing();   // the context's own allocations, so the next sweep sees only the one below
    const size_t n = (size_t) 1 << 30;
    uint8_t* d = nullptr;
    check(cudaMalloc((void**) &d, n) == cudaSuccess, "cudaMalloc 1 GiB");
    check(cudaMemset(d, 0x5A, n) == cudaSuccess && cudaDeviceSynchronize() == cudaSuccess, "fill");
    const uint64_t charged = private_bytes();
    const BackingSweep s = release_vram_backing();
    const uint64_t after = private_bytes();
    std::printf("     released %.3f GiB in %llu regions, kept %.3f GiB; private %.3f -> %.3f GiB\n",
                (double) s.released_bytes / 1073741824.0, (unsigned long long) s.released_regions,
                (double) s.kept_bytes / 1073741824.0, (double) charged / 1073741824.0, (double) after / 1073741824.0);
    check(s.released_bytes >= n, "the allocation's whole backing is released");
    check(charged - after >= n, "the process commit falls by at least the allocation");

    std::vector<uint8_t> h(n);
    bool same = cudaMemcpy(h.data(), d, n, cudaMemcpyDeviceToHost) == cudaSuccess;
    for (size_t i = 0; same && i < n; i += 4096) same = h[i] == 0x5A;
    check(same, "the allocation reads back intact after the release");
    check(cudaMemset(d, 0x33, n) == cudaSuccess && cudaDeviceSynchronize() == cudaSuccess, "and takes new writes");

    const BackingSweep again = release_vram_backing();
    check(again.released_bytes == 0 && again.total_released == s.total_released, "a second sweep releases nothing new");

    // freed with its backing released, then a new allocation of the same size: the driver may hand back the same
    // addresses, which must be judged as the new allocation they are
    uint8_t* d2 = nullptr;
    check(cudaMalloc((void**) &d2, n) == cudaSuccess, "a second allocation");
    check(release_vram_backing().released_bytes >= n, "its backing is released");
    check(cudaFree(d2) == cudaSuccess, "cudaFree with the backing released");
    uint8_t* d3 = nullptr;
    check(cudaMalloc((void**) &d3, n) == cudaSuccess, "a third allocation, right after");
    check(release_vram_backing().released_bytes >= n, "its backing is released too");
    check(cudaMemset(d3, 0x11, n) == cudaSuccess && cudaMemcpy(h.data(), d3, n, cudaMemcpyDeviceToHost) == cudaSuccess &&
              h[n - 1] == 0x11, "it reads back what it was given");

    const uint64_t low = private_bytes();
    uint64_t refused = 1;
    const uint64_t back = restore_vram_backing(&refused);
    check(back >= 2 * n && refused == 0, "restore commits every released region again");
    check(private_bytes() >= low + 2 * n, "the commit is charged again");
    check(release_vram_backing().released_bytes == 0, "a restored region is never released again");
    check(cudaFree(d) == cudaSuccess && cudaFree(d3) == cudaSuccess, "cudaFree after release and restore");
#endif
    std::printf("%s\n", fail ? "FAILED" : "all passed");
    return fail ? 1 : 0;
}
