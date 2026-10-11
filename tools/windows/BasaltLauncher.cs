// Starts `basalt serve` for Avvia-Basalt.ps1 (Windows PowerShell 5.1 compiles it with Add-Type, so C# 5 only).
//
// Tied (the default): the server shares the script's console, so Ctrl+C reaches it, and it runs in a job object
// with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE whose only handle belongs to the PowerShell process. However PowerShell
// ends - Ctrl+C, the window's X, Task Manager - Windows closes that handle and ends basalt.exe; basalt.exe's own
// job then ends the Python server, and serve/util/winjob.py's job ends the engine and the vision sidecar.
// The process starts suspended and enters the job before its first instruction, so nothing it starts escapes.
//
// Detached (-Sfondo): a hidden console of its own, no job of ours (breaking away from one PowerShell may be in),
// so the server outlives the script; Ferma-Basalt.ps1 stops it.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class BasaltLauncher
{
    [StructLayout(LayoutKind.Sequential)]
    struct SECURITY_ATTRIBUTES { public int nLength; public IntPtr lpSecurityDescriptor; public bool bInheritHandle; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO
    {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }

    [StructLayout(LayoutKind.Sequential)]
    struct BASIC_LIMITS
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit;
        public UIntPtr Affinity; public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS { public ulong R, W, O, RB, WB, OB; }

    [StructLayout(LayoutKind.Sequential)]
    struct EXTENDED_LIMITS
    {
        public BASIC_LIMITS Basic; public IO_COUNTERS Io;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref EXTENDED_LIMITS info, int size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcessW(string app, System.Text.StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit,
                                      uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, ref SECURITY_ATTRIBUTES sa, uint disposition,
                                     uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint WaitForSingleObject(IntPtr handle, uint ms);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr process, out uint code);

    const uint CREATE_SUSPENDED = 0x4, CREATE_NEW_CONSOLE = 0x10, CREATE_BREAKAWAY_FROM_JOB = 0x01000000;
    const int STARTF_USESHOWWINDOW = 0x1, STARTF_USESTDHANDLES = 0x100;
    const uint KILL_ON_JOB_CLOSE = 0x2000;
    const int JobObjectExtendedLimitInformation = 9;
    static readonly IntPtr INVALID = new IntPtr(-1);

    static IntPtr job = IntPtr.Zero;      // held until this process ends: closing it ends everything in the job
    static IntPtr process = IntPtr.Zero;

    /// <summary>The job is made once and never closed by this code; the OS closes it when PowerShell ends.</summary>
    static IntPtr Job()
    {
        if (job != IntPtr.Zero) return job;
        IntPtr j = CreateJobObjectW(IntPtr.Zero, null);   // no security attributes: the handle is not inherited
        if (j == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject");
        EXTENDED_LIMITS info = new EXTENDED_LIMITS();
        info.Basic.LimitFlags = KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(j, JobObjectExtendedLimitInformation, ref info, Marshal.SizeOf(typeof(EXTENDED_LIMITS))))
        {
            int e = Marshal.GetLastWin32Error();
            CloseHandle(j);
            throw new Win32Exception(e, "SetInformationJobObject");
        }
        job = j;
        return job;
    }

    static IntPtr Inheritable(string path, uint access, uint disposition)
    {
        SECURITY_ATTRIBUTES sa = new SECURITY_ATTRIBUTES();
        sa.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        sa.bInheritHandle = true;
        // FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE: the script reads the log while it is written
        IntPtr h = CreateFileW(path, access, 0x7, ref sa, disposition, 0x80, IntPtr.Zero);
        if (h == INVALID) throw new Win32Exception(Marshal.GetLastWin32Error(), path);
        return h;
    }

    /// <summary>Start `commandLine` in `cwd` with stdout and stderr appended to `logPath`. tied: same console, in
    /// the kill-on-close job; otherwise a hidden console of its own and no job. Returns the process id.</summary>
    public static int Start(string exe, string commandLine, string cwd, string logPath, bool tied)
    {
        IntPtr log = Inheritable(logPath, 0x0004 /* FILE_APPEND_DATA */, 4 /* OPEN_ALWAYS */);
        IntPtr nul = Inheritable("NUL", 0x80000000 /* GENERIC_READ */, 3 /* OPEN_EXISTING */);
        try
        {
            STARTUPINFO si = new STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
            si.dwFlags = STARTF_USESTDHANDLES;
            si.hStdInput = nul; si.hStdOutput = log; si.hStdError = log;
            uint flags = CREATE_SUSPENDED | 0x20;               // NORMAL_PRIORITY_CLASS, whatever the script runs at
            if (!tied)
            {
                flags |= CREATE_NEW_CONSOLE;
                si.dwFlags |= STARTF_USESHOWWINDOW;
                si.wShowWindow = 0;                         // SW_HIDE
            }
            PROCESS_INFORMATION pi;
            bool ok = CreateProcessW(exe, new System.Text.StringBuilder(commandLine), IntPtr.Zero, IntPtr.Zero, true,
                                     flags | (tied ? 0u : CREATE_BREAKAWAY_FROM_JOB), IntPtr.Zero, cwd, ref si, out pi);
            if (!ok && !tied && Marshal.GetLastWin32Error() == 5)   // the job PowerShell is in forbids breaking away
                ok = CreateProcessW(exe, new System.Text.StringBuilder(commandLine), IntPtr.Zero, IntPtr.Zero, true,
                                    flags, IntPtr.Zero, cwd, ref si, out pi);
            if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateProcess " + exe);
            if (tied && !AssignProcessToJobObject(Job(), pi.hProcess))
            {
                int e = Marshal.GetLastWin32Error();
                TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
                throw new Win32Exception(e, "AssignProcessToJobObject");
            }
            ResumeThread(pi.hThread);
            CloseHandle(pi.hThread);
            process = pi.hProcess;
            return pi.dwProcessId;
        }
        finally
        {
            CloseHandle(log);                                   // the child holds its own copies
            CloseHandle(nul);
        }
    }

    /// <summary>The started process's exit code, or -1 while it runs.</summary>
    public static long ExitCode()
    {
        if (process == IntPtr.Zero) return -2;
        if (WaitForSingleObject(process, 0) != 0) return -1;
        uint code;
        return GetExitCodeProcess(process, out code) ? (long) code : -3;
    }

    /// <summary>Wait up to `ms` for the started process to end; true when it has.</summary>
    public static bool Wait(int ms)
    {
        return process == IntPtr.Zero || WaitForSingleObject(process, (uint) ms) == 0;
    }

    /// <summary>End everything in the job now (what closing the window does).</summary>
    public static void KillAll()
    {
        if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
    }
}
