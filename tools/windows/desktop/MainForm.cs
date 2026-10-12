// The window: a start panel (load profile, port, the memory check, Avvia / Avvia solo server) while no server runs,
// then the server's own web UI in WebView2. Closing the window only hides it: Basalt keeps running behind the
// notification-area icon, whose menu opens it again, reloads with other settings or turns everything off.
using System.Diagnostics;
using System.Drawing.Imaging;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace BasaltDesktop;

sealed class MainForm : Form
{
    static readonly Color Bg = Color.FromArgb(14, 17, 20), Panel = Color.FromArgb(21, 26, 31), Fg = Color.FromArgb(233, 236, 238),
        Fg2 = Color.FromArgb(154, 164, 173), Accent = Color.FromArgb(255, 122, 61), Line = Color.FromArgb(40, 49, 58), Bad = Color.FromArgb(255, 143, 163);

    readonly Options opt;
    readonly EventWaitHandle showEvent;
    readonly NotifyIcon tray;
    readonly WebView2 web = new() { Dock = DockStyle.Fill, Visible = false, DefaultBackgroundColor = Color.FromArgb(14, 17, 20) };
    readonly TableLayoutPanel start = new() { Dock = DockStyle.Fill, Padding = new Padding(32, 24, 32, 24), BackColor = Bg };
    readonly ComboBox profiles = new() { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, Dock = DockStyle.Fill, Font = new Font("Segoe UI", 12f) };
    readonly NumericUpDown port = new() { Minimum = 1, Maximum = 65535, Value = 7232, Width = 140, Font = new Font("Segoe UI", 12f) };
    readonly Label check = new() { AutoSize = false, Dock = DockStyle.Fill, ForeColor = Fg2, Font = new Font("Consolas", 10.5f) };
    readonly Button go = Btn("Avvia", true), headless = Btn("Avvia solo server", false), quit = Btn("Esci", false);
    readonly System.Windows.Forms.Timer poll = new() { Interval = 500 };
    List<JsonElement> profileList = new();
    string cfgPath;
    int serverPort;                 // the port of the server this window shows
    bool ours;                      // started by this app: in its job object
    bool exiting, checkOk;
    int checkSeq;
    public int ExitCode { get; private set; }
    readonly string statePath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Basalt", "desktop.json");
    readonly List<string> report = new();

