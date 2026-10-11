#pragma once
// include/basalt/platform/compat.hpp - the small spellings that differ between the Linux (GCC/Clang) and the
// Windows (MSVC) builds: CPUID/XGETBV, the spin-wait pause, 64-bit FILE* seeks, positional reads on a file
// descriptor, environment variables and the read-only file descriptor the loaders open.
//
// Header-only on purpose: the GGUF reader and other header-only parts use it without linking a library.  On Linux
// every function is the call the code made before (::pread, fseeko, setenv, __cpuid_count ...), so behaviour there
// is unchanged; the Windows bodies are the CRT / Win32 equivalents.
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#if !defined(__CUDACC__)
#include <intrin.h>
#include <immintrin.h>
#endif
#include <sys/stat.h>
#include <sys/types.h>
// POSIX's signed size, which MSVC does not define (some CUDA/third-party headers do, with the same type)
#if !defined(_SSIZE_T_DEFINED) && !defined(_SSIZE_T_)
#define _SSIZE_T_DEFINED
#define _SSIZE_T_
typedef intptr_t ssize_t;
#endif
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#if (defined(__x86_64__) || defined(__i386__)) && !defined(__CUDACC__)
#include <cpuid.h>
#endif
#endif

namespace basalt::platform {

// ------------------------------------------------------------------------------------------------ CPU
// Host C++ only: nvcc's front end does not know every compiler's x86 builtins, and no .cu file needs these.
#if !defined(__CUDACC__)
/// CPUID leaf `leaf`, sub-leaf `sub`: r = {eax, ebx, ecx, edx}.
inline void cpuid(unsigned leaf, unsigned sub, unsigned r[4]) {
#if defined(_MSC_VER)
    int x[4] = {0, 0, 0, 0};
    __cpuidex(x, (int) leaf, (int) sub);
    for (int i = 0; i < 4; ++i) r[i] = (unsigned) x[i];
#elif defined(__x86_64__) || defined(__i386__)
    __cpuid_count(leaf, sub, r[0], r[1], r[2], r[3]);
#else
    (void) leaf; (void) sub;
    r[0] = r[1] = r[2] = r[3] = 0;
#endif
}

/// XCR0, the state components the OS saves (bit 1 SSE, 2 AVX, 5-7 AVX-512).  Call only when CPUID.1:ECX.OSXSAVE.
inline unsigned long long xgetbv0() {
#if defined(_MSC_VER)
    return (unsigned long long) _xgetbv(0);
#elif defined(__x86_64__) || defined(__i386__)
    unsigned lo = 0, hi = 0;
    __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
    return ((unsigned long long) hi << 32) | lo;
#else
    return 0;
#endif
}

/// The spin-wait hint (PAUSE on x86).
inline void cpu_pause() {
#if defined(_MSC_VER)
    _mm_pause();
#elif defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#endif
}
#endif  // !__CUDACC__

// ------------------------------------------------------------------------------------------------ environment
/// setenv(name, value, overwrite): `overwrite` false keeps a value already set.
inline int set_env(const char* name, const char* value, bool overwrite = true) {
#if defined(_WIN32)
    if (!overwrite && std::getenv(name) != nullptr) return 0;
    return _putenv_s(name, value);
#else
    return ::setenv(name, value, overwrite ? 1 : 0);
#endif
}

// ------------------------------------------------------------------------------------------------ FILE*
/// 64-bit fseek: `fseek` takes a 32-bit `long` on Windows and wraps past 2 GiB.
inline int fseek64(std::FILE* f, long long off, int whence) {
#if defined(_WIN32)
    return _fseeki64(f, (__int64) off, whence);
#else
    return fseeko(f, (off_t) off, whence);
#endif
}

inline long long ftell64(std::FILE* f) {
#if defined(_WIN32)
    return (long long) _ftelli64(f);
#else
    return (long long) ftello(f);
#endif
}

#if defined(_WIN32)
/// UTF-8 -> UTF-16 for the wide Win32 file APIs: a path with non-ASCII characters would otherwise not be found.
inline std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int) s.size(), nullptr, 0);
    std::wstring w((size_t) (n > 0 ? n : 0), L'\0');
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), (int) s.size(), w.data(), n);
    return w;
}
#endif

