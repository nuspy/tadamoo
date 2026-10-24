// The window of "Basalt Win+GUI": the server's own web UI in WebView2 (or the browser when WebView2 is missing). On
// opening, the app starts the server without loading a model (--standby) and shows its Loader: the models folders (the
// folder dialog is this
// app's, asked for by the page), the model list, profiles, every value, the memory and speed prediction, the
// configuration wizard and Load. A Load of another model ends the server with exit code 75 and the app starts it again
// with that model. The native start panel (model, profile, VRAM, port, check, Avvia / Avvia solo server) is only the
// fallback when the web server cannot start, with the reason and the log. Closing the window only hides it: Basalt
// keeps running behind the notification-area icon, whose menu opens it again, reloads with other settings, starts the
// server alone for agents or turns everything off.
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
    readonly TrackBar reserveBar = new() { Minimum = 1, Maximum = 128, Value = 24, TickFrequency = 8, SmallChange = 1, LargeChange = 4, Width = 360, Height = 48, AutoSize = false };
    readonly NumericUpDown reserve = new() { Minimum = 0.25m, Maximum = 32, DecimalPlaces = 2, Increment = 0.25m, Value = 6, Width = 110, Font = new Font("Segoe UI", 12f) };
    readonly Label check = new() { AutoSize = true, ForeColor = Fg2, Font = new Font("Consolas", 10.5f), Margin = new Padding(0, 8, 0, 8) };
    readonly Label reasonLabel = new() { AutoSize = true, ForeColor = Color.FromArgb(255, 143, 163), Font = new Font("Consolas", 10f), Margin = new Padding(0, 0, 0, 12), Visible = false };
    readonly FlowLayoutPanel startBar = new() { Dock = DockStyle.Bottom, AutoSize = false, Height = 84, WrapContents = false, Padding = new Padding(32, 12, 32, 16), BackColor = Color.FromArgb(21, 26, 31) };
    readonly Button go = Btn(Tr.T("Avvia", "Start"), true), headless = Btn(Tr.T("Avvia solo server", "Start server only"), false), quit = Btn(Tr.T("Esci", "Quit"), false);
    readonly Button foldersBtn = Btn(Tr.T("Cartelle dei modelli…", "Models folders…"), false), defaultBtn = Btn(Tr.T("Usa come modello predefinito per il server", "Use as the default server model"), false);
    readonly System.Windows.Forms.Timer poll = new() { Interval = 500 }, recheck = new() { Interval = 350 };
    // the folder prompt, shown instead of the start panel until the models folders are set
    readonly TableLayoutPanel folders = new() { Dock = DockStyle.Fill, Padding = new Padding(32, 24, 32, 24), BackColor = Bg, Visible = false };
    readonly ListBox folderList = new() { Height = 150, Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11.5f), IntegralHeight = false };
    readonly Label folderMsg = new() { AutoSize = true, ForeColor = Bad, Font = new Font("Segoe UI", 10.5f) };
    readonly FlowLayoutPanel suggestRow = new() { AutoSize = true, WrapContents = true, Margin = new Padding(0, 8, 0, 8) };
    readonly Button folderGo = Btn(Tr.T("Continua", "Continue"), true);
    List<JsonElement> profileList = new();
    List<JsonElement> modelList = new();
    string defaultModel;
    string cfgPath;
    int serverPort;                 // the port of the server this window shows
    bool ours;                      // started by this app: in its job object
    bool exiting, checkOk;
    bool waitingHealth;             // the server just started: the page opens when /health answers
    bool loadingProfiles;           // the profile list is being refilled: no model change, no check
    bool firstPage = true;          // the first page carries the command line's profile, model and VRAM
    string pageView = "loader";     // the view the page opens on
    int checkSeq;
    public const int RelaunchExitCode = 75;   // the server saved another model: start it again (serve/config.py)
    public int ExitCode { get; private set; }
    // per user: %BASALT_DATA% or %APPDATA%\BasaltPannello\desktop.json (the last model, the tray tip), next to the
    // server's profiles.json and models.json (serve/userdata.py); never in the repository
    readonly string statePath;
    readonly List<string> report = new();

    public MainForm(Options opt, EventWaitHandle showEvent)
    {
        this.opt = opt;
        this.showEvent = showEvent;
        Text = Program.AppName;
        Icon = LoadIcon();
        BackColor = Bg;
        ForeColor = Fg;
        MinimumSize = new Size(420, 520);
        Size = new Size(1360, 900);
        StartPosition = FormStartPosition.CenterScreen;
        cfgPath = opt.ConfigFile != null ? Path.GetFullPath(opt.ConfigFile) : Path.Combine(opt.Repo, "basalt_config.json");
        string data = Environment.GetEnvironmentVariable("BASALT_DATA");
        statePath = opt.TestMock ? Path.Combine(Path.GetTempPath(), "basalt-desktop-test", "desktop.json")
                  : Path.Combine(string.IsNullOrEmpty(data) ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BasaltPannello") : data, "desktop.json");

        var menu = new ContextMenuStrip();
        menu.Items.Add(Tr.T("Apri", "Open"), null, (_, _) => ShowWindow());
        menu.Items.Add(Tr.T("Ricarica con altri parametri…", "Reload with other settings…"), null, (_, _) => { ShowWindow(); if (web.Visible) web.CoreWebView2?.Navigate($"http://127.0.0.1:{serverPort}/#loader"); });
        menu.Items.Add(Tr.T("Avvia solo server (per gli agenti)…", "Start the server alone (for agents)…"), null, async (_, _) => await HeadlessFromTray());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(Tr.T("Spegni", "Turn off"), null, (_, _) => TurnOff(true));
        tray = new NotifyIcon { Icon = Icon, Text = Program.AppName, ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ShowWindow();

        BuildStartPanel();
        BuildFolderPanel();
        Controls.Add(web);
        Controls.Add(start);
        Controls.Add(folders);
        Controls.Add(startBar);                            // last: docked first, at the bottom
        poll.Tick += (_, _) => WatchServer();
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
        start.Controls.Add(L(Program.AppName, 22f, Fg, FontStyle.Bold));
        start.Controls.Add(reasonLabel);
        start.Controls.Add(L(Tr.T("Scegli il modello e il profilo di caricamento: prima di avviare controllo che VRAM, RAM e commit bastino lasciando libera la parte di RAM e di commit scelta per gli altri programmi.", "Choose the model and the load profile: before starting, VRAM, RAM and commit are checked, leaving free the share of RAM and commit chosen for the other programs."), 11f, Fg2));
        start.Controls.Add(L(Tr.T("Modello", "Model"), 11.5f, Fg, FontStyle.Bold));
        var mrow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 0) };
        mrow.Controls.AddRange(new Control[] { modelBox, foldersBtn });
        start.Controls.Add(mrow);
        start.Controls.Add(modelInfo);
        var drow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 12) };
        drow.Controls.Add(defaultBtn);
        start.Controls.Add(drow);
        start.Controls.Add(L(Tr.T("Profilo di caricamento", "Load profile"), 11.5f, Fg, FontStyle.Bold));
        start.Controls.Add(profiles);
        start.Controls.Add(L(Tr.T("VRAM da lasciare libera", "VRAM to leave free"), 11.5f, Fg, FontStyle.Bold));
        var rrow = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 0) };
        rrow.Controls.AddRange(new Control[] { reserveBar, reserve, L("GB", 11.5f, Fg) });
        start.Controls.Add(rrow);
        start.Controls.Add(L(Tr.T("Il resto della VRAM contiene esperti: più VRAM libera vuol dire meno esperti sulla GPU, un po' più lento e meno commit. Da 1 a 2 GB è il minimo per desktop e driver.", "The rest of the VRAM holds experts: more free VRAM means fewer experts on the GPU, a little slower, and less commit. 1 to 2 GB is the least for the desktop and the driver."), 10f, Fg2));
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 8) };
        row.Controls.Add(L(Tr.T("Porta", "Port"), 11.5f, Fg, FontStyle.Bold));
        row.Controls.Add(port);
        start.Controls.Add(row);
        start.Controls.Add(check);
        // the buttons stay in sight at the bottom of the window while the panel above scrolls
        startBar.Controls.AddRange(new Control[] { go, headless, quit });
        start.VisibleChanged += (_, _) => startBar.Visible = start.Visible;
        start.Controls.Add(L(Tr.T("«Avvia solo server» avvia il server senza finestra né icona (per gli agenti); si ferma con Ferma-Basalt.ps1 --port N.", "“Start server only” starts the server without window or icon (for agents); Ferma-Basalt.ps1 --port N stops it."), 10f, Fg2));
        for (int i = 0; i < start.Controls.Count; i++) start.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        WrapLabels(start);
        foreach (var c in new Control[] { profiles, modelBox, port, reserve }) { c.BackColor = Panel; c.ForeColor = Fg; }
        reserveBar.BackColor = Bg;
        profiles.SelectedIndexChanged += (_, _) =>
        {
            var p = SelectedProfile();
            if (p != null && p.Value.TryGetProperty("values", out var v))
            {
                if (v.TryGetProperty("server.port", out var pp) && opt.Port == 0 && !opt.TestMock) port.Value = pp.GetInt32();
                if (v.TryGetProperty("engine.vram-reserve-mib", out var rm) && rm.ValueKind == JsonValueKind.Number) SetReserve(rm.GetDouble() / 1024.0);
                if (!loadingProfiles && p.Value.TryGetProperty("model", out var pm) && pm.ValueKind == JsonValueKind.String) SelectModel(pm.GetString());
            }
            if (!loadingProfiles) _ = Check();
        };
        modelBox.SelectedIndexChanged += async (_, _) =>
        {
            ShowModelInfo();
            if (profileList.Count > 0 && !loadingProfiles) await LoadProfiles();   // their summaries are for this model
            await Check();
        };
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
        folders.Controls.Add(L(Tr.T("Dove sono i modelli?", "Where are the models?"), 20f, Fg, FontStyle.Bold));
        folders.Controls.Add(L(Tr.T("Prima di continuare scegli una o più cartelle in cui cercare i modelli: file .basalt pronti e modelli GGUF scaricati (per esempio da LM Studio). La ricerca guarda anche nelle sottocartelle e legge solo le intestazioni dei file.", "Before going on, choose one or more folders to search for models: ready .basalt files and downloaded GGUF models (for example from LM Studio). The search also looks in the subfolders and reads only the files' headers."), 11f, Fg2));
        folders.Controls.Add(folderList);
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 8, 0, 0) };
        var add = Btn(Tr.T("Aggiungi cartella…", "Add a folder…"), false);
        var remove = Btn(Tr.T("Rimuovi", "Remove"), false);
        row.Controls.AddRange(new Control[] { add, remove });
        folders.Controls.Add(row);
        folders.Controls.Add(L(Tr.T("Trovate su questo PC:", "Found on this PC:"), 10.5f, Fg2));
        folders.Controls.Add(suggestRow);
        folders.Controls.Add(folderMsg);
        var buttons = new FlowLayoutPanel { AutoSize = true, WrapContents = true, Margin = new Padding(0, 12, 0, 0) };
        var leave = Btn(Tr.T("Esci", "Quit"), false);
        buttons.Controls.AddRange(new Control[] { folderGo, leave });
        folders.Controls.Add(buttons);
        for (int i = 0; i < folders.Controls.Count; i++) folders.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        folders.RowStyles[2] = new RowStyle(SizeType.Absolute, 160);
        WrapLabels(folders);
        folderList.BackColor = Panel; folderList.ForeColor = Fg;
        folderGo.Enabled = false;
        add.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = Tr.T("Cartella dei modelli", "Models folder"), UseDescriptionForTitle = true, ShowNewFolderButton = false };
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
            var b = Btn(Tr.T("Aggiungi ", "Add ") + path, false);
            b.Click += (_, _) => AddFolder(path);
            suggestRow.Controls.Add(b);
        }
        if (suggestRow.Controls.Count == 0) suggestRow.Controls.Add(L(Tr.T("(nessuna cartella di modelli nota trovata)", "(no known models folder found)"), 10.5f, Fg2));
        folderGo.Enabled = folderList.Items.Count > 0;
    }

    async Task SaveFolders()
    {
        var dirs = folderList.Items.Cast<string>().ToList();
        if (dirs.Count == 0) { folderMsg.Text = Tr.T("Aggiungi almeno una cartella.", "Add at least one folder."); return; }
        folderGo.Enabled = false;
        folderMsg.ForeColor = Fg2; folderMsg.Text = Tr.T("Cerco nelle cartelle…", "Searching the folders…");
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

    static string Status(string s) => s switch { "ready" => Tr.T("✓ Pronto", "✓ Ready"), "convert" => Tr.T("⇄ Da convertire", "⇄ To convert"), _ => Tr.T("✕ Non compatibile", "✕ Not compatible") };
    static string Gb(long bytes) => (bytes / 1073741824.0).ToString("0.0", System.Globalization.CultureInfo.GetCultureInfo("it-IT")) + " GB";

    async Task LoadModels()
    {
        modelInfo.ForeColor = Fg2;
        modelInfo.Text = Tr.T("Cerco i modelli nelle cartelle…", "Searching the models in the folders…");
        var (code, output) = await Py("-m", "serve.models", "json", "--config", cfgPath);
        modelList.Clear();
        modelBox.Items.Clear();
        if (code != 0) { modelInfo.Text = Tr.T("Non riesco a leggere i modelli:\n", "Cannot read the models:\n") + output; return; }
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
        if (modelList.Count == 0) modelInfo.Text = Tr.T($"Nessun modello pronto o da convertire nelle cartelle ({incompatible} file non compatibili). Usa «Cartelle dei modelli…».", $"No model ready or to convert in the folders ({incompatible} files not compatible). Use “Models folders…”.");
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
        if (e.TryGetProperty("experts_bytes", out var eb) && eb.GetInt64() > 0) parts.Add(Tr.T("esperti ", "experts ") + Gb(eb.GetInt64()));
        if (e.TryGetProperty("mtp", out var mtp) && mtp.ValueKind == JsonValueKind.String) parts.Add(mtp.GetString() == "present" ? Tr.T("drafter MTP", "MTP drafter") : mtp.GetString() == "source" ? Tr.T("MTP da mtp-qwen", "MTP from mtp-qwen") : Tr.T("MTP da scaricare", "MTP to download"));
        if (e.TryGetProperty("vision", out var vis) && vis.ValueKind == JsonValueKind.True) parts.Add(Tr.T("visione", "vision"));
        string why = e.GetProperty("reason").GetProperty(Tr.Code).GetString();
        string extra = st == "convert" ? Tr.T("\nDa convertire: avvia Basalt Win+GUI con un modello pronto e usa «Carica modello» → Converti, oppure Avvia-Basalt.ps1 --model \"<file gguf>\" -converti.", "\nTo convert: start Basalt Win+GUI with a ready model and use the Loader → Converti, or Avvia-Basalt.ps1 --model \"<gguf file>\" -converti.") : "";
        int inc = modelBox.Tag is int n ? n : 0;
        modelInfo.ForeColor = st == "ready" ? Fg2 : Bad;
        modelInfo.Text = string.Join(" · ", parts) + "\n" + why + extra + (inc > 0 ? Tr.T($"\n({inc} file non compatibili nelle cartelle: l'elenco completo è in «Carica modello».)", $"\n({inc} files not compatible in the folders: the full list is in the Loader.)") : "");
        bool isDefault = defaultModel != null && string.Equals(defaultModel.Replace('\\', '/'), SelectedModelPath?.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase);
        defaultBtn.Visible = st == "ready";
        defaultBtn.Enabled = !isDefault;
        defaultBtn.Text = isDefault ? Tr.T("★ Modello predefinito per il server", "★ Default server model") : Tr.T("Usa come modello predefinito per il server", "Use as the default server model");
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

    // ------------------------------------------------------------------ start: attach, or the server without a model
    async Task Begin()
    {
        if (opt.TestMock) PrepareMock();
        start.Visible = false;
        var running = opt.TestMock ? new List<(int, JsonElement)>() : await Task.Run(Native.RunningServers);
        if (running.Count > 0)
        {
            var (p, h) = running[0];
            string model = h.TryGetProperty("model", out var m) ? m.GetString() : "?";
            string last = opt.Model ?? ReadState("last_model");
            string want = last != null ? Path.GetFileNameWithoutExtension(last) : Tr.T("un altro modello", "another model");
            var attach = new TaskDialogButton(Tr.T("Collegati al server in esecuzione", "Connect to the running server"));
            var change = new TaskDialogButton(Tr.T($"Cambia modello: chiudi il server attuale e scegli «{want}» nel Loader", $"Change model: close the running server and choose “{want}” in the Loader"));
            var page = new TaskDialogPage
            {
                Caption = Program.AppName, Heading = Tr.T($"{Program.AppName} è già acceso sulla porta {p}", $"{Program.AppName} is already running on port {p}"),
                Text = Tr.T($"Modello in uso: {model}. Puoi usare questo server, oppure chiuderlo (solo lui e i suoi processi) e caricare un altro modello dal Loader.", $"Model in use: {model}. You can use this server, or close it (only it and its processes) and load another model from the Loader."),
                Buttons = { attach, change, TaskDialogButton.Cancel }, Icon = TaskDialogIcon.Information,
            };
            var choice = TaskDialog.ShowDialog(this, page);
            if (choice == attach) { pageView = "chat"; ShowServer(p, false); return; }
            if (choice != change) { exiting = true; Close(); return; }
            await StopExternal(p);
        }
        if (opt.TestFallback) { await Fallback(Tr.T("prova: il server web non parte (--test-fallback)", "test: the web server does not start (--test-fallback)")); return; }
        await StartWeb(true);
    }

    /// <summary>The port of the app's server: --port, else the config file's, else 7232.</summary>
    int WebPort()
    {
        if (opt.Port != 0) return opt.Port;
        try
        {
            if (JsonDocument.Parse(File.ReadAllText(cfgPath)).RootElement.TryGetProperty("server", out var s) &&
                s.TryGetProperty("port", out var p) && p.ValueKind == JsonValueKind.Number) return p.GetInt32();
        }
        catch { }
        return 7232;
    }

    /// <summary>Start the server in this app's job: in standby (the Loader first, no model loaded) or, after the Loader
    /// chose another model (exit code 75), loading the model now in the config file while the page already answers.</summary>
    async Task StartWeb(bool standby)
    {
        if (!File.Exists(cfgPath) && opt.ConfigFile == null)
        {
            // a new installation: a config file without a model; the Loader writes the model and the values into it
            try { File.WriteAllText(cfgPath, "{\n  \"server\": {\n    \"port\": 7232,\n    \"listen-first\": true\n  },\n  \"engine\": {}\n}\n"); }
            catch (Exception e) { await Fallback(Tr.T($"Non riesco a creare {cfgPath}: {e.Message}", $"Cannot create {cfgPath}: {e.Message}")); return; }
        }
        if (!File.Exists(cfgPath)) { await Fallback(Tr.T($"Manca il file di configurazione {cfgPath}.", $"The config file is missing: {cfgPath}.")); return; }
        int p = WebPort();
        var taken = Native.Listeners().FirstOrDefault(x => x.port == p);
        if (taken.port != 0)
        {
            string who = "?";
            try { who = Process.GetProcessById(taken.pid).ProcessName; } catch { }
            await Fallback(Tr.T($"La porta {p} è occupata da {who} (PID {taken.pid}): scegli un'altra porta qui sotto, oppure avvia l'app con --port N.", $"Port {p} is taken by {who} (PID {taken.pid}): choose another port below, or start the app with --port N."));
            return;
        }
        string py = Path.Combine(opt.Repo, ".venv", "Scripts", "python.exe");
        string exe = opt.TestMock ? py : Path.Combine(opt.Repo, "bin", "basalt.exe");
        if (!File.Exists(exe)) { await Fallback(Tr.T($"Manca {exe}: il motore non è costruito in questa cartella (vedi docs\\WINDOWS.md).", $"{exe} is missing: the engine is not built in this folder (see docs\\WINDOWS.md).")); return; }
        string mode = (standby ? " --standby" : " --listen-first") + " --relaunch-on-model-change";
        string cmd = opt.TestMock ? $"\"{py}\" -m serve.server --mock-engine --config \"{cfgPath}\" --port {p}{mode}"
                                  : $"\"{exe}\" serve --config \"{cfgPath}\" --port {p}{mode}";
        string logDir = Path.Combine(opt.Repo, "logs");
        Directory.CreateDirectory(logDir);
        string log = Path.Combine(logDir, opt.TestMock ? "basalt-app-test.log" : "basalt-serve.log");
        try { if (File.Exists(log)) File.Move(log, log + ".1", true); } catch { }
        foreach (var v in new[] { "CL", "_CL_" }) Environment.SetEnvironmentVariable(v, null);
        Environment.SetEnvironmentVariable("PYTHONUTF8", "1");
        Environment.SetEnvironmentVariable("PYTHONIOENCODING", "utf-8");
        Environment.SetEnvironmentVariable("VIRTUAL_ENV", Path.Combine(opt.Repo, ".venv"));
        try
        {
            int pid = BasaltLauncher.Start(exe, cmd, opt.Repo, log, true, true);
            ours = true;
            report.Add($"started pid {pid} on port {p} ({(standby ? "standby" : "loading")})");
            if (!opt.TestMock)
                File.WriteAllText(Path.Combine(opt.Repo, "basalt.pid"), JsonSerializer.Serialize(new
                {
                    pid, start = Process.GetProcessById(pid).StartTime.ToString("o"), mode = "app", port = p, log,
                }));
        }
        catch (Exception e) { await Fallback(Tr.T("Avvio del server non riuscito: ", "The server did not start: ") + e.Message); return; }
        pageView = standby ? "loader" : "stats";
        ShowServer(p, true, standby ? Tr.T($"Avvio di {Program.AppName}…", $"Starting {Program.AppName}…") : Tr.T($"{Program.AppName} riparte con il nuovo modello…", $"{Program.AppName} starts again with the new model…"));
    }

    /// <summary>The web server could not start: the native start panel, with the reason and the end of the log.</summary>
    async Task Fallback(string reason)
    {
        poll.Stop();
        web.Visible = false;
        start.Visible = true;
        string log = Path.Combine(opt.Repo, "logs", opt.TestMock ? "basalt-app-test.log" : "basalt-serve.log");
        string tail = "";
        try { using var s = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var r = new StreamReader(s); tail = string.Join("\n", r.ReadToEnd().Split('\n').TakeLast(10)); } catch { }
        fallbackReason = Tr.T("Il server web non è partito: ", "The web server did not start: ") + reason + (tail.Trim().Length > 0 ? Tr.T("\n\nUltime righe del log (", "\n\nThe log's last lines (") + log + "):\n" + tail.TrimEnd() : "");
        report.Add("fallback: " + reason);
        await FallbackPanel();
    }

    string fallbackReason;

    async Task FallbackPanel()
    {
        reasonLabel.Text = fallbackReason ?? "";
        reasonLabel.Visible = !string.IsNullOrEmpty(fallbackReason);
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
            MessageBox.Show(this, Tr.T($"Profilo sconosciuto: «{opt.Profile}».\nProfili: ", $"Unknown profile: “{opt.Profile}”.\nProfiles: ") + string.Join(", ", profileList.Select(x => x.GetProperty("name").GetString())),
                Program.AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        await Check();
        if (opt.TestMock && opt.Screenshot != null)
        {
            await Task.Delay(300);
            SnapForm(Path.ChangeExtension(opt.Screenshot, null) + "-fallback.png");
            File.WriteAllLines(Path.ChangeExtension(opt.Screenshot, ".txt"), report.Append("check: " + check.Text));
            if (opt.TestExit) { ExitCode = 0; exiting = true; Close(); }
        }
    }

    async Task LoadProfiles()
    {
        // the summaries (VRAM, tok/s) are for the model chosen above, not the config file's
        var args = new List<string> { "-m", "serve.profiles", "json", "--config", cfgPath };
        if (SelectedReady) { args.Add("--model"); args.Add(SelectedModelPath); }
        string keep = SelectedName;
        var (code, output) = await Py(args.ToArray());
        profileList.Clear();
        profiles.Items.Clear();
        if (code != 0) { check.Text = Tr.T("Non riesco a leggere i profili:\n", "Cannot read the profiles:\n") + output; return; }
        var root = JsonDocument.Parse(output.Trim().Split('\n').Last()).RootElement.Clone();
        string def = root.GetProperty("default").GetString();
        foreach (var p in root.GetProperty("load").EnumerateArray())
        {
            profileList.Add(p);
            var s = p.GetProperty("summary");
            string extra = (s.TryGetProperty("vram_gb", out var v) ? $" · VRAM ~{v.GetDouble():0.0} GB" : "") + (s.TryGetProperty("tok_s", out var t) ? $" · ~{t.GetInt32()} tok/s" : "");
            string name = p.GetProperty("name").GetString();
            profiles.Items.Add($"{name}{(p.GetProperty("default").GetBoolean() ? Tr.T(" (predefinito)", " (default)") : "")} — {s.GetProperty("text").GetString()}{extra}");
        }
        string want = keep ?? opt.Profile ?? def;
        int idx = profileList.FindIndex(x => x.GetProperty("name").GetString() == want);
        loadingProfiles = true;
        profiles.SelectedIndex = Math.Max(0, idx);
        loadingProfiles = false;
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
            check.Text = Tr.T("Questo modello va convertito prima di caricarlo (vedi sopra).", "This model must be converted before it can be loaded (see above).");
            return;
        }
        check.ForeColor = Fg2;
        check.Text = Tr.T("Controllo la memoria…", "Checking the memory…");
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
            0 => Tr.T("\n\nOK: si può avviare.", "\n\nOK: it can start."),
            4 => Tr.T("\n\nNon si avvia: il motore di Basalt gira solo su GPU NVIDIA Blackwell (RTX 50, RTX PRO 6000, B200).", "\n\nIt does not start: the Basalt engine runs only on NVIDIA Blackwell GPUs (RTX 50, RTX PRO 6000, B200)."),
            _ => Tr.T("\n\nNon si avvia: la memoria non basta senza usare il file di paging. Scegli un altro profilo, lascia libera meno VRAM o chiudi altri programmi.", "\n\nIt does not start: the memory is not enough without the page file. Choose another profile, leave less VRAM free or close other programs."),
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
            cmd = $"\"{exe}\" -m serve.server --mock-engine --config \"{cfgPath}\" --port {p} --relaunch-on-model-change";
        }
        else
        {
            var ap = await Py(ApplyArgs(name, cfgPath));
            if (ap.code != 0) { check.Text = ap.output; check.ForeColor = Bad; return; }
            exe = Path.Combine(opt.Repo, "bin", "basalt.exe");
            cmd = $"\"{exe}\" serve --config \"{cfgPath}\" --port {p} --relaunch-on-model-change";
        }
        pageView = "stats";
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
            check.Text = Tr.T("Avvio non riuscito: ", "The start failed: ") + e.Message; check.ForeColor = Bad;
            return;
        }
        ShowServer(p, true);
    }

    async Task StartHeadless()
    {
        string name = SelectedName;
        int p = (int)port.Value;
        go.Enabled = headless.Enabled = false;
        check.Text = Tr.T($"Avvio il solo server «{name}» sulla porta {p}…", $"Starting the server alone “{name}” on port {p}…");
        var psi = new ProcessStartInfo("powershell.exe") { WorkingDirectory = opt.Repo, UseShellExecute = false, CreateNoWindow = true };
        var args = new List<string> { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Avvia-Basalt.ps1"), "-server", "--lang", Tr.Code, "--config", name, "--port", p.ToString(),
                                      "--vram-libera", reserve.Value.ToString(System.Globalization.CultureInfo.InvariantCulture), "--file", cfgPath };
        if (SelectedReady) { args.Add("--model"); args.Add(SelectedModelPath); WriteState("last_model", SelectedModelPath); }
        foreach (var a in args) psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi);
        await proc.WaitForExitAsync();
        if (proc.ExitCode == 0)
        {
            MessageBox.Show(this, Tr.T($"Server avviato sulla porta {p} senza finestra.\nIndirizzo: http://127.0.0.1:{p}/\nPer fermarlo: Ferma-Basalt.ps1 --port {p}", $"Server started on port {p} without a window.\nAddress: http://127.0.0.1:{p}/\nTo stop it: Ferma-Basalt.ps1 --port {p}"), Program.AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
            exiting = true; Close();
        }
        else { check.Text = Tr.T("Il solo server non è partito: vedi logs\\basalt-serve.log.", "The server alone did not start: see logs\\basalt-serve.log."); check.ForeColor = Bad; go.Enabled = headless.Enabled = true; }
    }

    // ------------------------------------------------------------------ the web UI
    void ShowServer(int p, bool loading, string heading = null)
    {
        serverPort = p;
        start.Visible = false;
        web.Visible = true;
        _ = InitWeb(loading, heading ?? Tr.T($"Avvio di {Program.AppName}…", $"Starting {Program.AppName}…"));
    }

    bool useBrowser;                // WebView2 is missing: the page opens in the default browser

    /// <summary>The page's address: the view to open and, the first time, the command line's profile, model and VRAM
    /// to leave free for the Loader, and the wizard when this PC has no Machine Default yet.</summary>
    string PageUrl()
    {
        var q = new List<string>();
        if (firstPage)
        {
            if (opt.Profile != null) q.Add("profile=" + Uri.EscapeDataString(opt.Profile));
            if (opt.Model != null) q.Add("model=" + Uri.EscapeDataString(opt.Model));
            if (opt.VramLibera >= 0) q.Add("reserve=" + opt.VramLibera.ToString(System.Globalization.CultureInfo.InvariantCulture));
            if (pageView == "loader" && !opt.TestNoWizard && !HasMachineDefault()) q.Add("wizard=1");
            firstPage = false;
        }
        return $"http://127.0.0.1:{serverPort}/{(q.Count > 0 ? "?" + string.Join("&", q) : "")}#{pageView}";
    }

    bool HasMachineDefault()
    {
        if (Native.GetJson($"http://127.0.0.1:{serverPort}/api/profiles") is not JsonElement p) return true;
        return p.TryGetProperty("load", out var load) && load.EnumerateArray().Any(x => x.GetProperty("name").GetString() == "Machine Default");
    }

    /// <summary>The page asks for the folder dialog: {"type": "pickFolder", "id": ..., "path": ...}; the answer is
    /// {"type": "folder", "id": ..., "path": ...} (no path when cancelled).</summary>
    void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            var m = JsonDocument.Parse(e.WebMessageAsJson).RootElement;
            if (m.GetProperty("type").GetString() != "pickFolder") return;
            string id = m.TryGetProperty("id", out var i) ? i.GetString() : "";
            string start = m.TryGetProperty("path", out var sp) && sp.ValueKind == JsonValueKind.String ? sp.GetString() : null;
            using var dlg = new FolderBrowserDialog { Description = Tr.T("Cartella dei modelli", "Models folder"), UseDescriptionForTitle = true, ShowNewFolderButton = false };
            if (!string.IsNullOrEmpty(start) && Directory.Exists(start)) dlg.InitialDirectory = start;
            string path = dlg.ShowDialog(this) == DialogResult.OK ? dlg.SelectedPath : null;
            web.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new { type = "folder", id, path }));
        }
        catch (Exception ex) { report.Add("web message: " + ex.Message); }
    }

    async Task InitWeb(bool loading, string heading)
    {
        if (web.CoreWebView2 == null && !useBrowser)
        {
            try
            {
                if (opt.TestNoWebView) throw new WebView2RuntimeNotFoundException("test");
                string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Basalt", opt.TestMock ? "WebView2-test" : "WebView2");
                // the page speaks the user's language (Tr.Code: --lang, BASALT_LANG, the Settings choice, Windows')
                var env = await CoreWebView2Environment.CreateAsync(null, data, new CoreWebView2EnvironmentOptions { Language = Tr.Code == "it" ? "it-IT" : "en-US" });
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2.Settings.AreDevToolsEnabled = opt.TestMock;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.DocumentTitleChanged += (_, _) => Text = Program.AppName;
                web.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true }); };
                web.CoreWebView2.WebMessageReceived += OnWebMessage;
            }
            catch (Exception e) when (e is WebView2RuntimeNotFoundException || e is COMException || e is FileNotFoundException)
            {
                useBrowser = true;
                report.Add("webview2 missing: " + e.Message);
                const string get = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
                var answer = opt.TestMock ? DialogResult.Yes : MessageBox.Show(this,
                    Tr.T($"Manca Microsoft Edge WebView2 Runtime, che serve per mostrare {Program.AppName} in questa finestra.\n\nSì: apro {Program.AppName} nel browser (il server resta acceso dietro l'icona vicino all'orologio).\nNo: apro la pagina per installare WebView2 (poi riavvia {Program.AppName}).", $"Microsoft Edge WebView2 Runtime is missing; it shows {Program.AppName} in this window.\n\nYes: {Program.AppName} opens in the browser (the server keeps running behind the icon near the clock).\nNo: the page to install WebView2 opens (then start {Program.AppName} again)."),
                    Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (answer == DialogResult.No) Process.Start(new ProcessStartInfo(get) { UseShellExecute = true });
                web.Visible = false;
                start.Visible = true;
                reasonLabel.Visible = false;
                check.ForeColor = Fg2;
                check.Text = Tr.T($"{Program.AppName} si apre nel browser: http://127.0.0.1:{serverPort}/ (WebView2 non è installato).", $"{Program.AppName} opens in the browser: http://127.0.0.1:{serverPort}/ (WebView2 is not installed).");
            }
        }
        if (useBrowser)
        {
            waitingHealth = loading;
            if (loading || ours) poll.Start();
            if (!loading) OpenBrowser();
            return;
        }
        if (loading)
        {
            web.CoreWebView2.NavigateToString($"<html lang='{Tr.Code}'><body style='background:#0E1114;color:#E9ECEE;font:18px Segoe UI;display:flex;align-items:center;justify-content:center;height:90vh;margin:0 16px'>" +
                $"<div><b>{System.Net.WebUtility.HtmlEncode(heading)}</b><p style='color:#9AA4AD'>" + Tr.T($"Porta {serverPort}. La pagina si apre appena il server risponde.", $"Port {serverPort}. The page opens as soon as the server answers.") + "</p></div></body></html>");
            waitingHealth = true;
            poll.Start();
        }
        else
        {
            web.CoreWebView2.Navigate(PageUrl());
            if (ours) poll.Start();
        }
    }

    /// <summary>Every half second while this app's server runs: open the page once /health answers; start the server
    /// again with the new model after exit code 75; the native panel with the reason after any other end.</summary>
    void WatchServer()
    {
        long code = ours ? BasaltLauncher.ExitCode() : -1;
        if (ours && code == RelaunchExitCode && !exiting)
        {
            poll.Stop();
            report.Add("exit 75: the server starts again with the new model");
            ours = false;
            _ = StartWeb(false);
            return;
        }
        if (ours && code >= 0 && !exiting)
        {
            poll.Stop();
            ours = false;
            try { File.Delete(Path.Combine(opt.Repo, "basalt.pid")); } catch { }
            _ = Fallback(Tr.T($"il server si è fermato (codice {code}).", $"the server stopped (code {code})."));
            return;
        }
        if (!waitingHealth) return;
        if (Native.GetJson($"http://127.0.0.1:{serverPort}/health") is JsonElement)
        {
            waitingHealth = false;
            report.Add("health ok");
            if (!ours) poll.Stop();
            if (useBrowser) { OpenBrowser(); return; }
            web.CoreWebView2.Navigate(PageUrl());
            if (opt.Screenshot != null) _ = TestRun();
        }
    }

    /// <summary>Tray: start the server alone for agents (Avvia-Basalt.ps1 -server, detached) in place of this app's,
    /// with the default profile, then close the app.</summary>
    async Task HeadlessFromTray()
    {
        int p = serverPort != 0 ? serverPort : WebPort();
        if (MessageBox.Show(this, Tr.T($"Avviare il solo server sulla porta {p}, senza finestra né icona, con il profilo predefinito?\n\nIl server di questa app si chiude e il modello si carica di nuovo dal server per gli agenti. Per fermarlo: Ferma-Basalt.ps1 --port {p}", $"Start the server alone on port {p}, without window or icon, with the default profile?\n\nThis app's server closes and the model loads again in the server for agents. To stop it: Ferma-Basalt.ps1 --port {p}"),
                Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        if (ours)
        {
            Native.UnloadServer(p);
            exiting = true;                                // this app's server ends on purpose
            BasaltLauncher.KillAll();
            BasaltLauncher.Wait(15000);
            ours = false;
            try { File.Delete(Path.Combine(opt.Repo, "basalt.pid")); } catch { }
        }
        var psi = new ProcessStartInfo("powershell.exe") { WorkingDirectory = opt.Repo, UseShellExecute = false, CreateNoWindow = true };
        foreach (var a in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Avvia-Basalt.ps1"), "-server", "--lang", Tr.Code, "--port", p.ToString(), "--file", cfgPath })
            psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi);
        await proc.WaitForExitAsync();
        MessageBox.Show(proc.ExitCode == 0 ? Tr.T($"Server avviato sulla porta {p} senza finestra.\nIndirizzo: http://127.0.0.1:{p}/\nPer fermarlo: Ferma-Basalt.ps1 --port {p}", $"Server started on port {p} without a window.\nAddress: http://127.0.0.1:{p}/\nTo stop it: Ferma-Basalt.ps1 --port {p}")
                                           : Tr.T("Il solo server non è partito: vedi logs\\basalt-serve.log.", "The server alone did not start: see logs\\basalt-serve.log."), Program.AppName, MessageBoxButtons.OK,
                        proc.ExitCode == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Error);
        exiting = true;
        Close();
    }

    void OpenBrowser()
    {
        string url = PageUrl();
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
                tray.ShowBalloonTip(6000, Program.AppName, Tr.T($"{Program.AppName} è ancora acceso: spegnilo dall'icona.", $"{Program.AppName} is still running: turn it off from the icon."), ToolTipIcon.Info);
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
        foreach (var a in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(opt.Repo, "Ferma-Basalt.ps1"), "--lang", Tr.Code, "--port", p.ToString() })
            psi.ArgumentList.Add(a);
        using var proc = Process.Start(psi);
        await proc.WaitForExitAsync();
    }

    /// <summary>Stop the server (the model unloads first, then the job ends every process), say how much RAM came
    /// back, and exit.</summary>
    void TurnOff(bool ask)
    {
        if (ask && MessageBox.Show(this, Tr.T($"Spegnere {Program.AppName} (server e motore)?", $"Turn off {Program.AppName} (server and engine)?"), Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        exiting = true;                                    // the server's end is wanted: no restart, no fallback panel
        poll.Stop();
        Cursor = Cursors.WaitCursor;
        double before = Native.AvailableRamGiB();
        if (serverPort != 0)
        {
            Native.UnloadServer(serverPort);
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
        string text = down ? Tr.T($"{Program.AppName} è spento. RAM disponibile: {after:0.0} GiB (liberati {Math.Max(0, after - before):0.0} GiB).", $"{Program.AppName} is off. RAM available: {after:0.0} GiB ({Math.Max(0, after - before):0.0} GiB freed).")
                           : Tr.T("Il server risponde ancora: fermalo con Ferma-Basalt.ps1.", "The server still answers: stop it with Ferma-Basalt.ps1.");
        if (opt.Screenshot != null)
            File.WriteAllLines(Path.ChangeExtension(opt.Screenshot, ".txt"), report);
        else
            MessageBox.Show(text, Program.AppName, MessageBoxButtons.OK, down ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        ExitCode = down ? 0 : 1;
        exiting = true;
        Close();
    }

    void PrepareMock()
    {
        // the mock server: a scratch config, demo memory readings, nothing of the real model touched
        string dir = Path.Combine(Path.GetTempPath(), "basalt-desktop-test");
        Directory.CreateDirectory(dir);
        if (opt.ConfigFile == null)
        {
            cfgPath = Path.Combine(dir, "mock_config.json");
            File.WriteAllText(cfgPath, "{\"server\": {\"port\": 7288, \"vision\": \"dynamic\", \"listen-first\": true}}");
        }
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("BASALT_CACHE")))
            Environment.SetEnvironmentVariable("BASALT_CACHE", Path.Combine(dir, "cache"));
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("BASALT_DATA")))
            Environment.SetEnvironmentVariable("BASALT_DATA", Path.Combine(dir, "data"));
        if (opt.TestFolderPrompt)                          // the folders are not set yet: the prompt comes first
            try { File.Delete(Path.Combine(Environment.GetEnvironmentVariable("BASALT_DATA"), "models.json")); File.Delete(statePath); } catch { }
        Environment.SetEnvironmentVariable("BASALT_FAKE_SYSTEM", "{\"ram_total\": 137438953472, \"ram_available\": 118111600640, \"commit_limit\": 154618822656, \"commit_used\": 21474836480, \"vram_total\": 34359738368, \"vram_free\": 32212254720}");
        Environment.SetEnvironmentVariable("BASALT_FAKE_GPU", "{\"name\": \"NVIDIA GeForce RTX 5090\", \"cc\": [12, 0], \"vram_total\": 34359738368, \"vram_free\": 32212254720, \"processes\": [{\"pid\": 11, \"name\": \"dwm.exe\"}]}");
        Environment.SetEnvironmentVariable("BASALT_FAKE_SYSTEM_OK", "1");
        port.Value = opt.Port != 0 ? opt.Port : WebPort();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { tray.Dispose(); poll.Dispose(); }
        base.Dispose(disposing);
    }
}
