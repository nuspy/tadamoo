// trim_test.cpp — probe D3DKMTTrimProcessCommitment on Windows 11 26100.
//
// On this build the export table lives in win32u.dll as
// NtGdiDdDDITrimProcessCommitment (dxcore.dll forwards it). Older layouts
// expose the name without the NtGdi prefix. We resolve dynamically so the
// harness works on either layout.
//
// Usage:  trim_test.exe [pid]      (default: this process)

#include <windows.h>
#include <cstdio>
#include <cstdint>

struct TRIM_FLAGS {
    struct {
        unsigned int Lazy : 1;
        unsigned int OnlyRepurposed : 1;
        unsigned int Reserved : 30;
    };
    unsigned int Value;
};

struct TRIM_STRUCT {
    unsigned int cbSize;
    HANDLE hProcess;
    TRIM_FLAGS Flags;
    unsigned long long DecommitRequested;
    unsigned long long NumBytesDecommitted;
};

typedef NTSTATUS(WINAPI* PFN_TRIM)(TRIM_STRUCT*);

static PFN_TRIM resolve(void) {
    const wchar_t* mods[] = { L"win32u.dll", L"dxgapi.dll", L"dxcore.dll", L"d3d11.dll" };
    const char* names[] = { "NtGdiDdDDITrimProcessCommitment",
                            "D3DKMTTrimProcessCommitment" };
    for (auto* mod : mods) {
        HMODULE h = LoadLibraryW(mod);
        if (!h) continue;
        for (auto* nm : names) {
            auto fn = (PFN_TRIM)GetProcAddress(h, nm);
            if (fn) {
                wprintf(L"risolta in %s (%S)\n", mod, nm);
                return fn;
            }
        }
    }
    return nullptr;
}

int wmain(int argc, wchar_t** argv) {
    DWORD pid = GetCurrentProcessId();
    if (argc > 1) pid = (DWORD)_wtoi(argv[1]);

    HANDLE proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!proc) {
        wprintf(L"OpenProcess fallito per %lu (errore %lu)\n", pid, GetLastError());
        return 1;
    }

    PFN_TRIM trim = resolve();
    if (!trim) {
        wprintf(L"D3DKMTTrimProcessCommitment non risolvibile\n");
        CloseHandle(proc);
        return 2;
    }

    TRIM_STRUCT info{};
    info.cbSize = sizeof(info);
    info.hProcess = proc;
    info.Flags.Value = 0;

    NTSTATUS st = trim(&info);
    if (st == 0) {
        wprintf(L"OK: scaricati %llu byte di commit\n", info.NumBytesDecommitted);
    } else {
        wprintf(L"trim fallito, status 0x%08lX\n", (unsigned long)st);
        CloseHandle(proc);
        return 3;
    }
    CloseHandle(proc);
    return 0;
}
