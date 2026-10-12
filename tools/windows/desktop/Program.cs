// Basalt.exe: the desktop window of the local Basalt server. One instance per user session: a second start brings the
// running window to the front.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace BasaltDesktop;

static class Program
{
    const string MutexName = @"Local\Basalt.Desktop";
    public const string ShowEventName = @"Local\Basalt.Desktop.Show";

    [STAThread]
    static int Main(string[] args)
    {
        var opt = Options.Parse(args);
        if (opt.Error != null)
        {
            MessageBox.Show(opt.Error, "Basalt", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
        using var mutex = new Mutex(true, opt.TestMock ? MutexName + ".test" : MutexName, out bool first);
        if (!first)
        {
            try { EventWaitHandle.OpenExisting(ShowEventName + (opt.TestMock ? ".test" : "")).Set(); } catch (WaitHandleCannotBeOpenedException) { }
            return 0;
        }
        ApplicationConfiguration.Initialize();
        using var show = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName + (opt.TestMock ? ".test" : ""));
        var form = new MainForm(opt, show);
        Application.Run(form);
        return form.ExitCode;
    }
}

/// <summary>The command line: --config "profile", --port N, --repo DIR; hidden test options --test-mock,
/// --test-screenshot FILE, --test-exit.</summary>
sealed class Options
{
    public string Profile, Repo, Error, Screenshot;
    public int Port;
    public bool TestMock, TestExit;

    public static Options Parse(string[] args)
    {
        var o = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i], v = null;
            int eq = a.IndexOf('=');
            if (a.StartsWith("--") && eq > 0) { v = a[(eq + 1)..]; a = a[..eq]; }
            string Next() => v ?? (i + 1 < args.Length ? args[++i] : null);
            switch (a.ToLowerInvariant())
            {
                case "--config": case "-config": o.Profile = Next(); break;
                case "--port": case "-port":
                    if (!int.TryParse(Next(), out o.Port) || o.Port < 1 || o.Port > 65535) o.Error = "--port vuole un numero da 1 a 65535.";
                    break;
                case "--repo": o.Repo = Next(); break;
                case "--test-mock": o.TestMock = true; break;
                case "--test-screenshot": o.Screenshot = Next(); break;
                case "--test-exit": o.TestExit = true; break;
                default: o.Error = $"Argomento sconosciuto: {args[i]}\nUso: Basalt.exe [--config \"<profilo>\"] [--port N]"; break;
            }
        }
        o.Repo ??= FindRepo(AppContext.BaseDirectory);
        if (o.Repo == null && o.Error == null)
            o.Error = "Non trovo la cartella di Basalt (bin\\basalt.exe e Avvia-Basalt.ps1) sopra " + AppContext.BaseDirectory +
                      ". Avvia Basalt.exe con --repo <cartella>.";
        return o;
    }

    static string FindRepo(string from)
    {
        for (var d = new DirectoryInfo(from); d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "Avvia-Basalt.ps1")) && Directory.Exists(Path.Combine(d.FullName, "serve")))
                return d.FullName;
        return null;
    }
}

/// <summary>This PC's memory, the TCP listeners and their processes, and small HTTP calls to the server.</summary>
static class Native
{
    [StructLayout(LayoutKind.Sequential)]
    struct MEMORYSTATUSEX
    {
        public uint dwLength, dwMemoryLoad;
        public ulong ullTotalPhys, ullAvailPhys, ullTotalPageFile, ullAvailPageFile, ullTotalVirtual, ullAvailVirtual, ullAvailExtendedVirtual;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX m);

    public static double AvailableRamGiB()
    {
        var m = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
        return GlobalMemoryStatusEx(ref m) ? m.ullAvailPhys / 1073741824.0 : 0;
    }

    [DllImport("iphlpapi.dll", SetLastError = true)]
    static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool order, int af, int tableClass, uint reserved);

    /// <summary>(port, pid) of every IPv4 TCP listener.</summary>
    public static List<(int port, int pid)> Listeners()
    {
        var list = new List<(int, int)>();
        int size = 0;
        GetExtendedTcpTable(IntPtr.Zero, ref size, false, 2, 3 /* TCP_TABLE_OWNER_PID_LISTENER */, 0);
        IntPtr buf = Marshal.AllocHGlobal(size);
        try
        {
            if (GetExtendedTcpTable(buf, ref size, false, 2, 3, 0) != 0) return list;
            int n = Marshal.ReadInt32(buf);
            for (int i = 0; i < n; i++)
            {
                IntPtr row = buf + 4 + i * 24;                 // state, local addr, local port, remote addr, remote port, pid
                int port = (Marshal.ReadByte(row, 8) << 8) | Marshal.ReadByte(row, 9);
                int pid = Marshal.ReadInt32(row, 20);
                list.Add((port, pid));
            }
        }
        finally { Marshal.FreeHGlobal(buf); }
        return list;
    }

    static readonly HttpClient http = new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(3) };

    public static JsonElement? GetJson(string url)
    {
        try
        {
            string text = http.GetStringAsync(url).GetAwaiter().GetResult();
            return JsonDocument.Parse(text).RootElement.Clone();
        }
        catch { return null; }
    }

    public static bool PostJson(string url, string body, int timeoutS = 90)
    {
        try
        {
            using var c = new HttpClient(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(timeoutS) };
            using var r = c.PostAsync(url, new StringContent(body, System.Text.Encoding.UTF8, "application/json")).GetAwaiter().GetResult();
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    /// <summary>The ports where a Basalt server answers /health (python or basalt processes only).</summary>
    public static List<(int port, JsonElement health)> RunningServers()
    {
        var found = new List<(int, JsonElement)>();
        foreach (var (port, pid) in Listeners().DistinctBy(x => x.port))
        {
            string name;
            try { name = Process.GetProcessById(pid).ProcessName.ToLowerInvariant(); } catch { continue; }
            if (!name.StartsWith("python") && name != "basalt") continue;
            var h = GetJson($"http://127.0.0.1:{port}/health");
            if (h is JsonElement e && e.TryGetProperty("service", out var s) && s.GetString() == "basalt") found.Add((port, e));
        }
        return found;
    }
}
