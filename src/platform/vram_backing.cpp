// src/platform/vram_backing.cpp - see include/basalt/platform/vram_backing.hpp.
#include "basalt/platform/vram_backing.hpp"

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#include <powrprof.h>

#include <algorithm>
#include <cstdio>
#include <iterator>
#include <map>
#include <mutex>
#include <set>
#include <vector>
#endif

namespace basalt::platform {

#if defined(_WIN32)
namespace {

constexpr DWORD kBackingProtect = PAGE_READWRITE | PAGE_WRITECOMBINE;

std::mutex g_mu;
std::map<uintptr_t, SIZE_T> g_released;   // base -> size of the regions a sweep decommitted
std::set<uintptr_t> g_judged;             // allocation bases of backing regions a sweep has already looked at

// Every page outside the working set: the CPU never touched the region and no eviction wrote into it.
bool untouched(uint8_t* base, SIZE_T size, SIZE_T page, std::vector<PSAPI_WORKING_SET_EX_INFORMATION>& q) {
    constexpr SIZE_T kBatch = 16384;
    const SIZE_T pages = size / page;
    for (SIZE_T first = 0; first < pages; first += kBatch) {
        const SIZE_T n = std::min(kBatch, pages - first);
        q.assign(n, PSAPI_WORKING_SET_EX_INFORMATION{});
        for (SIZE_T i = 0; i < n; ++i) q[i].VirtualAddress = base + (first + i) * page;
        if (!QueryWorkingSetEx(GetCurrentProcess(), q.data(), (DWORD) (n * sizeof q[0]))) return false;
        for (const auto& w : q)
            if (w.VirtualAttributes.Valid) return false;
    }
    return true;
}

// The region at `base` is still a backing allocation (not freed and taken by something else since).
bool still_backing(uint8_t* base, MEMORY_BASIC_INFORMATION& m) {
    return VirtualQuery(base, &m, sizeof m) == sizeof m && m.AllocationBase == base && m.Type == MEM_PRIVATE &&
           m.AllocationProtect == kBackingProtect;
}

ULONG CALLBACK on_power(PVOID, ULONG type, PVOID) {
    if (type != PBT_APMSUSPEND) return 0;
    uint64_t refused = 0;
    const uint64_t back = restore_vram_backing(&refused);
    if (back > 0 || refused > 0)
        std::fprintf(stderr, "basalt serve: going to sleep: %.2f GiB of VRAM backing committed again%s\n",
                     (double) back / 1073741824.0,
                     refused > 0 ? " (the commit limit refused the rest: the VRAM may not survive the sleep)" : "");
    return 0;
}

}  // namespace

bool vram_backing_supported() { return true; }

BackingSweep release_vram_backing() {
    BackingSweep s;
    std::lock_guard<std::mutex> lk(g_mu);
    SYSTEM_INFO si{};
    GetSystemInfo(&si);
    const SIZE_T page = si.dwPageSize;
    std::vector<PSAPI_WORKING_SET_EX_INFORMATION> q;
    std::set<uintptr_t> alive;
    MEMORY_BASIC_INFORMATION m{};
    for (uint8_t* p = nullptr; VirtualQuery(p, &m, sizeof m) == sizeof m; p = (uint8_t*) m.BaseAddress + m.RegionSize) {
        if (m.Type != MEM_PRIVATE || m.AllocationProtect != kBackingProtect) continue;
        const uintptr_t key = (uintptr_t) m.AllocationBase;
        alive.insert(key);
        if (m.State != MEM_COMMIT || m.Protect != kBackingProtect || m.BaseAddress != m.AllocationBase) continue;
        if (!g_judged.insert(key).second) {
            // judged before: released and committed again by someone else is a new allocation at the same address
            // (the driver reuses them); anything else keeps its verdict
            const auto was = g_released.find(key);
            if (was == g_released.end()) continue;
            g_released.erase(was);
        }
        uint8_t* base = (uint8_t*) m.BaseAddress;
        if (untouched(base, m.RegionSize, page, q) && VirtualFree(base, m.RegionSize, MEM_DECOMMIT)) {
            g_released[key] = m.RegionSize;
            s.released_bytes += m.RegionSize;
            ++s.released_regions;
        } else {
            s.kept_bytes += m.RegionSize;
        }
    }
    // allocations freed since: forget them, so a new one at the same address is judged afresh
    for (auto it = g_judged.begin(); it != g_judged.end();) it = alive.count(*it) ? std::next(it) : g_judged.erase(it);
    for (auto it = g_released.begin(); it != g_released.end();)
        it = alive.count(it->first) ? std::next(it) : g_released.erase(it);
    for (const auto& r : g_released) s.total_released += r.second;
    return s;
}

uint64_t restore_vram_backing(uint64_t* refused) {
    std::lock_guard<std::mutex> lk(g_mu);
    uint64_t done = 0, no = 0;
    std::map<uintptr_t, SIZE_T> left;
    MEMORY_BASIC_INFORMATION m{};
    for (const auto& [key, size] : g_released) {
        uint8_t* base = (uint8_t*) key;
        if (!still_backing(base, m) || m.State == MEM_COMMIT) continue;
        if (VirtualAlloc(base, size, MEM_COMMIT, kBackingProtect) != nullptr) {
            done += size;
        } else {
            no += size;
            left.emplace(key, size);
        }
    }
    g_released.swap(left);   // restored regions stay judged: no later sweep releases them again
    if (refused) *refused = no;
    return done;
}

bool restore_vram_backing_on_suspend(std::string& why) {
    static std::once_flag once;
    static bool ok = false;
    static std::string err;
    std::call_once(once, [] {
        // powrprof.dll is loaded when asked, not linked: a start without --release-vram-commit keeps its imports
        HMODULE dll = LoadLibraryA("powrprof.dll");
        using Register = DWORD(WINAPI*)(DWORD, HANDLE, PHPOWERNOTIFY);
        const auto reg = dll ? (Register) (void*) GetProcAddress(dll, "PowerRegisterSuspendResumeNotification") : nullptr;
        if (reg == nullptr) {
            err = "PowerRegisterSuspendResumeNotification not found";
            return;
        }
        static DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS params{on_power, nullptr};
        HPOWERNOTIFY handle = nullptr;
        const DWORD rc = reg(DEVICE_NOTIFY_CALLBACK, &params, &handle);
        ok = rc == ERROR_SUCCESS;
        if (!ok) err = "PowerRegisterSuspendResumeNotification failed (error " + std::to_string((unsigned long) rc) + ")";
    });
    why = err;
    return ok;
}
#else
bool vram_backing_supported() { return false; }
BackingSweep release_vram_backing() { return {}; }
uint64_t restore_vram_backing(uint64_t* refused) {
    if (refused) *refused = 0;
    return 0;
}
bool restore_vram_backing_on_suspend(std::string& why) {
    why = "no WDDM backing on this platform";
    return false;
}
#endif

}  // namespace basalt::platform
