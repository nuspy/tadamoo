// BasaltWinGUI.exe ("Basalt Win+GUI"): the desktop window of the local Basalt server. One instance per user session:
// a second start brings the running window to the front.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace BasaltDesktop;

static class Program
{
    public const string AppName = "Basalt Win+GUI";       // the name the window, the tray and the dialogs show
    const string MutexName = @"Local\BasaltWinGUI.Desktop";
    public const string ShowEventName = @"Local\BasaltWinGUI.Desktop.Show";

    [STAThread]
    static int Main(string[] args)
    {
        var opt = Options.Parse(args);
        Tr.Init(opt.Lang);
        if (opt.Error != null)
        {
            MessageBox.Show(opt.Error, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
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

/// <summary>The command line (the same names as Avvia-Basalt.ps1, one or two dashes): --config "profile", --model
/// FILE.basalt, --port N, --vram-libera GB, --file CONFIG.json, --repo DIR; hidden test options --test-mock,
/// --test-screenshot FILE, --test-exit, --test-no-wizard, --test-fallback (the web server cannot start: the native
/// panel), --test-folder-prompt (with --test-fallback: the models folders unset, the prompt, then --test-models-dir
/// DIR is chosen as the user would), --test-no-webview.</summary>
sealed class Options
{
    public string Profile, Repo, Error, Screenshot, Model, TestModelsDir, ConfigFile, Lang;
    public int Port;
    public double VramLibera = -1;
    public bool TestMock, TestExit, TestFolderPrompt, TestNoWebView, TestNoWizard, TestFallback;

    public static Options Parse(string[] args)
    {
        var o = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i], v = null;
            int eq = a.IndexOf('=');
            if (a.StartsWith("-") && eq > 0) { v = a[(eq + 1)..]; a = a[..eq]; }
            string Next() => v ?? (i + 1 < args.Length ? args[++i] : null);
            switch ("--" + a.TrimStart('-').ToLowerInvariant())
            {
                case "--config": o.Profile = Next(); break;
                case "--port":
                    if (!int.TryParse(Next(), out o.Port) || o.Port < 1 || o.Port > 65535) o.Error = "--port: 1..65535";
                    break;
                case "--vram-libera":
                    if (!double.TryParse((Next() ?? "").Replace(',', '.'), System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out o.VramLibera) || o.VramLibera < 0 || o.VramLibera > 1024)
                        o.Error = "--vram-libera: GB (2, 1.5)";
                    break;
                case "--file": o.ConfigFile = Next(); break;
                case "--lang":
                    o.Lang = (Next() ?? "").ToLowerInvariant();
                    if (o.Lang != "it" && o.Lang != "en") o.Error = "--lang: it | en";
                    break;
                case "--repo": o.Repo = Next(); break;
                case "--model": o.Model = Next(); break;
                case "--test-folder-prompt": o.TestFolderPrompt = o.TestFallback = o.TestMock = true; break;
                case "--test-models-dir": o.TestModelsDir = Next(); break;
                case "--test-no-webview": o.TestNoWebView = true; break;
                case "--test-no-wizard": o.TestNoWizard = true; break;
                case "--test-fallback": o.TestFallback = o.TestMock = true; break;
                case "--test-mock": o.TestMock = true; break;
                case "--test-screenshot": o.Screenshot = Next(); break;
                case "--test-exit": o.TestExit = true; break;
                default: o.Error = $"? {args[i]}\nBasaltWinGUI.exe [--config \"<profile>\"] [--model \"<file .basalt>\"] [--port N] [--vram-libera GB] [--file <config>] [--lang it|en]"; break;
            }
        }
        o.Repo ??= FindRepo(AppContext.BaseDirectory);
        if (o.Repo == null && o.Error == null)
            o.Error = "Basalt Win+GUI: bin\\basalt.exe + Avvia-Basalt.ps1 ? " + AppContext.BaseDirectory + " (--repo <folder>)";
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

/// <summary>The language of the app's texts, Italian or English: --lang, else BASALT_LANG, else the user's choice in
/// prefs.json (the web page's Settings; serve/lang.py), else English. Children (the server, the scripts) get
/// BASALT_LANG when --lang is given.</summary>
static class Tr
{
    public static string Code = "en";

    public static void Init(string arg)
    {
        string Valid(string v) => v != null && (v.ToLowerInvariant() == "it" || v.ToLowerInvariant() == "en") ? v.ToLowerInvariant() : null;
        string chosen = Valid(arg) ?? Valid(Environment.GetEnvironmentVariable("BASALT_LANG"));
        if (chosen == null)
        {
            try
            {
                string data = Environment.GetEnvironmentVariable("BASALT_DATA");
                if (string.IsNullOrEmpty(data)) data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BasaltPannello");
                string prefs = Path.Combine(data, "prefs.json");
                if (File.Exists(prefs) && JsonDocument.Parse(File.ReadAllText(prefs)).RootElement.TryGetProperty("lang", out var l) &&
                    l.ValueKind == JsonValueKind.String) chosen = Valid(l.GetString());
            }
            catch { }
        }
        Code = chosen ?? "en";
        if (Valid(arg) != null) Environment.SetEnvironmentVariable("BASALT_LANG", Code);
    }

    public static string T(string it, string en) => Code == "it" ? it : en;
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

    /// <summary>Asks the server on this port to unload its model, so the engine ends cleanly and frees its memory. The
    /// control route needs the secret the server wrote to the user data folder when it started; without it (an older
    /// server, no file) the caller ends the processes anyway.</summary>
    public static bool UnloadServer(int port)
    {
        try
        {
            string data = Environment.GetEnvironmentVariable("BASALT_DATA");
            string dir = string.IsNullOrEmpty(data) ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BasaltPannello") : data;
            string secret = File.ReadAllText(Path.Combine(dir, $"control-{port}.token")).Trim();
            using var c = new HttpClient(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(90) };
            using var req = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/control/unload") { Content = new StringContent("{}", System.Text.Encoding.UTF8, "application/json") };
            req.Headers.Add("X-Basalt-Control", secret);
            using var r = c.SendAsync(req).GetAwaiter().GetResult();
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    /// <summary>The ports where a Basalt server answers /health (python or basalt processes only). A test server
    /// (/health "mock": true) is not Basalt running, unless BASALT_SCAN_MOCK=1 asks for it.</summary>
    public static List<(int port, JsonElement health)> RunningServers()
    {
        bool mocks = Environment.GetEnvironmentVariable("BASALT_SCAN_MOCK") == "1";
        var found = new List<(int, JsonElement)>();
        foreach (var (port, pid) in Listeners().DistinctBy(x => x.port))
        {
            string name;
            try { name = Process.GetProcessById(pid).ProcessName.ToLowerInvariant(); } catch { continue; }
            if (!name.StartsWith("python") && name != "basalt") continue;
            var h = GetJson($"http://127.0.0.1:{port}/health");
            if (h is JsonElement e && e.TryGetProperty("service", out var s) && s.GetString() == "basalt")
            {
                if (!mocks && e.TryGetProperty("mock", out var m) && m.ValueKind == JsonValueKind.True) continue;
                found.Add((port, e));
            }
        }
        return found;
    }
}