    public MainForm(Options opt, EventWaitHandle showEvent)
    {
        this.opt = opt;
        this.showEvent = showEvent;
        Text = "Basalt";
        Icon = LoadIcon();
        BackColor = Bg;
        ForeColor = Fg;
        MinimumSize = new Size(420, 520);
        Size = new Size(1360, 900);
        StartPosition = FormStartPosition.CenterScreen;
        cfgPath = Path.Combine(opt.Repo, "basalt_config.json");

        var menu = new ContextMenuStrip();
        menu.Items.Add("Apri", null, (_, _) => ShowWindow());
        menu.Items.Add("Ricarica con altri parametri…", null, (_, _) => { ShowWindow(); if (web.Visible) web.CoreWebView2?.Navigate($"http://127.0.0.1:{serverPort}/#loader"); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Spegni", null, (_, _) => TurnOff(true));
        tray = new NotifyIcon { Icon = Icon, Text = "Basalt", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ShowWindow();

        BuildStartPanel();
        Controls.Add(web);
        Controls.Add(start);
        poll.Tick += (_, _) => WaitForServer();
        new Thread(() => { while (showEvent.WaitOne()) BeginInvoke(ShowWindow); }) { IsBackground = true }.Start();
        Shown += async (_, _) => await Begin();
    }

    static Icon LoadIcon()
    {
        using var s = typeof(MainForm).Assembly.GetManifestResourceStream("BasaltDesktop.basalt.ico");
        return s != null ? new Icon(s) : SystemIcons.Application;
    }

    static Button Btn(string text, bool primary) => new()
    {
        Text = text, AutoSize = true, MinimumSize = new Size(140, 48), FlatStyle = FlatStyle.Flat, Font = new Font("Segoe UI", 11.5f, primary ? FontStyle.Bold : FontStyle.Regular),
        BackColor = primary ? Accent : Color.FromArgb(42, 51, 60), ForeColor = primary ? Color.FromArgb(14, 17, 20) : Color.FromArgb(233, 236, 238),
        Margin = new Padding(0, 0, 12, 0), Cursor = Cursors.Hand,
    };

    void BuildStartPanel()
    {
        start.ColumnCount = 1;
        Label L(string t, float size, Color c, FontStyle st = FontStyle.Regular) => new() { Text = t, AutoSize = true, ForeColor = c, Font = new Font("Segoe UI", size, st), Margin = new Padding(0, 0, 0, 8) };
        start.Controls.Add(L("Basalt", 22f, Fg, FontStyle.Bold));
        start.Controls.Add(L("Scegli il profilo di caricamento: prima di avviare controllo che VRAM, RAM e commit bastino lasciando liberi il 15% della RAM e del commit.", 11f, Fg2));
        start.Controls.Add(L("Profilo di caricamento", 11.5f, Fg, FontStyle.Bold));
        start.Controls.Add(profiles);
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 8) };
        row.Controls.Add(L("Porta", 11.5f, Fg, FontStyle.Bold));
        row.Controls.Add(port);
        start.Controls.Add(row);
        start.Controls.Add(check);
        var buttons = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 0) };
        buttons.Controls.AddRange(new Control[] { go, headless, quit });
        start.Controls.Add(buttons);
        start.Controls.Add(L("«Avvia solo server» avvia il server senza finestra né icona (per gli agenti); si ferma con Ferma-Basalt.ps1 --port N.", 10f, Fg2));
        for (int i = 0; i < start.Controls.Count; i++) start.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        void Wrap()
        {
            int w = Math.Max(200, start.ClientSize.Width - start.Padding.Horizontal - 8);
            foreach (Control c in start.Controls) if (c is Label { AutoSize: true } l) l.MaximumSize = new Size(w, 0);
        }
        start.Resize += (_, _) => Wrap();
        Wrap();
        start.RowStyles[5] = new RowStyle(SizeType.Percent, 100);
        profiles.BackColor = Panel; profiles.ForeColor = Fg;
        port.BackColor = Panel; port.ForeColor = Fg;
        profiles.SelectedIndexChanged += (_, _) => { var p = SelectedProfile(); if (p != null && p.Value.TryGetProperty("values", out var v) && v.TryGetProperty("server.port", out var pp) && opt.Port == 0) port.Value = pp.GetInt32(); _ = Check(); };
        go.Click += async (_, _) => await StartServer();
        headless.Click += async (_, _) => await StartHeadless();
        quit.Click += (_, _) => { exiting = true; Close(); };
    }

    JsonElement? SelectedProfile() => profiles.SelectedIndex >= 0 && profiles.SelectedIndex < profileList.Count ? profileList[profiles.SelectedIndex] : null;
    string SelectedName => SelectedProfile()?.GetProperty("name").GetString();

    // ------------------------------------------------------------------ the python helpers of the repository
    async Task<(int code, string output)> Py(params string[] args)
    {
        var psi = new ProcessStartInfo(Path.Combine(opt.Repo, ".venv", "Scripts", "python.exe"))
        {
            WorkingDirectory = opt.Repo, RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8, StandardErrorEncoding = System.Text.Encoding.UTF8,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        using var p = Process.Start(psi);
        var outTask = p.StandardOutput.ReadToEndAsync();
        var errTask = p.StandardError.ReadToEndAsync();
        await p.WaitForExitAsync();
        return (p.ExitCode, (await outTask) + (await errTask));
    }

    // ------------------------------------------------------------------ start: attach, or the start panel
    async Task Begin()
    {
        if (opt.TestMock) PrepareMock();
        var running = await Task.Run(Native.RunningServers);
        if (running.Count > 0)
        {
            var (p, h) = running[0];
            string model = h.TryGetProperty("model", out var m) ? m.GetString() : "?";
            string want = opt.Profile ?? "il profilo predefinito";
            var attach = new TaskDialogButton("Collegati al server in esecuzione");
            var restart = new TaskDialogButton($"Chiudi il server attuale e avvia con «{want}»");
            var page = new TaskDialogPage
            {
                Caption = "Basalt", Heading = $"Basalt è già acceso sulla porta {p}",
                Text = $"Modello: {model}. Puoi collegarti a questo server oppure chiuderlo e avviarne uno nuovo.",
                Buttons = { attach, restart, TaskDialogButton.Cancel }, Icon = TaskDialogIcon.Information,
            };
            var choice = opt.TestMock ? attach : TaskDialog.ShowDialog(this, page);
            if (choice == attach) { ShowServer(p, false); return; }
            if (choice != restart) { exiting = true; Close(); return; }
            await StopExternal(p);
        }
        await LoadProfiles();
        if (opt.Profile != null && !profileList.Any(x => x.GetProperty("name").GetString() == opt.Profile))
        {
            MessageBox.Show(this, $"Profilo sconosciuto: «{opt.Profile}».\nProfili: " + string.Join(", ", profileList.Select(x => x.GetProperty("name").GetString())),
                "Basalt", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        if (opt.TestMock)
        {
            await Check();
            if (opt.Screenshot != null) SnapForm(Path.ChangeExtension(opt.Screenshot, null) + "-panel.png");
            await StartServer();
        }
    }

    async Task LoadProfiles()
    {
        var (code, output) = await Py("-m", "serve.profiles", "json", "--config", cfgPath);
        profileList.Clear();
        profiles.Items.Clear();
        if (code != 0) { check.Text = "Non riesco a leggere i profili:\n" + output; return; }
        var root = JsonDocument.Parse(output.Trim().Split('\n').Last()).RootElement.Clone();
        string def = root.GetProperty("default").GetString();
        foreach (var p in root.GetProperty("load").EnumerateArray())
        {
            profileList.Add(p);
            var s = p.GetProperty("summary");
            string extra = (s.TryGetProperty("vram_gb", out var v) ? $" · VRAM ~{v.GetDouble():0.0} GB" : "") + (s.TryGetProperty("tok_s", out var t) ? $" · ~{t.GetInt32()} tok/s" : "");
            string name = p.GetProperty("name").GetString();
            profiles.Items.Add($"{name}{(p.GetProperty("default").GetBoolean() ? " (predefinito)" : "")} — {s.GetProperty("text").GetString()}{extra}");
        }
        string want = opt.Profile ?? def;
        int idx = profileList.FindIndex(x => x.GetProperty("name").GetString() == want);
        profiles.SelectedIndex = Math.Max(0, idx);
        if (opt.Port != 0) port.Value = opt.Port;
    }

    async Task Check()
    {
        int seq = ++checkSeq;
        string name = SelectedName;
        if (name == null) return;
        go.Enabled = headless.Enabled = false;
        check.ForeColor = Fg2;
        check.Text = "Controllo la memoria…";
        string tmp = Path.Combine(Path.GetTempPath(), $"basalt_config.check.{Environment.ProcessId}.json");
        string output; int code;
        try
        {
            File.Copy(cfgPath, tmp, true);
            if (!opt.TestMock)
            {
                var ap = await Py("-m", "serve.profiles", "apply", name, "--config", tmp);
                if (ap.code != 0) { check.Text = ap.output; check.ForeColor = Bad; return; }
            }
            (code, output) = await Py("-m", "serve.loader", "check", "--config", tmp);
        }
        finally { try { File.Delete(tmp); File.Delete(tmp + ".bak"); } catch { } }
        if (seq != checkSeq) return;
        var lines = output.TrimEnd().Split('\n').Select(l => l.TrimEnd('\r')).ToList();
        if (lines.Count > 0 && lines[^1].StartsWith("{")) lines.RemoveAt(lines.Count - 1);
        checkOk = code == 0;
        check.Text = string.Join("\n", lines) + (checkOk ? "\n\nOK: si può avviare." : "\n\nNon si avvia: la memoria non basta senza usare il file di paging. Scegli un altro profilo o chiudi altri programmi.");
        check.ForeColor = checkOk ? Fg : Bad;
        go.Enabled = headless.Enabled = checkOk;
    }

    // ------------------------------------------------------------------ start the server, in this app's job
    async Task StartServer()
    {
        if (!opt.TestMock && !checkOk) { await Check(); if (!checkOk) return; }
        string name = SelectedName;
        int p = (int)port.Value;
        go.Enabled = headless.Enabled = false;
        string exe, cmd;
        if (opt.TestMock)
        {
            exe = Path.Combine(opt.Repo, ".venv", "Scripts", "python.exe");
            cmd = $"\"{exe}\" -m serve.server --mock-engine --config \"{cfgPath}\" --port {p}";
        }
        else
        {
            var ap = await Py("-m", "serve.profiles", "apply", name, "--config", cfgPath);
            if (ap.code != 0) { check.Text = ap.output; check.ForeColor = Bad; return; }
            exe = Path.Combine(opt.Repo, "bin", "basalt.exe");
            cmd = $"\"{exe}\" serve --config \"{cfgPath}\" --port {p}";
        }
        string logDir = Path.Combine(opt.Repo, "logs");
        Directory.CreateDirectory(logDir);
        string log = Path.Combine(logDir, "basalt-serve.log");
        try { if (File.Exists(log)) File.Move(log, log + ".1", true); } catch { }
        foreach (var v in new[] { "CL", "_CL_" }) Environment.SetEnvironmentVariable(v, null);
        Environment.SetEnvironmentVariable("PYTHONUTF8", "1");
        Environment.SetEnvironmentVariable("PYTHONIOENCODING", "utf-8");
        Environment.SetEnvironmentVariable("VIRTUAL_ENV", Path.Combine(opt.Repo, ".venv"));
        try
        {
            int pid = BasaltLauncher.Start(exe, cmd, opt.Repo, log, true, true);
            ours = true;
            report.Add($"started pid {pid} on port {p}");
            File.WriteAllText(Path.Combine(opt.Repo, "basalt.pid"), JsonSerializer.Serialize(new
            {
                pid, start = Process.GetProcessById(pid).StartTime.ToString("o"), mode = "app", port = p, profile = name, log,
            }));
        }
        catch (Exception e)
        {
            check.Text = "Avvio non riuscito: " + e.Message; check.ForeColor = Bad;
            return;
        }
        ShowServer(p, true);
    }

    async Task StartHeadless()
    {
        string name = SelectedName;
        int p = (int)port.Value;
        go.Enabled = headless.Enabled = false;
        check.Text = $"Avvio il solo server «{name}» sulla porta {p}…";
        var psi = new ProcessStartInfo("powershell.exe") { WorkingDirectory = opt.Repo, UseShellExecute = false, CreateNoWindow = true };
        foreach (var a in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Avvia-Basalt.ps1"), "--config", name, "--port", p.ToString(), "-SoloServer" })
            psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi);
        await proc.WaitForExitAsync();
        if (proc.ExitCode == 0)
        {
            MessageBox.Show(this, $"Server avviato sulla porta {p} senza finestra.\nIndirizzo: http://127.0.0.1:{p}/\nPer fermarlo: Ferma-Basalt.ps1 --port {p}", "Basalt", MessageBoxButtons.OK, MessageBoxIcon.Information);
            exiting = true; Close();
        }
        else { check.Text = "Il solo server non è partito: vedi logs\\basalt-serve.log."; check.ForeColor = Bad; go.Enabled = headless.Enabled = true; }
    }

    // ------------------------------------------------------------------ the web UI
    void ShowServer(int p, bool loading)
    {
        serverPort = p;
        start.Visible = false;
        web.Visible = true;
        _ = InitWeb(loading);
    }

    async Task InitWeb(bool loading)
    {
        if (web.CoreWebView2 == null)
        {
            string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Basalt", opt.TestMock ? "WebView2-test" : "WebView2");
            // the app speaks Italian, so does the page (the Loader and the chat settings follow the browser language)
            var env = await CoreWebView2Environment.CreateAsync(null, data, new CoreWebView2EnvironmentOptions { Language = "it-IT" });
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.Settings.AreDevToolsEnabled = opt.TestMock;
            web.CoreWebView2.Settings.IsStatusBarEnabled = false;
            web.CoreWebView2.DocumentTitleChanged += (_, _) => Text = "Basalt";
            web.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true }); };
        }
        if (loading)
        {
            web.CoreWebView2.NavigateToString("<html><body style='background:#0E1114;color:#E9ECEE;font:18px Segoe UI;display:flex;align-items:center;justify-content:center;height:90vh'>" +
                $"<div><b>Avvio di Basalt sulla porta {serverPort}…</b><p style='color:#9AA4AD'>La pagina si apre appena il server risponde; il modello si carica dietro (Monitor).</p></div></body></html>");
            poll.Start();
        }
        else web.CoreWebView2.Navigate($"http://127.0.0.1:{serverPort}/");
    }

    void WaitForServer()
    {
        if (ours && BasaltLauncher.ExitCode() >= 0)
        {
            poll.Stop();
            string log = Path.Combine(opt.Repo, "logs", "basalt-serve.log");
            string tail = "";
            try { using var s = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var r = new StreamReader(s); tail = string.Join("\n", r.ReadToEnd().Split('\n').TakeLast(12)); } catch { }
            MessageBox.Show(this, "Il server si è fermato durante l'avvio.\n\n" + tail, "Basalt", MessageBoxButtons.OK, MessageBoxIcon.Error);
            web.Visible = false; start.Visible = true; ours = false; _ = Check();
            return;
        }
        if (Native.GetJson($"http://127.0.0.1:{serverPort}/health") is JsonElement)
        {
            poll.Stop();
            report.Add("health ok");
            web.CoreWebView2.Navigate($"http://127.0.0.1:{serverPort}/");
            if (opt.Screenshot != null) _ = TestRun();
        }
    }

    // ------------------------------------------------------------------ the hidden test: screenshot, close to tray, turn off
    async Task TestRun()
    {
        await Task.Delay(3000);
        try
        {
            using var fs = File.Create(opt.Screenshot);
            await web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, fs);
            report.Add("screenshot " + opt.Screenshot);
        }
        catch (Exception e) { report.Add("screenshot failed: " + e.Message); }
        Close();                                           // a user close: the window hides, the server stays
        await Task.Delay(800);
        bool alive = Native.GetJson($"http://127.0.0.1:{serverPort}/health") is JsonElement;
        report.Add($"after close: window visible={Visible}, server answers={alive}");
        Snap(Path.ChangeExtension(opt.Screenshot, null) + "-tray.png", screen: true);
        if (opt.TestExit) TurnOff(false);
    }

    void Snap(string path, bool screen = false)
    {
        try
        {
            var b = screen ? Screen.PrimaryScreen.Bounds : Bounds;
            if (screen) b = new Rectangle(b.Right - 520, b.Bottom - 260, 520, 260);
            using var bmp = new Bitmap(b.Width, b.Height);
            using (var g = Graphics.FromImage(bmp)) g.CopyFromScreen(b.Location, Point.Empty, b.Size);
            bmp.Save(path, ImageFormat.Png);
            report.Add("screenshot " + path);
        }
        catch (Exception e) { report.Add("screenshot failed: " + e.Message); }
    }

    // ------------------------------------------------------------------ hide, show, turn off
    void SnapForm(string path)
    {
        using var bmp = new Bitmap(ClientSize.Width, ClientSize.Height);
        DrawToBitmap(bmp, new Rectangle(Point.Empty, ClientSize));
        bmp.Save(path, ImageFormat.Png);
        report.Add("screenshot " + path);
    }

    void ShowWindow()
    {
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!exiting && e.CloseReason == CloseReason.UserClosing && (web.Visible || ours))
        {
            e.Cancel = true;                               // the server keeps running: the window goes to the tray
            Hide();
            if (!BalloonShown())
            {
                tray.ShowBalloonTip(6000, "Basalt", "Basalt è ancora acceso: spegnilo dall'icona.", ToolTipIcon.Info);
                RememberBalloon();
            }
            return;
        }
        tray.Visible = false;
        base.OnFormClosing(e);
    }

    bool BalloonShown()
    {
        try { return File.Exists(statePath) && JsonDocument.Parse(File.ReadAllText(statePath)).RootElement.TryGetProperty("tray_tip_shown", out _); } catch { return false; }
    }

    void RememberBalloon()
    {
        if (opt.TestMock) return;
        try { Directory.CreateDirectory(Path.GetDirectoryName(statePath)); File.WriteAllText(statePath, "{\"tray_tip_shown\": true}"); } catch { }
    }

    async Task StopExternal(int p)
    {
        var psi = new ProcessStartInfo("powershell.exe") { WorkingDirectory = opt.Repo, UseShellExecute = false, CreateNoWindow = true };
        foreach (var a in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Ferma-Basalt.ps1"), "--port", p.ToString() })
            psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi);
        await proc.WaitForExitAsync();
    }

    /// <summary>Stop the server (the model unloads first, then the job ends every process), say how much RAM came
    /// back, and exit.</summary>
    void TurnOff(bool ask)
    {
        if (ask && MessageBox.Show(this, "Spegnere Basalt (server e motore)?", "Basalt", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        Cursor = Cursors.WaitCursor;
        double before = Native.AvailableRamGiB();
        if (serverPort != 0)
        {
            Native.PostJson($"http://127.0.0.1:{serverPort}/unload", "{}");
            if (ours)
            {
                BasaltLauncher.KillAll();
                BasaltLauncher.Wait(15000);
                try { File.Delete(Path.Combine(opt.Repo, "basalt.pid")); } catch { }
            }
            else StopExternal(serverPort).GetAwaiter().GetResult();
        }
        double after = before;
        for (int i = 0; i < 10 && after - before < 1; i++) { Thread.Sleep(500); after = Native.AvailableRamGiB(); }
        bool down = serverPort == 0 || Native.GetJson($"http://127.0.0.1:{serverPort}/health") == null;
        report.Add($"turned off: server down={down}, RAM available {before:0.0} -> {after:0.0} GiB");
        string text = down ? $"Basalt è spento. RAM disponibile: {after:0.0} GiB (liberati {Math.Max(0, after - before):0.0} GiB)."
                           : "Il server risponde ancora: fermalo con Ferma-Basalt.ps1.";
        if (opt.Screenshot != null)
            File.WriteAllLines(Path.ChangeExtension(opt.Screenshot, ".txt"), report);
        else
            MessageBox.Show(text, "Basalt", MessageBoxButtons.OK, down ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        ExitCode = down ? 0 : 1;
        exiting = true;
        Close();
    }

    void PrepareMock()
    {
        // the mock server: a scratch config, demo memory readings, nothing of the real model touched
        string dir = Path.Combine(Path.GetTempPath(), "basalt-desktop-test");
        Directory.CreateDirectory(dir);
        cfgPath = Path.Combine(dir, "mock_config.json");
        File.WriteAllText(cfgPath, "{\"server\": {\"port\": 7298, \"vision\": \"dynamic\", \"listen-first\": true}}");
        Environment.SetEnvironmentVariable("BASALT_CACHE", Path.Combine(dir, "cache"));
        Environment.SetEnvironmentVariable("BASALT_FAKE_SYSTEM", "{\"ram_total\": 137438953472, \"ram_available\": 118111600640, \"commit_limit\": 154618822656, \"commit_used\": 21474836480, \"vram_total\": 34359738368, \"vram_free\": 32212254720}");
        Environment.SetEnvironmentVariable("BASALT_FAKE_SYSTEM_OK", "1");
        if (opt.Port == 0) port.Value = 7298;
        else port.Value = opt.Port;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { tray.Dispose(); poll.Dispose(); }
        base.Dispose(disposing);
    }
}
