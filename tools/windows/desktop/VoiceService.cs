// The voice service: a Python process (python -m voice.service) that this app starts, watches and ends. It runs in a
// Windows job object of its own (kill on close, handle held by this app), not in the server's job: a server restart
// (exit code 75, a new model) leaves it alone, and it ends when the app ends, however the app ends.
using System.ComponentModel;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;

namespace TaDaMooDesktop;

enum VoiceState { Stopped, Starting, Running }

sealed class VoiceService
{
    const int MaxRestarts = 3;                                  // crashes answered by a restart ...
    static readonly TimeSpan RestartWindow = TimeSpan.FromMinutes(2);   // ... within this time; then the app stops trying
    static readonly TimeSpan RestartDelay = TimeSpan.FromSeconds(2);
    const int ForbiddenPort = 8080;

    readonly string root;                                       // where the `voice` package is: the working directory
    readonly Func<int> serverPort;                              // the web server's port, never used for this service
    readonly object gate = new();
    readonly List<DateTime> crashes = new();
    static readonly HttpClient http = new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };
    static bool hooked;

    Process proc;
    IntPtr job = IntPtr.Zero;
    bool stopping, ready, adopted;
    int port, pid;

    public VoiceService(string root, Func<int> serverPort)
    {
        this.root = root;
        this.serverPort = serverPort;
    }

    // ------------------------------------------------------------------ files
    /// <summary>%TADAMOO_DATA% or %APPDATA%\TaDaMoo, as everywhere else in the app.</summary>
    public static string DataDir()
    {
        string data = Environment.GetEnvironmentVariable("TADAMOO_DATA");
        return string.IsNullOrEmpty(data) ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "TaDaMoo") : data;
    }

    public static string SettingsFile => Path.Combine(DataDir(), "voice.json");
    public static string InfoFile => Path.Combine(DataDir(), "voice-service.json");
    public static string TokenFile => Path.Combine(DataDir(), "voice-service.token");
    string LogFile => Path.Combine(root, "logs", "voice-service.log");

    // ------------------------------------------------------------------ state
    public VoiceState State
    {
        get
        {
            lock (gate)
            {
                if (adopted) return Healthy(ReadInfoPort()) ? VoiceState.Running : VoiceState.Stopped;
                if (proc == null) return VoiceState.Stopped;
                return ready ? VoiceState.Running : VoiceState.Starting;
            }
        }
    }

    public int Port { get { lock (gate) return adopted ? ReadInfoPort() : port; } }

    /// <summary>The tray's line.</summary>
    public string StatusText => State switch
    {
        VoiceState.Running => Tr.T("Servizio vocale: attivo", "Voice service: running"),
        VoiceState.Starting => Tr.T("Servizio vocale: in avvio", "Voice service: starting"),
        _ => Tr.T("Servizio vocale: fermo", "Voice service: stopped"),
    };

    // ------------------------------------------------------------------ start, stop, restart
    /// <summary>Starts the service unless one already answers on the port of the info file or Python cannot be found
    /// (then one line goes to the log and nothing else happens). True when a service is running or starting.</summary>
    public bool Start()
    {
        lock (gate)
        {
            if (!hooked)
            {
                hooked = true;
                AppDomain.CurrentDomain.ProcessExit += (_, _) => Stop();
                Microsoft.Win32.SystemEvents.SessionEnding += (_, _) => Stop();
            }
            stopping = false;
            crashes.Clear();
            return Launch();
        }
    }

    /// <summary>Ends the service this app started (its whole job). A copy the app did not start is left alone.</summary>
    public void Stop()
    {
        Process p;
        IntPtr j;
        lock (gate)
        {
            stopping = true;
            if (adopted) { adopted = false; Log("a service not started by this app is left running"); return; }
            p = proc; j = job;
            proc = null; job = IntPtr.Zero; ready = false;
        }
        if (j != IntPtr.Zero) CloseHandle(j);                   // KILL_ON_JOB_CLOSE ends the service and its children
        if (p == null) return;
        try { if (!p.WaitForExit(5000)) p.Kill(true); } catch { }
        p.Dispose();
        Log("stopped");
    }

    public bool Restart()
    {
        Stop();
        return Start();
    }

    // must be called under the lock
    bool Launch()
    {
        if (proc != null) return true;
        try
        {
            if (Healthy(ReadInfoPort())) { adopted = true; Log($"a service already answers on port {ReadInfoPort()}: not started again"); return true; }
            adopted = false;
            var py = FindPython();
            if (py.exe == null)
            {
                Log("not started: no Python found (voice\\venv, TADAMOO_VOICE_PYTHON, py -3.10, python)");
                return false;
            }
            Directory.CreateDirectory(DataDir());
            EnsureToken();
            port = FreePort();
            try { File.Delete(InfoFile); } catch { }            // a stale file from an earlier run
            var psi = new ProcessStartInfo(py.exe)
            {
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = false,
                StandardOutputEncoding = System.Text.Encoding.UTF8, StandardErrorEncoding = System.Text.Encoding.UTF8,
            };
            foreach (var a in py.prefix) psi.ArgumentList.Add(a);
            foreach (var a in new[] { "-m", "voice.service", "--settings", SettingsFile, "--host", "127.0.0.1", "--port", port.ToString(),
                                      "--token-file", TokenFile, "--info-file", InfoFile })
                psi.ArgumentList.Add(a);
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            psi.Environment["PYTHONUNBUFFERED"] = "1";
            psi.Environment["TADAMOO_DATA"] = DataDir();
            psi.Environment.Remove("VIRTUAL_ENV");
            RotateLog();
            var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
            p.OutputDataReceived += (_, e) => OnLine(p, e.Data, false);
            p.ErrorDataReceived += (_, e) => OnLine(p, e.Data, true);
            p.Exited += (_, _) => OnExited(p);
            IntPtr j = MakeJob();
            try
            {
                p.Start();
                if (!AssignProcessToJobObject(j, p.Handle))
                {
                    int err = Marshal.GetLastWin32Error();
                    try { p.Kill(true); } catch { }
                    throw new Win32Exception(err, "AssignProcessToJobObject");
                }
            }
            catch { CloseHandle(j); throw; }
            proc = p; job = j; ready = false; pid = p.Id;
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            Log($"started pid {pid} on port {port} ({py.exe})");
            return true;
        }
        catch (Exception e)
        {
            Log("not started: " + e.Message);
            return false;
        }
    }

    void OnLine(Process p, string line, bool err)
    {
        if (line == null) return;
        Log((err ? "err: " : "out: ") + line);
        if (err || !line.StartsWith("{")) return;
        try
        {
            var r = JsonDocument.Parse(line).RootElement;
            if (r.TryGetProperty("ready", out var ok) && ok.ValueKind == JsonValueKind.True)
                lock (gate)
                {
                    if (p != proc) return;
                    ready = true;
                    if (r.TryGetProperty("port", out var pp) && pp.ValueKind == JsonValueKind.Number) port = pp.GetInt32();
                    if (r.TryGetProperty("pid", out var pi) && pi.ValueKind == JsonValueKind.Number) pid = pi.GetInt32();
                }
        }
        catch (JsonException) { }
    }

    void OnExited(Process p)
    {
        int code = -1;
        try { code = p.ExitCode; } catch { }
        lock (gate)
        {
            if (p != proc) return;                              // stopped on purpose
            proc = null; ready = false;
            if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }   // nothing the service left behind survives it
            if (stopping) return;
            var now = DateTime.UtcNow;
            crashes.RemoveAll(t => now - t > RestartWindow);
            if (crashes.Count >= MaxRestarts)
            {
                Log($"ended with code {code}; {MaxRestarts} restarts in {RestartWindow.TotalMinutes:0} minutes: not started again");
                return;
            }
            crashes.Add(now);
            Log($"ended with code {code}; restart {crashes.Count} of {MaxRestarts}");
        }
        Task.Run(async () =>
        {
            await Task.Delay(RestartDelay);
            lock (gate) { if (!stopping && proc == null && !adopted) Launch(); }
        });
    }

    // ------------------------------------------------------------------ helpers
    static int ReadInfoPort()
    {
        try
        {
            if (File.Exists(InfoFile) && JsonDocument.Parse(File.ReadAllText(InfoFile)).RootElement.TryGetProperty("port", out var p) &&
                p.ValueKind == JsonValueKind.Number) return p.GetInt32();
        }
        catch { }
        return 0;
    }

    /// <summary>/health answers on this port (it needs no token).</summary>
    static bool Healthy(int port)
    {
        if (port < 1 || port > 65535) return false;
        try { return http.GetAsync($"http://127.0.0.1:{port}/health").GetAwaiter().GetResult().IsSuccessStatusCode; }
        catch { return false; }
    }

    /// <summary>A free local port, never 8080 and never the server's.</summary>
    int FreePort()
    {
        int avoid = serverPort();
        for (int i = 0; i < 50; i++)
        {
            var l = new TcpListener(IPAddress.Loopback, 0);
            l.Start();
            int p = ((IPEndPoint)l.LocalEndpoint).Port;
            l.Stop();
            if (p != ForbiddenPort && p != avoid) return p;
        }
        throw new InvalidOperationException("no free local port");
    }

    /// <summary>32 random bytes in hex, readable by the current user only. An existing token is kept.</summary>
    static void EnsureToken()
    {
        string path = TokenFile;
        if (File.Exists(path) && new FileInfo(path).Length > 0) return;
        try { File.Delete(path); } catch { }
        var sec = new FileSecurity();
        sec.SetAccessRuleProtection(true, false);               // no inherited permissions
        sec.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User, FileSystemRights.FullControl, AccessControlType.Allow));
        using var fs = FileSystemAclExtensions.Create(new FileInfo(path), FileMode.CreateNew, FileSystemRights.FullControl, FileShare.None, 4096, FileOptions.None, sec);
        var bytes = System.Text.Encoding.ASCII.GetBytes(Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant());
        fs.Write(bytes, 0, bytes.Length);
    }

    /// <summary>The service's Python: voice\venv next to the application, else TADAMOO_VOICE_PYTHON, else `py -3.10`, else
    /// `python` on PATH (3.10 or newer). exe is null when none is found.</summary>
    (string exe, string[] prefix) FindPython()
    {
        foreach (var dir in new[] { root, AppContext.BaseDirectory }.Distinct())
        {
            string venv = Path.Combine(dir, "voice", "venv", "Scripts", "python.exe");
            if (File.Exists(venv)) return (venv, Array.Empty<string>());
        }
        string env = Environment.GetEnvironmentVariable("TADAMOO_VOICE_PYTHON");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(env)) return (env, Array.Empty<string>());
        string py = OnPath("py.exe");
        if (py != null && Works(py, "-3.10")) return (py, new[] { "-3.10" });
        string python = OnPath("python.exe", skipStore: true);
        if (python != null && Works(python)) return (python, Array.Empty<string>());
        return (null, null);
    }

    static string OnPath(string name, bool skipStore = false)
    {
        foreach (var d in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                string f = Path.Combine(d.Trim().Trim('"'), name);
                if (File.Exists(f) && !(skipStore && f.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase))) return f;
            }
            catch (ArgumentException) { }
        }
        return null;
    }

    /// <summary>The interpreter runs and is Python 3.10 or newer.</summary>
    static bool Works(string exe, params string[] prefix)
    {
        try
        {
            var psi = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            foreach (var a in prefix) psi.ArgumentList.Add(a);
            psi.ArgumentList.Add("-c");
            psi.ArgumentList.Add("import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)");
            using var p = Process.Start(psi);
            if (!p.WaitForExit(8000)) { try { p.Kill(true); } catch { } return false; }
            return p.ExitCode == 0;
        }
        catch { return false; }
    }

    // ------------------------------------------------------------------ log (logs\voice-service.log next to the server's)
    readonly object logGate = new();

    void RotateLog()
    {
        try
        {
            var f = new FileInfo(LogFile);
            if (f.Exists && f.Length > 1_000_000) File.Move(LogFile, LogFile + ".1", true);
        }
        catch { }
    }

    void Log(string text)
    {
        try
        {
            lock (logGate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogFile));
                File.AppendAllText(LogFile, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {text}{Environment.NewLine}");
            }
        }
        catch { }
    }

    // ------------------------------------------------------------------ the job object (this app's, one per start)
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
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);

    static IntPtr MakeJob()
    {
        IntPtr j = CreateJobObjectW(IntPtr.Zero, null);
        if (j == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject");
        var info = new EXTENDED_LIMITS();
        info.Basic.LimitFlags = 0x2000;                         // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (!SetInformationJobObject(j, 9 /* JobObjectExtendedLimitInformation */, ref info, Marshal.SizeOf<EXTENDED_LIMITS>()))
        {
            int e = Marshal.GetLastWin32Error();
            CloseHandle(j);
            throw new Win32Exception(e, "SetInformationJobObject");
        }
        return j;
    }
}