/// rename(2): `from` replaces `to` when `to` exists (Windows' rename refuses an existing target).  0 on success.
inline int replace_file(const char* from, const char* to) {
#if defined(_WIN32)
    return MoveFileExW(widen(from).c_str(), widen(to).c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 0 : -1;
#else
    return std::rename(from, to);
#endif
}

// ------------------------------------------------------------------------------------------------ file descriptors
/// A read-only file descriptor (binary on Windows, where the CRT's default text mode would rewrite the bytes), or -1.
///
/// On Windows the descriptor wraps a handle opened for OVERLAPPED reads: a synchronous handle runs its reads one at
/// a time, while the loaders read one file from many threads at once (pread_at always passes an offset and waits for
/// its own read).  Such a descriptor is only for pread_at, fd_size, map_file_readonly and close_fd - never _read.
inline int open_read(const char* path) {
#if defined(_WIN32)
    HANDLE h = CreateFileW(widen(path).c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, nullptr);
    if (h == INVALID_HANDLE_VALUE) return -1;
    const int fd = _open_osfhandle((intptr_t) h, _O_RDONLY | _O_BINARY);
    if (fd < 0) CloseHandle(h);
    return fd;
#else
    return ::open(path, O_RDONLY);
#endif
}

inline int close_fd(int fd) {
#if defined(_WIN32)
    return _close(fd);
#else
    return ::close(fd);
#endif
}

/// The size of the regular file at `path`; false when it is missing or not a regular file.  (MSVC's plain `stat`
/// has a 32-bit size and fails on files over 2 GiB, so Windows asks _wstat64.)
inline bool path_file_size(const char* path, uint64_t& bytes) {
#if defined(_WIN32)
    struct _stat64 st{};
    if (_wstat64(widen(path).c_str(), &st) != 0 || (st.st_mode & _S_IFMT) != _S_IFREG || st.st_size < 0) return false;
    bytes = (uint64_t) st.st_size;
    return true;
#else
    struct stat st{};
    if (::stat(path, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0) return false;
    bytes = (uint64_t) st.st_size;
    return true;
#endif
}

/// The size of the open file `fd`, false when it cannot be read.
inline bool fd_size(int fd, uint64_t& bytes) {
#if defined(_WIN32)
    LARGE_INTEGER n{};
    const HANDLE h = (HANDLE) _get_osfhandle(fd);
    if (h == INVALID_HANDLE_VALUE || !GetFileSizeEx(h, &n) || n.QuadPart < 0) return false;
    bytes = (uint64_t) n.QuadPart;
    return true;
#else
    struct stat st{};
    if (fstat(fd, &st) != 0 || st.st_size < 0) return false;
    bytes = (uint64_t) st.st_size;
    return true;
#endif
}

/// One positional read (POSIX pread): the bytes read, 0 at end of file, -1 on an error.  It does not move a shared
/// file position, so threads may read the same descriptor at once.
inline long long pread_at(int fd, void* dst, size_t bytes, uint64_t offset) {
#if defined(_WIN32)
    HANDLE h = (HANDLE) _get_osfhandle(fd);
    if (h == INVALID_HANDLE_VALUE) return -1;
    // this thread's event: the handle itself is signalled by every read on it, so a wait on it would race
    struct Event {
        HANDLE e = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        ~Event() { if (e != nullptr) CloseHandle(e); }
    };
    thread_local Event ev;
    if (ev.e == nullptr) return -1;
    // ReadFile takes a 32-bit length: the caller's loop asks again for the rest
    const DWORD want = (DWORD) (bytes > (1u << 30) ? (1u << 30) : bytes);
    OVERLAPPED ov{};
    ov.Offset = (DWORD) (offset & 0xFFFFFFFFull);
    ov.OffsetHigh = (DWORD) (offset >> 32);
    ov.hEvent = ev.e;
    // works on an overlapped handle (open_read) and on a synchronous one alike: either way the read is at `offset`
    if (!ReadFile(h, dst, want, nullptr, &ov)) {
        const DWORD e = GetLastError();
        if (e == ERROR_HANDLE_EOF) return 0;
        if (e != ERROR_IO_PENDING) return -1;
    }
    DWORD got = 0;
    if (!GetOverlappedResult(h, &ov, &got, TRUE)) return GetLastError() == ERROR_HANDLE_EOF ? 0 : -1;
    return (long long) got;
#else
    ssize_t n;
    do {
        n = ::pread(fd, dst, bytes, (off_t) offset);
    } while (n < 0 && errno == EINTR);
    return (long long) n;
#endif
}

#if defined(_WIN32)
// ------------------------------------------------------------------------------------------------ file mappings
/// Map `bytes` of the open file `fd` from `offset`, read-only (mmap PROT_READ): the address of byte `offset`, or
/// nullptr.  The view may start below `offset` (views begin on the 64 KiB allocation granularity); the mapping object
/// is closed at once, the view keeps it alive.  Release with unmap_file.
inline void* map_file_readonly(int fd, uint64_t offset, uint64_t bytes) {
    HANDLE h = (HANDLE) _get_osfhandle(fd);
    if (h == INVALID_HANDLE_VALUE || bytes == 0) return nullptr;
    HANDLE m = CreateFileMappingW(h, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (m == nullptr) return nullptr;
    SYSTEM_INFO si{};
    GetSystemInfo(&si);
    const uint64_t g = si.dwAllocationGranularity ? si.dwAllocationGranularity : 65536;
    const uint64_t a = offset / g * g;
    void* v = MapViewOfFile(m, FILE_MAP_READ, (DWORD) (a >> 32), (DWORD) (a & 0xFFFFFFFFull), (SIZE_T) (bytes + (offset - a)));
    CloseHandle(m);
    return v != nullptr ? (void*) ((uint8_t*) v + (offset - a)) : nullptr;
}

/// Unmap a view from map_file_readonly, given any address inside it.
inline void unmap_file(const void* p) {
    MEMORY_BASIC_INFORMATION mbi{};
    if (p != nullptr && VirtualQuery(p, &mbi, sizeof mbi) != 0) UnmapViewOfFile(mbi.AllocationBase);
}
#endif

/// pread until `bytes` arrived: false on an error or a short file.
inline bool pread_full(int fd, void* dst, size_t bytes, uint64_t offset) {
    uint8_t* p = (uint8_t*) dst;
    while (bytes > 0) {
        const long long n = pread_at(fd, p, bytes, offset);
        if (n <= 0) return false;
        p += n;
        bytes -= (size_t) n;
        offset += (uint64_t) n;
    }
    return true;
}

}  // namespace basalt::platform
