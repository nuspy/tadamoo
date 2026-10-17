// src/platform/memory.cpp - see include/basalt/platform/memory.hpp.
#include "basalt/platform/memory.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <thread>
#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <dxgi1_4.h>
#include <cstring>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#endif

namespace basalt::platform {

#if defined(_WIN32)
// ------------------------------------------------------------------------------------------------ Windows
LockResult lock_resident(void* p, uint64_t bytes) {
    LockResult r;
    if (p == nullptr || bytes == 0) { r.note = "nothing to lock"; return r; }
    HANDLE self = GetCurrentProcess();
    SIZE_T min_ws = 0, max_ws = 0;
    DWORD flags = 0;
    if (!GetProcessWorkingSetSizeEx(self, &min_ws, &max_ws, &flags)) {
        r.note = "GetProcessWorkingSetSizeEx failed (error " + std::to_string(GetLastError()) + ")";
        return r;
    }
    // Locked pages count against the minimum working set, so it must grow by the region plus headroom for the rest
    // of the process.  Soft limits: the maximum is not enforced, only the minimum is raised.
    const SIZE_T margin = (SIZE_T) 512 << 20;
    const SIZE_T new_min = min_ws + (SIZE_T) bytes + margin;
    const SIZE_T new_max = max_ws > new_min + margin ? max_ws : new_min + margin;
    if (!SetProcessWorkingSetSizeEx(self, new_min, new_max,
                                    QUOTA_LIMITS_HARDWS_MIN_DISABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE)) {
        r.note = "SetProcessWorkingSetSizeEx(" + std::to_string((unsigned long long) (new_min >> 20)) +
                 " MiB) failed (error " + std::to_string(GetLastError()) + ")";
        return r;
    }
    const uint64_t chunk = 1ull << 30;
    uint8_t* base = (uint8_t*) p;
    for (uint64_t off = 0; off < bytes; off += chunk) {
        const uint64_t n = bytes - off < chunk ? bytes - off : chunk;
        if (!VirtualLock(base + off, (SIZE_T) n)) {
            r.note = "VirtualLock stopped at " + std::to_string((unsigned long long) (off >> 20)) + " of " +
                     std::to_string((unsigned long long) (bytes >> 20)) + " MiB (error " +
                     std::to_string(GetLastError()) + ")";
            r.ok = off > 0;
            return r;
        }
        r.locked_bytes = off + n;
    }
    r.ok = true;
    r.note = "locked " + std::to_string((unsigned long long) (bytes >> 20)) + " MiB via working-set minimum + VirtualLock";
    return r;
}

void unlock_resident(void* p, uint64_t bytes) {
    if (p == nullptr || bytes == 0) return;
    const uint64_t chunk = 1ull << 30;
    for (uint64_t off = 0; off < bytes; off += chunk)
        VirtualUnlock((uint8_t*) p + off, (SIZE_T) (bytes - off < chunk ? bytes - off : chunk));
}

namespace {
bool memory_status(MEMORYSTATUSEX& ms) {
    ms = MEMORYSTATUSEX{};
    ms.dwLength = sizeof ms;
    return GlobalMemoryStatusEx(&ms) != 0;
}
}  // namespace

uint64_t total_physical_memory() {
    MEMORYSTATUSEX ms;
    return memory_status(ms) ? (uint64_t) ms.ullTotalPhys : 0;
}

uint64_t available_commit() {
    MEMORYSTATUSEX ms;
    return memory_status(ms) ? (uint64_t) ms.ullAvailPageFile : 0;
}

// Windows charges every committed allocation (and the VRAM a WDDM driver backs with system memory) to the commit
// limit, RAM + page file.  What the engine may still take is the smaller of the free physical RAM and the commit
// left: a budget from the physical RAM alone could be granted only by growing into the page file.
uint64_t available_memory() {
    MEMORYSTATUSEX ms;
    if (!memory_status(ms)) return 0;
    return std::min<uint64_t>((uint64_t) ms.ullAvailPhys, (uint64_t) ms.ullAvailPageFile);
}

// 15% of the RAM stays free: Windows trims working sets and pages to the SSD well before RAM is exhausted, so the
// Linux 5% would let a load push the rest of the machine into the page file.
uint64_t ram_margin() {
    return std::max<uint64_t>(2ull << 30, total_physical_memory() / 100 * 15);
}

bool read_ahead_enabled() { return true; }

// PrefetchVirtualMemory (Windows 8+) queues the whole range at once, where the copies' page faults would read a few
// clusters each.  A hint: a range it does not fetch is faulted in by the read that follows.
void advise_willneed(const void* p, uint64_t bytes) {
    if (p == nullptr || bytes == 0 || !read_ahead_enabled()) return;
    WIN32_MEMORY_RANGE_ENTRY range{(PVOID) p, (SIZE_T) bytes};
    (void) PrefetchVirtualMemory(GetCurrentProcess(), 1, &range, 0);
}

// No fadvise for a file descriptor: the reads that follow go through the cache manager's own read-ahead.
void advise_willneed(int, uint64_t, uint64_t) {}

bool gpu_shared_memory_budget(const void* luid, uint64_t& budget, uint64_t& usage, std::string& why) {
    budget = usage = 0;
    // dxgi.dll is loaded when asked, not linked: a start that never needs this keeps the imports it had
    HMODULE dxgi = LoadLibraryA("dxgi.dll");
    if (dxgi == nullptr) { why = "dxgi.dll not found"; return false; }
    using CreateFactory = HRESULT(WINAPI*)(REFIID, void**);
    const auto create = (CreateFactory) (void*) GetProcAddress(dxgi, "CreateDXGIFactory1");
    IDXGIFactory1* factory = nullptr;
    if (create == nullptr || FAILED(create(__uuidof(IDXGIFactory1), (void**) &factory)) || factory == nullptr) {
        why = "CreateDXGIFactory1 failed";
        FreeLibrary(dxgi);
        return false;
    }
    bool ok = false;
    why = "no DXGI adapter has the CUDA device's LUID";
    for (UINT i = 0; !ok; ++i) {
        IDXGIAdapter1* a = nullptr;
        if (factory->EnumAdapters1(i, &a) == DXGI_ERROR_NOT_FOUND || a == nullptr) break;
        DXGI_ADAPTER_DESC1 d{};
        if (SUCCEEDED(a->GetDesc1(&d)) && std::memcmp(&d.AdapterLuid, luid, sizeof d.AdapterLuid) == 0) {
            IDXGIAdapter3* a3 = nullptr;
            DXGI_QUERY_VIDEO_MEMORY_INFO info{};
            if (SUCCEEDED(a->QueryInterface(__uuidof(IDXGIAdapter3), (void**) &a3)) && a3 != nullptr &&
                SUCCEEDED(a3->QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL, &info))) {
                budget = info.Budget;
                usage = info.CurrentUsage;
                ok = budget > 0;
                why = ok ? "" : "the adapter reports no shared-memory budget";
            } else {
                why = "QueryVideoMemoryInfo failed";
            }
            if (a3 != nullptr) a3->Release();
            a->Release();
            break;
        }
        a->Release();
    }
    factory->Release();
    FreeLibrary(dxgi);
    return ok;
}
#else
// ------------------------------------------------------------------------------------------------ Linux
LockResult lock_resident(void* p, uint64_t bytes) {
    LockResult r;
    if (p == nullptr || bytes == 0) { r.note = "nothing to lock"; return r; }
    if (mlock(p, bytes) != 0) { r.note = "mlock failed (raise ulimit -l)"; return r; }
    r.ok = true;
    r.locked_bytes = bytes;
    r.note = "mlock";
    return r;
}

