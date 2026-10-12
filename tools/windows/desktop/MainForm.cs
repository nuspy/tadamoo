// The window: a start panel (the model, the load profile, the VRAM to leave free, the port, the memory check, Avvia /
// Avvia solo server) while no server runs, then the server's own web UI in WebView2 (or the browser when WebView2 is
// missing). Until the models folders are set, a folder prompt comes first. Closing the window only hides it: Basalt
// keeps running behind the notification-area icon, whose menu opens it again, reloads with other settings or turns
// everything off.
using System.Diagnostics;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
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
    readonly ComboBox modelBox = new() { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, Width = 560, Font = new Font("Segoe UI", 12f), Margin = new Padding(0, 0, 12, 0) };
    readonly Label modelInfo = new() { AutoSize = true, ForeColor = Fg2, Font = new Font("Segoe UI", 10.5f), Margin = new Padding(0, 4, 0, 8) };
    readonly NumericUpDown port = new() { Minimum = 1, Maximum = 65535, Value = 7232, Width = 140, Font = new Font("Segoe UI", 12f) };
    readonly TrackBar reserveBar = new() { Minimum = 0, Maximum = 128, Value = 24, TickFrequency = 8, SmallChange = 1, LargeChange = 4, Width = 360, Height = 48, AutoSize = false };
    readonly NumericUpDown reserve = new() { Minimum = 0, Maximum = 32, DecimalPlaces = 2, Increment = 0.25m, Value = 6, Width = 110, Font = new Font("Segoe UI", 12f) };
    readonly Label check = new() { AutoSize = true, ForeColor = Fg2, Font = new Font("Consolas", 10.5f), Margin = new Padding(0, 8, 0, 8) };
    readonly FlowLayoutPanel startBar = new() { Dock = DockStyle.Bottom, AutoSize = false, Height = 84, WrapContents = false, Padding = new Padding(32, 12, 32, 16), BackColor = Color.FromArgb(21, 26, 31) };
    readonly Button go = Btn("Avvia", true), headless = Btn("Avvia solo server", false), quit = Btn("Esci", false);
    readonly Button foldersBtn = Btn("Cartelle dei modelli…", false), defaultBtn = Btn("Usa come modello predefinito per il server", false);
    readonly System.Windows.Forms.Timer poll = new() { Interval = 500 }, recheck = new() { Interval = 350 };
    // the folder prompt, shown instead of the start panel until the models folders are set
    readonly TableLayoutPanel folders = new() { Dock = DockStyle.Fill, Padding = new Padding(32, 24, 32, 24), BackColor = Bg, Visible = false };
    readonly ListBox folderList = new() { Height = 150, Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11.5f), IntegralHeight = false };
    readonly Label folderMsg = new() { AutoSize = true, ForeColor = Bad, Font = new Font("Segoe UI", 10.5f) };
    readonly FlowLayoutPanel suggestRow = new() { AutoSize = true, WrapContents = true, Margin = new Padding(0, 8, 0, 8) };
    readonly Button folderGo = Btn("Continua", true);
    List<JsonElement> profileList = new();
    List<JsonElement> modelList = new();
    string defaultModel;
    string cfgPath;
    int serverPort;                 // the port of the server this window shows
    bool ours;                      // started by this app: in its job object
    bool exiting, checkOk;
    int checkSeq;
    public int ExitCode { get; private set; }
    // per user: %APPDATA%\BasaltPannello\desktop.json (the last model, the tray tip); never in the repository
    readonly string statePath;
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
        statePath = opt.TestMock ? Path.Combine(Path.GetTempPath(), "basalt-desktop-test", "desktop.json")
                                 : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BasaltPannello", "desktop.json");

        var menu = new ContextMenuStrip();
        menu.Items.Add("Apri", null, (_, _) => ShowWindow());
        menu.Items.Add("Ricarica con altri parametri…", null, (_, _) => { ShowWindow(); if (web.Visible) web.CoreWebView2?.Navigate($"http://127.0.0.1:{serverPort}/#loader"); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Spegni", null, (_, _) => TurnOff(true));
        tray = new NotifyIcon { Icon = Icon, Text = "Basalt", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ShowWindow();

        BuildStartPanel();
        BuildFolderPanel();
        Controls.Add(web);
        Controls.Add(start);
        Controls.Add(folders);
        Controls.Add(startBar);                            // last: docked first, at the bottom
        poll.Tick += (_, _) => WaitForServer();
        recheck.Tick += (_, _) => { recheck.Stop(); _ = Check(); };
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

    static Label L(string t, float size, Color c, FontStyle st = FontStyle.Regular) => new() { Text = t, AutoSize = true, ForeColor = c, Font = new Font("Segoe UI", size, st), Margin = new Padding(0, 0, 0, 8) };

    static void WrapLabels(TableLayoutPanel p)
    {
        void Wrap()
        {
            int w = Math.Max(200, p.ClientSize.Width - p.Padding.Horizontal - 8);
            foreach (Control c in p.Controls) if (c is Label { AutoSize: true } l) l.MaximumSize = new Size(w, 0);
        }
        p.Resize += (_, _) => Wrap();
        Wrap();
    }

    void BuildStartPanel()
    {
        start.ColumnCount = 1;
        start.AutoScroll = true;
        start.Controls.Add(L("Basalt", 22f, Fg, FontStyle.Bold));
        start.Controls.Add(L("Scegli il modello e il profilo di caricamento: prima di avviare controllo che VRAM, RAM e commit bastino lasciando liberi il 15% della RAM e del commit.", 11f, Fg2));
        start.Controls.Add(L("Modello", 11.5f, Fg, FontStyle.Bold));
        var mrow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 0) };
        mrow.Controls.AddRange(new Control[] { modelBox, foldersBtn });
        start.Controls.Add(mrow);
        start.Controls.Add(modelInfo);
        var drow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 12) };
        drow.Controls.Add(defaultBtn);
        start.Controls.Add(drow);
        start.Controls.Add(L("Profilo di caricamento", 11.5f, Fg, FontStyle.Bold));
        start.Controls.Add(profiles);
        start.Controls.Add(L("VRAM da lasciare libera", 11.5f, Fg, FontStyle.Bold));
        var rrow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 0) };
        rrow.Controls.AddRange(new Control[] { reserveBar, reserve, L("GB", 11.5f, Fg) });
        start.Controls.Add(rrow);
        start.Controls.Add(L("Il resto della VRAM contiene esperti: più VRAM libera vuol dire meno esperti sulla GPU, un po' più lento e meno commit. Da 1 a 2 GB è il minimo per desktop e driver.", 10f, Fg2));
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 8) };
        row.Controls.Add(L("Porta", 11.5f, Fg, FontStyle.Bold));
        row.Controls.Add(port);
        start.Controls.Add(row);
        start.Controls.Add(check);
        // the buttons stay in sight at the bottom of the window while the panel above scrolls
        startBar.Controls.AddRange(new Control[] { go, headless, quit });
        start.VisibleChanged += (_, _) => startBar.Visible = start.Visible;
        start.Controls.Add(L("«Avvia solo server» avvia il server senza finestra né icona (per gli agenti); si ferma con Ferma-Basalt.ps1 --port N.", 10f, Fg2));
        for (int i = 0; i < start.Controls.Count; i++) start.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        WrapLabels(start);
        foreach (var c in new Control[] { profiles, modelBox, port, reserve }) { c.BackColor = Panel; c.ForeColor = Fg; }
        reserveBar.BackColor = Bg;
        profiles.SelectedIndexChanged += (_, _) =>
        {
            var p = SelectedProfile();
            if (p != null && p.Value.TryGetProperty("values", out var v))
            {
                if (v.TryGetProperty("server.port", out var pp) && opt.Port == 0) port.Value = pp.GetInt32();
                if (v.TryGetProperty("engine.vram-reserve-mib", out var rm) && rm.ValueKind == JsonValueKind.Number) SetReserve(rm.GetDouble() / 1024.0);
                if (p.Value.TryGetProperty("model", out var pm) && pm.ValueKind == JsonValueKind.String) SelectModel(pm.GetString());
            }
            _ = Check();
        };
        modelBox.SelectedIndexChanged += (_, _) => { ShowModelInfo(); _ = Check(); };
        bool syncing = false;
        reserveBar.Scroll += (_, _) => { if (syncing) return; syncing = true; reserve.Value = Math.Min(reserve.Maximum, reserveBar.Value / 4m); syncing = false; recheck.Stop(); recheck.Start(); };
        reserve.ValueChanged += (_, _) => { if (syncing) return; syncing = true; reserveBar.Value = (int)Math.Min(reserveBar.Maximum, Math.Round(reserve.Value * 4)); syncing = false; recheck.Stop(); recheck.Start(); };
        foldersBtn.Click += (_, _) => ShowFolders(true);
        defaultBtn.Click += async (_, _) => await MakeDefault();
        go.Click += async (_, _) => await StartServer();
        headless.Click += async (_, _) => await StartHeadless();
        quit.Click += (_, _) => { exiting = true; Close(); };
    }

    void SetReserve(double gb)
    {
        decimal v = Math.Max(reserve.Minimum, Math.Min(reserve.Maximum, Math.Round((decimal)gb * 4) / 4));
        reserve.Value = v;
    }

    void BuildFolderPanel()
    {
        folders.ColumnCount = 1;
        folders.AutoScroll = true;
        folders.Controls.Add(L("Dove sono i modelli?", 20f, Fg, FontStyle.Bold));
        folders.Controls.Add(L("Prima di continuare scegli una o più cartelle in cui cercare i modelli: file .basalt pronti e modelli GGUF scaricati (per esempio da LM Studio). La ricerca guarda anche nelle sottocartelle e legge solo le intestazioni dei file.", 11f, Fg2));
        folders.Controls.Add(folderList);
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 8, 0, 0) };
        var add = Btn("Aggiungi cartella…", false);
        var remove = Btn("Rimuovi", false);
        row.Controls.AddRange(new Control[] { add, remove });
        folders.Controls.Add(row);
        folders.Controls.Add(L("Trovate su questo PC:", 10.5f, Fg2));
        folders.Controls.Add(suggestRow);
        folders.Controls.Add(folderMsg);
        var buttons = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 0) };
        var leave = Btn("Esci", false);
        buttons.Controls.AddRange(new Control[] { folderGo, leave });
        folders.Controls.Add(buttons);
        for (int i = 0; i < folders.Controls.Count; i++) folders.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        folders.RowStyles[2] = new RowStyle(SizeType.Absolute, 160);
        WrapLabels(folders);
        folderList.BackColor = Panel; folderList.ForeColor = Fg;
        folderGo.Enabled = false;
        add.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = "Cartella dei modelli", UseDescriptionForTitle = true, ShowNewFolderButton = false };
            if (dlg.ShowDialog(this) == DialogResult.OK) AddFolder(dlg.SelectedPath);
        };
        remove.Click += (_, _) => { if (folderList.SelectedIndex >= 0) folderList.Items.RemoveAt(folderList.SelectedIndex); folderGo.Enabled = folderList.Items.Count > 0; };
        folderGo.Click += async (_, _) => await SaveFolders();
        leave.Click += (_, _) => { if (start.Visible || modelList.Count > 0) ShowFolders(false); else { exiting = true; Close(); } };
    }

    void AddFolder(string path)
    {
        if (string.IsNullOrWhiteSpace(path)) return;
        foreach (var x in folderList.Items) if (string.Equals((string)x, path, StringComparison.OrdinalIgnoreCase)) return;
        folderList.Items.Add(path);
        folderGo.Enabled = true;
        folderMsg.Text = "";
    }

    /// <summary>The folder prompt (with the folders saved so far and the ones found on this PC) or back to the start panel.</summary>
    void ShowFolders(bool on)
    {
        folders.Visible = on;
        start.Visible = !on;
        if (on) _ = FillFolders();
    }

    async Task FillFolders()
    {
        var (code, output) = await Py("-m", "serve.models", "dirs");
        if (code != 0) { folderMsg.Text = output; return; }
        var root = JsonDocument.Parse(output.Trim().Split('\n').Last()).RootElement;
        folderList.Items.Clear();
        foreach (var d in root.GetProperty("dirs").EnumerateArray()) folderList.Items.Add(d.GetString());
        suggestRow.Controls.Clear();
        foreach (var s in root.GetProperty("suggestions").EnumerateArray())
        {
            string path = s.GetString();
            var b = Btn("Aggiungi " + path, false);
            b.Click += (_, _) => AddFolder(path);
            suggestRow.Controls.Add(b);
        }
        if (suggestRow.Controls.Count == 0) suggestRow.Controls.Add(L("(nessuna cartella di modelli nota trovata)", 10.5f, Fg2));
        folderGo.Enabled = folderList.Items.Count > 0;
    }

    async Task SaveFolders()
    {
        var dirs = folderList.Items.Cast<string>().ToList();
        if (dirs.Count == 0) { folderMsg.Text = "Aggiungi almeno una cartella."; return; }
        folderGo.Enabled = false;
        folderMsg.ForeColor = Fg2; folderMsg.Text = "Cerco nelle cartelle…";
        var (code, output) = await Py(new[] { "-m", "serve.models", "set-dirs" }.Concat(dirs).ToArray());
        folderGo.Enabled = true;
        if (code != 0) { folderMsg.ForeColor = Bad; folderMsg.Text = output.Trim(); return; }
        folderMsg.Text = "";
        ShowFolders(false);
        await LoadModels();
        await Check();
    }

    // ------------------------------------------------------------------ the models (serve.models json)
    JsonElement? SelectedModel() => modelBox.SelectedIndex >= 0 && modelBox.SelectedIndex < modelList.Count ? modelList[modelBox.SelectedIndex] : null;
    string SelectedModelPath => SelectedModel()?.GetProperty("path").GetString();
    bool SelectedReady => SelectedModel() is JsonElement m && m.GetProperty("status").GetString() == "ready";

    static string Status(string s) => s switch { "ready" => "✓ Pronto", "convert" => "⇄ Da convertire", _ => "✕ Non compatibile" };
    static string Gb(long bytes) => (bytes / 1073741824.0).ToString("0.0", System.Globalization.CultureInfo.GetCultureInfo("it-IT")) + " GB";

    async Task LoadModels()
    {
        modelInfo.ForeColor = Fg2;
        modelInfo.Text = "Cerco i modelli nelle cartelle…";
        var (code, output) = await Py("-m", "serve.models", "json", "--config", cfgPath);
        modelList.Clear();
        modelBox.Items.Clear();
        if (code != 0) { modelInfo.Text = "Non riesco a leggere i modelli:\n" + output; return; }
        var root = JsonDocument.Parse(output.Trim().Split('\n').Last()).RootElement.Clone();
        defaultModel = root.TryGetProperty("default_model", out var dm) && dm.ValueKind == JsonValueKind.String ? dm.GetString() : null;
        int incompatible = 0;
        foreach (var m in root.GetProperty("models").EnumerateArray())
        {
            string st = m.GetProperty("status").GetString();
            if (st == "incompatible") { incompatible++; continue; }
            modelList.Add(m);
            string quant = m.TryGetProperty("quant", out var q) && q.ValueKind == JsonValueKind.String ? " · " + q.GetString() : "";
            modelBox.Items.Add($"{Status(st)} · {m.GetProperty("name").GetString()}{quant} · {Gb(m.GetProperty("size_bytes").GetInt64())}");
        }
        modelBox.Tag = incompatible;
        // the model used last time, else the config file's, else the default server model, else the first ready one
        string last = ReadState("last_model");
        string current = root.TryGetProperty("current", out var cur) && cur.ValueKind == JsonValueKind.String ? cur.GetString() : null;
        if (!SelectModel(opt.Model) && !SelectModel(last) && !SelectModel(current) && !SelectModel(defaultModel))
        {
            int i = modelList.FindIndex(x => x.GetProperty("status").GetString() == "ready");
            if (i >= 0) modelBox.SelectedIndex = i;
        }
        if (modelList.Count == 0) modelInfo.Text = $"Nessun modello pronto o da convertire nelle cartelle ({incompatible} file non compatibili). Usa «Cartelle dei modelli…».";
        ShowModelInfo();
    }

    bool SelectModel(string path)
    {
        if (string.IsNullOrEmpty(path)) return false;
        string norm(string p) => p.Replace('\\', '/').ToLowerInvariant();
        int i = modelList.FindIndex(x => norm(x.GetProperty("path").GetString()) == norm(path));
        if (i < 0) return false;
        modelBox.SelectedIndex = i;
        return true;
    }

    void ShowModelInfo()
    {
        var m = SelectedModel();
        if (m == null) { defaultBtn.Visible = false; return; }
        var e = m.Value;
        string st = e.GetProperty("status").GetString();
        var parts = new List<string> { e.GetProperty("path").GetString() };
        if (e.TryGetProperty("experts_bytes", out var eb) && eb.GetInt64() > 0) parts.Add("esperti " + Gb(eb.GetInt64()));
        if (e.TryGetProperty("mtp", out var mtp) && mtp.ValueKind == JsonValueKind.String) parts.Add(mtp.GetString() == "present" ? "drafter MTP" : mtp.GetString() == "source" ? "MTP da mtp-qwen" : "MTP da scaricare");
        if (e.TryGetProperty("vision", out var vis) && vis.ValueKind == JsonValueKind.True) parts.Add("visione");
        string why = e.GetProperty("reason").GetProperty("it").GetString();
        string extra = st == "convert" ? "\nDa convertire: avvia Basalt con un modello pronto e usa «Carica modello» → Converti, oppure Avvia-Basalt.ps1 --model \"<file gguf>\" -Converti." : "";
        int inc = modelBox.Tag is int n ? n : 0;
        modelInfo.ForeColor = st == "ready" ? Fg2 : Bad;
        modelInfo.Text = string.Join(" · ", parts) + "\n" + why + extra + (inc > 0 ? $"\n({inc} file non compatibili nelle cartelle: l'elenco completo è in «Carica modello».)" : "");
        bool isDefault = defaultModel != null && string.Equals(defaultModel.Replace('\\', '/'), SelectedModelPath?.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase);
        defaultBtn.Visible = st == "ready";
        defaultBtn.Enabled = !isDefault;
        defaultBtn.Text = isDefault ? "★ Modello predefinito per il server" : "Usa come modello predefinito per il server";
    }

    async Task MakeDefault()
    {
        string path = SelectedModelPath;
        if (path == null || !SelectedReady) return;
        var (code, output) = await Py("-m", "serve.models", "default", "--set", path);
        if (code != 0) { modelInfo.ForeColor = Bad; modelInfo.Text = output; return; }
        defaultModel = path;
        ShowModelInfo();
    }

    // ------------------------------------------------------------------ per-user state (%APPDATA%\BasaltPannello)
    string ReadState(string key)
    {
        foreach (var p in new[] { statePath, Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Basalt", "desktop.json") })
        {
            try
            {
                if (File.Exists(p) && JsonDocument.Parse(File.ReadAllText(p)).RootElement.TryGetProperty(key, out var v))
                    return v.ValueKind == JsonValueKind.String ? v.GetString() : v.ToString();
            }
            catch { }
            if (opt.TestMock) break;
        }
        return null;
    }

    void WriteState(string key, object value)
    {
        try
        {
            var d = new Dictionary<string, object>();
            if (File.Exists(statePath))
                foreach (var p in JsonDocument.Parse(File.ReadAllText(statePath)).RootElement.EnumerateObject()) d[p.Name] = p.Value.Clone();
            d[key] = value;
            Directory.CreateDirectory(Path.GetDirectoryName(statePath));
            File.WriteAllText(statePath, JsonSerializer.Serialize(d));
        }
        catch { }
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
        bool autoStart = false;
        var running = opt.TestFolderPrompt ? new List<(int, JsonElement)>() : await Task.Run(Native.RunningServers);
        if (running.Count > 0)
        {
            var (p, h) = running[0];
            string model = h.TryGetProperty("model", out var m) ? m.GetString() : "?";
            string last = opt.Model ?? ReadState("last_model");
            string want = last != null ? Path.GetFileNameWithoutExtension(last) : "il modello scelto";
            var attach = new TaskDialogButton("Collegati al server in esecuzione");
            var change = new TaskDialogButton($"Cambia modello: chiudi il server attuale e carica «{want}»");
            var page = new TaskDialogPage
            {
                Caption = "Basalt", Heading = $"Basalt è già acceso sulla porta {p}",
                Text = $"Modello in uso: {model}. Puoi usare questo server, oppure chiuderlo (solo lui e i suoi processi) e caricare il modello scelto qui.",
                Buttons = { attach, change, TaskDialogButton.Cancel }, Icon = TaskDialogIcon.Information,
            };
            var choice = opt.TestMock ? attach : TaskDialog.ShowDialog(this, page);
            if (choice == attach) { ShowServer(p, false); return; }
            if (choice != change) { exiting = true; Close(); return; }
            await StopExternal(p);
            autoStart = true;
        }
        var (dc, dout) = await Py("-m", "serve.models", "dirs");
        bool configured = dc == 0 && JsonDocument.Parse(dout.Trim().Split('\n').Last()).RootElement.GetProperty("configured").GetBoolean();
        if (!configured)
        {
            ShowFolders(true);
            await FillFolders();
            if (opt.TestFolderPrompt)
            {
                await Task.Delay(300);
                if (opt.Screenshot != null) SnapForm(Path.ChangeExtension(opt.Screenshot, null) + "-folders.png");
                if (opt.TestModelsDir == null) { ExitCode = 0; exiting = true; Close(); return; }
                AddFolder(opt.TestModelsDir);
                await SaveFolders();
                await LoadProfiles();
                await Check();
                await Task.Delay(300);
                if (opt.Screenshot != null) SnapForm(Path.ChangeExtension(opt.Screenshot, null) + "-panel.png");
                if (opt.Screenshot != null) File.WriteAllLines(Path.ChangeExtension(opt.Screenshot, ".txt"), report.Append("check: " + check.Text));
                ExitCode = 0; exiting = true; Close();
            }
            return;                                        // the start panel follows the folders' "Continua"
        }
        await LoadModels();
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
            return;
        }
        await Check();
        if (autoStart && checkOk) await StartServer();     // "Cambia modello": the old server is gone, load the new one
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
        checkOk = false;
        if (SelectedModel() != null && !SelectedReady)
        {
            check.ForeColor = Bad;
            check.Text = "Questo modello va convertito prima di caricarlo (vedi sopra).";
            return;
        }
        check.ForeColor = Fg2;
        check.Text = "Controllo la memoria…";
        string tmp = Path.Combine(Path.GetTempPath(), $"basalt_config.check.{Environment.ProcessId}.{seq}.json");   // one per check: they can overlap
        string output; int code;
        try
        {
            File.Copy(cfgPath, tmp, true);
            var ap = await Py(ApplyArgs(name, tmp));
            if (ap.code != 0) { if (seq == checkSeq) { check.Text = ap.output; check.ForeColor = Bad; } return; }
            (code, output) = await Py("-m", "serve.loader", "check", "--config", tmp);
        }
        finally { try { File.Delete(tmp); File.Delete(tmp + ".bak"); } catch { } }
        if (seq != checkSeq) return;
        var lines = output.TrimEnd().Split('\n').Select(l => l.TrimEnd('\r')).ToList();
        if (lines.Count > 0 && lines[^1].StartsWith("{")) lines.RemoveAt(lines.Count - 1);
        checkOk = code == 0;
        string verdict = code switch
        {
            0 => "\n\nOK: si può avviare.",
            4 => "\n\nNon si avvia: il motore di Basalt gira solo su GPU NVIDIA Blackwell (RTX 50, RTX PRO 6000, B200).",
            _ => "\n\nNon si avvia: la memoria non basta senza usare il file di paging. Scegli un altro profilo, lascia libera meno VRAM o chiudi altri programmi.",
        };
        check.Text = string.Join("\n", lines) + verdict;
        check.ForeColor = checkOk ? Fg : Bad;
        go.Enabled = headless.Enabled = checkOk;
    }

    /// <summary>serve.profiles apply with the chosen model and the VRAM to leave free of the panel.</summary>
    string[] ApplyArgs(string profile, string config)
    {
        var a = new List<string> { "-m", "serve.profiles", "apply", profile, "--config", config,
                                   "--vram-reserve-mib", ((int)Math.Round(reserve.Value * 1024)).ToString() };
        if (SelectedReady) { a.Add("--model"); a.Add(SelectedModelPath); }
        return a.ToArray();
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
            var ap = await Py(ApplyArgs(name, cfgPath));
            if (ap.code != 0) { check.Text = ap.output; check.ForeColor = Bad; return; }
            exe = Path.Combine(opt.Repo, "bin", "basalt.exe");
            cmd = $"\"{exe}\" serve --config \"{cfgPath}\" --port {p}";
        }
        if (SelectedReady) WriteState("last_model", SelectedModelPath);
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
        var args = new List<string> { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Avvia-Basalt.ps1"), "--config", name, "--port", p.ToString(),
                                      "--vram-reserve-mib", ((int)Math.Round(reserve.Value * 1024)).ToString(), "-SoloServer" };
        if (SelectedReady) { args.Add("--model"); args.Add(SelectedModelPath); WriteState("last_model", SelectedModelPath); }
        foreach (var a in args) psi.ArgumentList.Add(a);
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

    bool useBrowser;                // WebView2 is missing: the page opens in the default browser

    async Task InitWeb(bool loading)
    {
        if (web.CoreWebView2 == null && !useBrowser)
        {
            try
            {
                if (opt.TestNoWebView) throw new WebView2RuntimeNotFoundException("test");
                string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Basalt", opt.TestMock ? "WebView2-test" : "WebView2");
                // the app speaks Italian, so does the page (the Loader and the chat settings follow the browser language)
                var env = await CoreWebView2Environment.CreateAsync(null, data, new CoreWebView2EnvironmentOptions { Language = "it-IT" });
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2.Settings.AreDevToolsEnabled = opt.TestMock;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.DocumentTitleChanged += (_, _) => Text = "Basalt";
                web.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true }); };
            }
            catch (Exception e) when (e is WebView2RuntimeNotFoundException || e is COMException || e is FileNotFoundException)
            {
                useBrowser = true;
                report.Add("webview2 missing: " + e.Message);
                const string get = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
                var answer = opt.TestMock ? DialogResult.Yes : MessageBox.Show(this,
                    "Manca Microsoft Edge WebView2 Runtime, che serve per mostrare Basalt in questa finestra.\n\n" +
                    "Sì: apro Basalt nel browser (il server resta acceso dietro l'icona vicino all'orologio).\n" +
                    "No: apro la pagina per installare WebView2 (poi riavvia BasaltPannello).",
                    "Basalt", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (answer == DialogResult.No) Process.Start(new ProcessStartInfo(get) { UseShellExecute = true });
                web.Visible = false;
                start.Visible = true;
                check.ForeColor = Fg2;
                check.Text = $"Basalt si apre nel browser: http://127.0.0.1:{serverPort}/ (WebView2 non è installato).";
            }
        }
        if (useBrowser)
        {
            if (loading) poll.Start();
            else OpenBrowser();
            return;
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
            if (useBrowser) { OpenBrowser(); return; }
            web.CoreWebView2.Navigate($"http://127.0.0.1:{serverPort}/");
            if (opt.Screenshot != null) _ = TestRun();
        }
    }

    void OpenBrowser()
    {
        string url = $"http://127.0.0.1:{serverPort}/";
        report.Add("browser " + url);
        if (!opt.TestMock) Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        if (opt.TestMock && opt.Screenshot != null)
        {
            SnapForm(Path.ChangeExtension(opt.Screenshot, null) + "-browser.png");
            if (opt.TestExit) TurnOff(false);
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
        using var bmp = new Bitmap(Width, Height);             // the whole window: DrawToBitmap includes the title bar
        DrawToBitmap(bmp, new Rectangle(Point.Empty, Size));
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

    bool BalloonShown() => ReadState("tray_tip_shown") != null;

    void RememberBalloon()
    {
        if (opt.TestMock) return;
        WriteState("tray_tip_shown", true);
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
        if (opt.TestFolderPrompt)                          // the folders are not set yet: the prompt comes first
            try { File.Delete(Path.Combine(dir, "cache", "models.json")); File.Delete(statePath); } catch { }
        Environment.SetEnvironmentVariable("BASALT_FAKE_SYSTEM", "{\"ram_total\": 137438953472, \"ram_available\": 118111600640, \"commit_limit\": 154618822656, \"commit_used\": 21474836480, \"vram_total\": 34359738368, \"vram_free\": 32212254720}");
        Environment.SetEnvironmentVariable("BASALT_FAKE_GPU", "{\"name\": \"NVIDIA GeForce RTX 5090\", \"cc\": [12, 0], \"vram_total\": 34359738368, \"vram_free\": 32212254720, \"processes\": [{\"pid\": 11, \"name\": \"dwm.exe\"}]}");
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
