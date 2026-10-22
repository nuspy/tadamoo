#include <windows.h>
#include <d3dkmthk.h>
#include <psapi.h>
#include <cstdio>
#ifndef MAXIMUM_ALLOWED
#define MAXIMUM_ALLOWED 0x02000000
#endif
typedef NTSTATUS(WINAPI* PFN_TRIM)(D3DKMT_TRIMPROCESSCOMMITMENT*);
typedef NTSTATUS(WINAPI* PFN_OFFER)(D3DKMT_QUERYPROCESSOFFERINFO*);
static unsigned long long PagefileOf(DWORD pid) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return 0;
    PROCESS_MEMORY_COUNTERS pmc{}; pmc.cb = sizeof(pmc);
    unsigned long long v = 0;
    if (GetProcessMemoryInfo(h, &pmc, sizeof(pmc))) v = (unsigned long long)pmc.PagefileUsage;
    CloseHandle(h);
    return v;
}
int wmain(int argc, wchar_t** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    DWORD pid = GetCurrentProcessId();
    if (argc > 1) pid = (DWORD)_wtoi(argv[1]);
    printf("sizeof(TRIM)=%u sizeof(OFFERINFO)=%u\n",
           (unsigned)sizeof(D3DKMT_TRIMPROCESSCOMMITMENT),
           (unsigned)sizeof(D3DKMT_QUERYPROCESSOFFERINFO));
    const wchar_t* mods[] = { L"dxcore.dll", L"win32u.dll", L"user32.dll" };
    for (auto* n : mods) {
        HMODULE h = LoadLibraryW(n);
        if (!h) continue;
        FARPROC ftr = GetProcAddress(h, "NtGdiDdDDTTrimProcessCommitment");
        if (!ftr) ftr = GetProcAddress(h, "D3DKMTTrimProcessCommitment");
        if (!ftr) { wprintf(L"%ls : nessun trim\n", n); continue; }
        HANDLE hp = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        D3DKMT_TRIMPROCESSCOMMITMENT s{}; s.cbSize = sizeof(s); s.hProcess = hp;
        NTSTATUS st = ((PFN_TRIM)ftr)(&s);
        wprintf(L"%ls TRIM status=0x%08lX NumBytesDecommitted=%.2f GB\n",
                n, (unsigned long)st, s.NumBytesDecommitted / 1073741824.0);
        CloseHandle(hp);
    }
    printf("commit basalt = %.2f GB\n", PagefileOf(pid) / 1073741824.0);
    return 0;
}