void unlock_resident(void* p, uint64_t bytes) {
    if (p != nullptr && bytes != 0) munlock(p, bytes);
}

uint64_t total_physical_memory() {
    const long pages = sysconf(_SC_PHYS_PAGES), page = sysconf(_SC_PAGE_SIZE);
    return pages > 0 && page > 0 ? (uint64_t) pages * (uint64_t) page : 0;
}

namespace {
// One "<key>: <n> kB" line of /proc/meminfo in bytes (0 when absent).
uint64_t meminfo_bytes(const char* key) {
    std::ifstream in("/proc/meminfo");
    std::string line;
    const std::string k = std::string(key) + ":";
    while (std::getline(in, line))
        if (line.compare(0, k.size(), k) == 0) return std::strtoull(line.c_str() + k.size(), nullptr, 10) * 1024;
    return 0;
}
}  // namespace

uint64_t available_memory() { return meminfo_bytes("MemAvailable"); }

uint64_t ram_margin() {
    return std::max<uint64_t>(2ull << 30, meminfo_bytes("MemTotal") / 20);
}
#endif

uint64_t ram_budget(bool* known) {
    const uint64_t avail = available_memory();
    if (known) *known = avail > 0;
    if (avail == 0) return UINT64_MAX;
    const uint64_t margin = ram_margin();
    return avail > margin ? avail - margin : 0;
}

