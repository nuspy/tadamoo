// trim_probe.cpp - risolve e chiama le esportazioni trim/offer e misura il commit
// di sistema prima e dopo ogni chiamata. Le maschere di accesso determinano se il
// kernel accetta o rifiuta la richiesta.

#include <windows.h>
#include <d3dkmthk.h>
#include <psapi.h>
#include <cstdio>

#ifndef MAXIMUM_ALLOWED
#define MAXIMUM_ALLOWED 0x02000000
#endif

typedef NTSTATUS(WINAPI* PFN_TRIM)(D3DKMT_TRIMPROCESSCOMMITMENT*);
typedef NTSTATUS(WINAPI* PFN_OFFER)(D3DKMT_QUERYPROCESSOFFERINFO*);

static const wchar_t* kModNames[] = { L"win32u.dll", L"dxcore.dll", L"user32.dll", L"d3d11.dll" };

static const char* kTrimNames[] = { "NtGdiDdDDITrimProcessCommitment",
                                    "D3DKMTTrimProcessCommitment" };
static const char* kOfferNames[] = { "NtGdiDdDDIQueryProcessOfferInfo",
                                     "NtGdiDdDDTQueryProcessOfferInfo",
                                     "D3DKMTQueryProcessOfferInfo" };

static unsigned long long SystemCommit() {
    MEMORYSTATUSEX m{};
    m.dwLength = sizeof(m);
    GlobalMemoryStatusEx(&m);
    return m.ullTotalPageFile - m.ullAvailPageFile;
}

static unsigned long long SystemCommitLimit() {
    MEMORYSTATUSEX m{};
    m.dwLength = sizeof(m);
    GlobalMemoryStatusEx(&m);
    return m.ullTotalPageFile;
}

int wmain(int argc, wchar_t** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    DWORD pid = (argc > 1) ? (DWORD)_wtoi(argv[1]) : GetCurrentProcessId();

    printf("sizeof(D3DKMT_TRIMPROCESSCOMMITMENT) = %u\n",
           (unsigned)sizeof(D3DKMT_TRIMPROCESSCOMMITMENT));
    printf("sizeof(D3DKMT_QUERYPROCESSOFFERINFO) = %u\n",
           (unsigned)sizeof(D3DKMT_QUERYPROCESSOFFERINFO));

    // 1) Quali moduli espongono i due entry point?
    HMODULE trimMod = nullptr, offerMod = nullptr;
    LPCSTR trimName = nullptr, offerName = nullptr;
    for (auto* m : kModNames) {
        HMODULE h = LoadLibraryW(m);
        if (!h) continue;
        wchar_t pathBuf[MAX_PATH]; wchar_t* base = nullptr;
        GetModuleFileNameW(h, pathBuf, MAX_PATH);
        base = wcsrchr(pathBuf, L'\\') ? wcsrchr(pathBuf, L'\\') + 1 : pathBuf;
        for (auto* t : kTrimNames)
            if (!trimMod && GetProcAddress(h, t)) { trimMod = h; trimName = t; }
        for (auto* o : kOfferNames)
            if (!offerMod && GetProcAddress(h, o)) { offerMod = h; offerName = o; }
        printf("modulo %ls: trim=%s offer=%s\n", base,
               (trimMod == h) ? "presente" : "-",
               (offerMod == h) ? "presente" : "-");
    }

    if (!trimMod) { printf("Trim non risolto su questo build\n"); return 2; }
    auto trim = (PFN_TRIM)GetProcAddress(trimMod, trimName);

    printf("limite commit sistema = %.2f GB\n", SystemCommitLimit() / 1073741824.0);

    // 2) Chiamate con maschere diverse.
    const struct { LPCSTR name; DWORD mask; } tries[] = {
        { "QUERY_LIMITED", PROCESS_QUERY_LIMITED_INFORMATION },
        { "MAXIMUM_ALLOWED", MAXIMUM_ALLOWED },
        { "ALL(0x1FFFFF)", 0x001FFFFF },
    };
    for (auto& t : tries) {
        unsigned long long s0 = SystemCommit();
        HANDLE hp = OpenProcess(t.mask, FALSE, pid);
        if (!hp) { printf("%-16s OpenProcess fallita gle=%lu\n", t.name, GetLastError()); continue; }
        D3DKMT_TRIMPROCESSCOMMITMENT s{}; s.cbSize = sizeof(s); s.hProcess = hp;
        NTSTATUS st = trim(&s);
        unsigned long long s1 = SystemCommit();
        printf("%-16s status=0x%08lX decommitted=%.3f GB | commit sistema %.2f -> %.2f GB\n",
               t.name, (unsigned long)st, s.NumBytesDecommitted / 1073741824.0,
               s0 / 1073741824.0, s1 / 1073741824.0);
        CloseHandle(hp);
        Sleep(150);
    }

    // 3) L'offerta dice quanto il driver ritiene decomprimibile adesso.
    if (offerMod) {
        auto offer = (PFN_OFFER)GetProcAddress(offerMod, offerName);
        D3DKMT_QUERYPROCESSOFFERINFO q{}; q.cbSize = (ULONG)sizeof(q);
        for (auto& t : tries) {
            HANDLE hp = OpenProcess(t.mask, FALSE, pid);
            q.hProcess = hp;
            NTSTATUS st = offer(&q);
            printf("OFFERTA con %-16s status=0x%08lX DecommitUniqueness=%.2f GB "
                   "DecommittableBytes=%.2f GB\n",
                   t.name, (unsigned long)st, q.DecommitUniqueness / 1073741824.0,
                   q.DecommittableBytes / 1073741824.0);
            CloseHandle(hp);
        }
    } else {
        printf("QueryProcessOfferInfo non risolvibile via GetProcAddress\n");
    }
    return 0;
}