namespace {
std::atomic<bool> g_watch_stop{false};
std::atomic<bool> g_watch_on{false};
std::atomic<bool> g_resident_mode{false};
std::atomic<uint64_t> g_resident_copy{0};
}  // namespace

void watch_resident_ram(bool resident, uint64_t copy_bytes) {
    g_resident_mode = resident;
    g_resident_copy = copy_bytes;
}

void start_memory_watchdog(uint64_t floor_bytes) {
    if (g_watch_on.exchange(true)) return;
    g_watch_stop = false;
    std::thread([floor_bytes] {
        while (!g_watch_stop.load()) {
            const uint64_t avail = available_memory();
            if (avail > 0 && avail < floor_bytes) {
                const unsigned long long mib = (unsigned long long) (avail >> 20);
                if (g_resident_mode.load()) {
                    // the copy shrunk by what the margin is short of leaves the margin free again
                    const uint64_t copy = g_resident_copy.load(), margin = ram_margin();
                    const uint64_t keep = copy - std::min(copy, margin > avail ? margin - avail : 0);
                    std::fprintf(stderr, "basalt error: RAM ran out loading (%llu MiB free); use "
                                         "--resident-budget-gib %llu\n", mib, (unsigned long long) std::max<uint64_t>(1, keep >> 30));
                } else {
                    std::fprintf(stderr, "basalt error: RAM ran out loading (%llu MiB free); use "
                                         "--resident-experts or --mmap-experts\n", mib);
                }
                std::fflush(stderr);
                _exit(1);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }).detach();   // never joined: the process may end while it sleeps
}

void stop_memory_watchdog() {
    g_watch_stop = true;
    g_watch_on = false;
}

namespace {
std::atomic<bool> g_guard_on{false};
std::atomic<bool> g_guard_low{false};
std::atomic<uint64_t> g_guard_avail{0};
std::atomic<uint64_t> g_guard_floor{0};
}  // namespace

void start_ram_guard(uint64_t floor_bytes) {
    g_guard_floor = floor_bytes;
    if (g_guard_on.exchange(true)) return;
    std::thread([] {
        bool was_low = false;
        for (;;) {
            const uint64_t avail = available_memory(), floor = g_guard_floor.load();
            const bool low = ram_below_floor(avail, floor);
            g_guard_avail = avail;
            g_guard_low = low;
            if (low && !was_low) {
                std::fprintf(stderr, "basalt warning: RAM low: %llu MiB free < %llu MiB guard\n",
                             (unsigned long long) (avail >> 20), (unsigned long long) (floor >> 20));
                std::fflush(stderr);
            }
            was_low = low;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }).detach();   // runs for the life of the process
}

bool ram_guard_low(uint64_t* avail, uint64_t* floor) {
    if (avail) *avail = g_guard_avail.load();
    if (floor) *floor = g_guard_floor.load();
    return g_guard_low.load(std::memory_order_relaxed);
}

#if !defined(_WIN32)
namespace {
constexpr uint64_t kAdviseStep = 128ull << 10;
}

bool read_ahead_enabled() { return true; }

void advise_willneed(const void* p, uint64_t bytes) {
    if (p == nullptr || bytes == 0 || !read_ahead_enabled()) return;
    const long ps = sysconf(_SC_PAGE_SIZE);
    const uintptr_t pg = ps > 0 ? (uintptr_t) ps : 4096, end = (uintptr_t) p + bytes;
    for (uintptr_t a = (uintptr_t) p & ~(pg - 1); a < end; a += kAdviseStep)
        (void) madvise((void*) a, (size_t) std::min<uintptr_t>(kAdviseStep, end - a), MADV_WILLNEED);
}

void advise_willneed(int fd, uint64_t offset, uint64_t bytes) {
    if (fd < 0 || !read_ahead_enabled()) return;
    for (uint64_t at = 0; at < bytes; at += kAdviseStep)
        (void) posix_fadvise(fd, (off_t) (offset + at), (off_t) std::min(kAdviseStep, bytes - at), POSIX_FADV_WILLNEED);
}
#endif

}  // namespace basalt::platform
