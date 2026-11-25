// TaDaMooSetup.exe: the step the installer runs once the files are copied. It makes the installed folder ready to use
// with nothing done by hand: finds a Python the packages support (or installs the bundled one), creates .venv, installs
// requirements.txt (from the bundled wheels, else from the internet), checks that the server imports, and sets the few
// write permissions the program needs inside its folder. It needs no Python and no .NET of its own: it is published
// self-contained next to TaDaMoo.exe, so it runs on a PC that has neither.
//
//   TaDaMooSetup.exe [--root DIR] [--lang it|en] [--skills all|none|NAME,NAME] [--no-ui] [--dry-run] [--keep-files]
//
// --root      the installed folder (default: found above this exe, the folder that holds Avvia-TaDaMoo.ps1)
// --skills    the built-in skills to install (skills\builtin.json): all, none, or names; without it a page asks (with --no-ui: the ones the catalogue ticks)
// --dry-run   says what it would do and changes nothing (no window)
// --keep-files  keeps setup\ (the bundled Python installer and the wheels) after a successful run
// --test-no-admin-check  for tests only: runs without administrator rights
// Exit code: 0 ready, 1 failed (the reason is in setup.log in the installed folder), 2 wrong arguments or folder.
using System.Diagnostics;
using System.Globalization;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace TaDaMooSetup;

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        var o = Options.Parse(args);
        Tr.Code = o.Lang ?? (CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "it" ? "it" : "en");
        if (o.Error != null) { Fail(o, o.Error); return 2; }
        var log = new Log(Path.Combine(o.Root, "setup.log"), o.DryRun);
        try
        {
            if (o.NoUi || o.DryRun) return new Installer(o, log, _ => { }).Run() ? 0 : 1;
            ApplicationConfiguration.Initialize();
            if (o.Skills == null)
            {
                var catalogue = Catalogue.Load(o.Root);
                if (catalogue.Count > 0)
                {
                    using var ask = new SkillsForm(catalogue);
                    ask.ShowDialog();
                    o.Skills = ask.Chosen;
                }
            }
            if (o.Cli == null && Directory.Exists(Path.Combine(o.Root, "tools", "terminal", "PSLLMKit")))
                o.Cli = MessageBox.Show(Tr.T("Vuoi la riga di comando di TaDaMoo in PowerShell? Una riga che comincia con § va all'harness, e il comando tadamoo avvia un agente nella cartella corrente. Il setup aggiunge poche righe ai tuoi profili di PowerShell (WindowsPowerShell e PowerShell 7); si tolgono cancellando il blocco « TaDaMoo command line ». Aggiunge anche due profili con l'icona di TaDaMoo al menu di Windows Terminal e rende predefinito quello di PowerShell 7 (se non c'è, quello di Windows PowerShell; una copia di settings.json resta accanto, con ending .before-tadamoo.bak).",
                                             "Do you want the TaDaMoo command line in PowerShell? A line that starts with § goes to the harness, and the tadamoo command starts an agent in the current folder. The setup adds a few lines to your PowerShell profiles (WindowsPowerShell and PowerShell 7); remove the block « TaDaMoo command line » to take them away. It also adds two profiles with the TaDaMoo icon to the Windows Terminal menu and makes the PowerShell 7 one the default (the Windows PowerShell one when 7 is not installed; a copy of settings.json is kept next to it, ending .before-tadamoo.bak)."),
                                       "TaDaMoo Win+GUI", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes ? "yes" : "no";
            using var form = new ProgressForm();
            int code = 1;
            log.Sink = form.Add;
            form.Shown += async (_, _) =>
            {
                bool ok = await Task.Run(() => new Installer(o, log, form.SetStep).Run());
                code = ok ? 0 : 1;
                form.Finish(ok, log.Path);
            };
            Application.Run(form);
            return code;
        }
        finally { log.Dispose(); }
    }

    static void Fail(Options o, string message)
    {
        if (o.NoUi || o.DryRun) Console.Error.WriteLine(message);
        else MessageBox.Show(message, Tr.T("TaDaMoo Win+GUI - configurazione", "TaDaMoo Win+GUI - setup"), MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}

sealed class Options
{
    public string Root, Lang, Error, Skills, Cli;
    public bool NoUi, DryRun, KeepFiles, TestNoAdminCheck;

    public static Options Parse(string[] args)
    {
        var o = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i].ToLowerInvariant())
            {
                case "--root": o.Root = i + 1 < args.Length ? Path.GetFullPath(args[++i].TrimEnd('"')) : null; break;
                case "--lang":
                    o.Lang = i + 1 < args.Length ? args[++i].ToLowerInvariant() : null;
                    if (o.Lang != "it" && o.Lang != "en") o.Error = "--lang: it | en";
                    break;
                case "--skills": o.Skills = i + 1 < args.Length ? args[++i].Trim() : null; if (string.IsNullOrEmpty(o.Skills)) o.Error = "--skills: all | none | NAME,NAME"; break;
                case "--cli": o.Cli = i + 1 < args.Length ? args[++i].Trim().ToLowerInvariant() : null; if (o.Cli != "yes" && o.Cli != "no") o.Error = "--cli: yes | no"; break;
                case "--no-ui": o.NoUi = true; break;
                case "--dry-run": o.DryRun = true; break;
                case "--keep-files": o.KeepFiles = true; break;
                case "--test-no-admin-check": o.TestNoAdminCheck = true; break;
                default: o.Error = $"? {args[i]}\nTaDaMooSetup.exe [--root DIR] [--lang it|en] [--skills all|none|NAME,NAME] [--cli yes|no] [--no-ui] [--dry-run] [--keep-files]"; break;
            }
        }
        if (o.Root == null) o.Root = FindRoot(AppContext.BaseDirectory);
        if (o.Error == null && (o.Root == null || !File.Exists(Path.Combine(o.Root, "Avvia-TaDaMoo.ps1")) || !Directory.Exists(Path.Combine(o.Root, "serve"))))
            o.Error = Tr.T("Cartella di TaDaMoo non trovata (serve Avvia-TaDaMoo.ps1 e serve\\): ", "TaDaMoo folder not found (Avvia-TaDaMoo.ps1 and serve\\ are needed): ") + o.Root;
        return o;
    }

    static string FindRoot(string from)
    {
        for (var d = new DirectoryInfo(from); d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "Avvia-TaDaMoo.ps1")) && Directory.Exists(Path.Combine(d.FullName, "serve")))
                return d.FullName;
        return null;
    }
}

/// <summary>One built-in skill of skills\builtin.json.</summary>
sealed class BuiltinSkill
{
    public string Name, Title, Summary;
    public bool Default;
    public string[] Requires = Array.Empty<string>();
}

static class Catalogue
{
    /// <summary>The catalogue of the built-in skills in the installed folder, titles in the language of the setup; empty when there is none.</summary>
    public static List<BuiltinSkill> Load(string root)
    {
        var list = new List<BuiltinSkill>();
        try
        {
            string file = Path.Combine(root, "skills", "builtin.json");
            if (!File.Exists(file)) return list;
            using var doc = JsonDocument.Parse(File.ReadAllText(file, Encoding.UTF8));
            foreach (var e in doc.RootElement.GetProperty("skills").EnumerateArray())
            {
                string Text(string key) { var t = e.GetProperty(key); return (t.TryGetProperty(Tr.Code, out var v) ? v : t.GetProperty("en")).GetString(); }
                list.Add(new BuiltinSkill
                {
                    Name = e.GetProperty("name").GetString(), Title = Text("title"), Summary = Text("summary"),
                    Default = e.TryGetProperty("default", out var d) && d.GetBoolean(),
                    Requires = e.TryGetProperty("requires", out var r) ? r.EnumerateArray().Select(x => x.GetString()).ToArray() : Array.Empty<string>(),
                });
            }
        }
        catch (Exception) { list.Clear(); }
        return list;
    }
}

/// <summary>The page that asks which built-in skills to install: a ticked skill ticks what it requires, an unticked one unticks what requires it.</summary>
sealed class SkillsForm : Form
{
    readonly List<BuiltinSkill> skills;
    readonly ListView list = new() { Dock = DockStyle.Fill, CheckBoxes = true, View = View.Details, FullRowSelect = true, HeaderStyle = ColumnHeaderStyle.None, MultiSelect = false };
    readonly Label detail = new() { Dock = DockStyle.Bottom, Height = 64, Padding = new Padding(12, 6, 12, 6) };
    readonly Button ok = new() { Dock = DockStyle.Bottom, Height = 48 };
    bool busy;
    public string Chosen = "none";

    public SkillsForm(List<BuiltinSkill> skills)
    {
        this.skills = skills;
        Text = Tr.T("TaDaMoo Win+GUI - skill incluse", "TaDaMoo Win+GUI - built-in skills");
        Size = new Size(640, 480);
        MinimumSize = new Size(420, 360);
        StartPosition = FormStartPosition.CenterScreen;
        var head = new Label { Dock = DockStyle.Top, Height = 56, Padding = new Padding(12, 12, 12, 0), Font = new Font("Segoe UI", 10f),
            Text = Tr.T("Scegli le skill incluse da installare. I modelli che alcune richiedono non si scaricano qui: ogni skill spiega come installarli, e solo se mancano.",
                        "Choose the built-in skills to install. The models some of them need are not downloaded here: each skill explains how to install them, and only when they are missing.") };
        list.Columns.Add("", 580);
        foreach (var s in skills) list.Items.Add(new ListViewItem(s.Title) { Checked = s.Default, Tag = s });
        list.ItemChecked += OnChecked;
        list.SelectedIndexChanged += (_, _) => { if (list.SelectedItems.Count > 0) detail.Text = ((BuiltinSkill)list.SelectedItems[0].Tag).Summary; };
        ok.Click += (_, _) => { Finish(); Close(); };
        Controls.Add(list); Controls.Add(detail); Controls.Add(ok); Controls.Add(head);
        UpdateButton();
        FormClosing += (_, _) => { if (DialogResult == DialogResult.None) Finish(); };
    }

    void OnChecked(object sender, ItemCheckedEventArgs e)
    {
        if (busy) return;
        busy = true;
        var s = (BuiltinSkill)e.Item.Tag;
        foreach (ListViewItem item in list.Items)
        {
            var other = (BuiltinSkill)item.Tag;
            if (e.Item.Checked && s.Requires.Contains(other.Name)) item.Checked = true;
            if (!e.Item.Checked && other.Requires.Contains(s.Name)) item.Checked = false;
        }
        busy = false;
        UpdateButton();
    }

    void UpdateButton() { int n = list.CheckedItems.Count; ok.Text = n == 0 ? Tr.T("Non installare skill", "Install no skills") : Tr.T($"Installa {n} skill", $"Install {n} skills"); }

    void Finish()
    {
        var names = list.CheckedItems.Cast<ListViewItem>().Select(i => ((BuiltinSkill)i.Tag).Name).ToArray();
        Chosen = names.Length == 0 ? "none" : names.Length == skills.Count ? "all" : string.Join(",", names);
    }
}

static class Tr
{
    public static string Code = "en";
    public static string T(string it, string en) => Code == "it" ? it : en;
}

/// <summary>The log: a file in the installed folder, the window and the console.</summary>
sealed class Log : IDisposable
{
    public readonly string Path;
    public Action<string> Sink = _ => { };
    readonly StreamWriter file;
    readonly bool console;

    public Log(string path, bool dryRun)
    {
        Path = path;
        console = dryRun || Console.IsOutputRedirected;
        if (dryRun) return;
        try { file = new StreamWriter(new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)) { AutoFlush = true }; } catch { }
    }

    public void Line(string text)
    {
        string s = $"{DateTime.Now:HH:mm:ss} {text}";
        lock (this) { file?.WriteLine(s); }
        if (console) try { Console.WriteLine(s); } catch { }
        Sink(s);
    }

    public void Dispose() { file?.Dispose(); }
}

sealed class ProgressForm : Form
{
    readonly Label step = new() { Dock = DockStyle.Top, Height = 44, Padding = new Padding(12, 12, 12, 0), Font = new Font("Segoe UI", 11f, FontStyle.Bold) };
    readonly ProgressBar bar = new() { Dock = DockStyle.Top, Style = ProgressBarStyle.Marquee, Height = 10, MarqueeAnimationSpeed = 30 };
    readonly TextBox box = new() { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Font = new Font("Consolas", 9f) };
    readonly Button close = new() { Dock = DockStyle.Bottom, Height = 48, Text = Tr.T("Chiudi", "Close"), Visible = false };

    public ProgressForm()
    {
        Text = Tr.T("TaDaMoo Win+GUI - preparazione", "TaDaMoo Win+GUI - setup");
        Size = new Size(640, 420);
        MinimumSize = new Size(420, 320);
        StartPosition = FormStartPosition.CenterScreen;
        ControlBox = false;
        step.Text = Tr.T("Preparazione di TaDaMoo…", "Preparing TaDaMoo…");
        Controls.Add(box); Controls.Add(close); Controls.Add(bar); Controls.Add(step);
        close.Click += (_, _) => Close();
    }

    public void Add(string line) { if (IsHandleCreated) BeginInvoke(() => box.AppendText(line + "\r\n")); }
    public void SetStep(string text) { if (IsHandleCreated) BeginInvoke(() => step.Text = text); }

    public void Finish(bool ok, string logPath)
    {
        bar.Style = ProgressBarStyle.Continuous;
        bar.Value = ok ? 100 : 0;
        step.ForeColor = ok ? Color.FromArgb(0, 110, 40) : Color.FromArgb(180, 20, 20);
        step.Text = ok ? Tr.T("TaDaMoo è pronto.", "TaDaMoo is ready.")
                       : Tr.T("Preparazione non riuscita. Il dettaglio è in ", "Setup did not finish. The details are in ") + logPath;
        if (ok) { var t = new System.Windows.Forms.Timer { Interval = 1500 }; t.Tick += (_, _) => Close(); t.Start(); }
        else { close.Visible = true; ControlBox = true; }
    }
}

/// <summary>What the setup does, in order. Run() logs every decision and returns whether TaDaMoo is ready.</summary>
sealed class Installer
{
    // The Python versions the pinned packages (requirements.txt) have wheels for.
    const int MinMinor = 10, MaxMinor = 14;
    static readonly string[] ImportCheck = { "numpy", "jinja2", "markupsafe", "regex", "yaml", "tqdm", "PIL", "psutil", "cryptography" };

    readonly Options o;
    readonly Log log;
    readonly Action<string> step;
    string Venv => Path.Combine(o.Root, ".venv");
    string VenvPy => Path.Combine(Venv, "Scripts", "python.exe");
    string SetupDir => Path.Combine(o.Root, "setup");

    public Installer(Options o, Log log, Action<string> step) { this.o = o; this.log = log; this.step = step; }

    public bool Run()
    {
        try
        {
            log.Line($"TaDaMoo Win+GUI setup{(o.DryRun ? " (dry run)" : "")} - {o.Root}");
            Preflight();

            Step(Tr.T("Cerco Python…", "Looking for Python…"));
            var bundled = Bundled();
            var py = FindPython(bundled?.Minor);
            if (py == null)
            {
                log.Line(Tr.T("Nessun Python adatto (64 bit, 3.10-3.14, con venv e pip).", "No suitable Python (64-bit, 3.10-3.14, with venv and pip)."));
                if (bundled == null) throw new Exception(Tr.T("Python non c'è e il pacchetto non ne contiene uno (setup\\python).", "Python is missing and the package carries none (setup\\python)."));
                Step(Tr.T($"Installo Python {bundled.Version}…", $"Installing Python {bundled.Version}…"));
                if (!o.DryRun) InstallBundledPython(bundled);
                else log.Line($"would run {bundled.Installer} for all users, no PATH change");
                py = o.DryRun ? new PyInfo { Exe = "(Python " + bundled.Version + ")", Major = 3, Minor = bundled.Minor } : FindPython(bundled.Minor);
                if (py == null) throw new Exception(Tr.T("Python è stato installato ma non lo trovo.", "Python was installed but cannot be found."));
            }
            log.Line($"Python {py.Major}.{py.Minor}: {py.Exe}");

            Step(Tr.T("Creo l'ambiente virtuale…", "Creating the virtual environment…"));
            EnsureVenv(py);

            Step(Tr.T("Installo i pacchetti Python…", "Installing the Python packages…"));
            InstallPackages();

            Step(Tr.T("Controllo che il server parta…", "Checking that the server starts…"));
            Verify();

            Step(Tr.T("Installo le skill incluse…", "Installing the built-in skills…"));
            BuiltinSkills();

            Step(Tr.T("Riga di comando di PowerShell…", "PowerShell command line…"));
            CommandLine();

            Step(Tr.T("Imposto i permessi…", "Setting permissions…"));
            Permissions();

            Step(Tr.T("Ultimi passi…", "Final steps…"));
            Compile();
            Cleanup();
            log.Line(Tr.T("Pronto.", "Ready."));
            return true;
        }
        catch (Exception e)
        {
            log.Line("ERROR: " + e.Message);
            return false;
        }
    }

    void Step(string s) { log.Line("== " + s); step(s); }

    // ------------------------------------------------------------------ checks
    void Preflight()
    {
        if (!Environment.Is64BitOperatingSystem) throw new Exception(Tr.T("Serve Windows a 64 bit.", "A 64-bit Windows is required."));
        foreach (var f in new[] { "requirements.txt", "bin\\basalt.exe" })
            if (!File.Exists(Path.Combine(o.Root, f))) throw new Exception(Tr.T($"Manca {f} nella cartella di TaDaMoo.", $"{f} is missing from the TaDaMoo folder."));
        try
        {
            var drive = new DriveInfo(Path.GetPathRoot(o.Root));
            if (drive.AvailableFreeSpace < 1L << 30) throw new Exception(Tr.T("Meno di 1 GB liberi sul disco di installazione.", "Less than 1 GB free on the install drive."));
        }
        catch (ArgumentException) { }
        if (!o.DryRun && !o.TestNoAdminCheck && !IsAdmin()) throw new Exception(Tr.T("Servono i privilegi di amministratore (lo fa già l'installer).", "Administrator rights are needed (the installer already has them)."));
        // A warning only: the server tells the same on start, and a GPU can be added later.
        var (code, outp) = Capture("nvidia-smi.exe", "--query-gpu=name,compute_cap,driver_version --format=csv,noheader", 15000);
        if (code != 0) log.Line(Tr.T("Avviso: nvidia-smi non risponde: serve una GPU NVIDIA Blackwell (RTX 50) con il suo driver.", "Warning: nvidia-smi does not answer: an NVIDIA Blackwell (RTX 50) GPU with its driver is needed."));
        else
        {
            log.Line("GPU: " + outp.Trim().Replace("\r", "").Replace("\n", " | "));
            if (!outp.Contains("12.")) log.Line(Tr.T("Avviso: nessuna GPU con compute capability 12.x (Blackwell): il motore non partirà.", "Warning: no GPU with compute capability 12.x (Blackwell): the engine will not start."));
        }
    }

    static bool IsAdmin()
    {
        using var id = WindowsIdentity.GetCurrent();
        return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
    }

    // ------------------------------------------------------------------ Python
    sealed class PyInfo { public string Exe; public int Major, Minor; }

    sealed class BundledPython
    {
        public string Version, Installer, Sha256;
        public int Minor => int.Parse(Version.Split('.')[1]);
    }

    BundledPython Bundled()
    {
        string json = Path.Combine(SetupDir, "python", "bundled.json");
        if (!File.Exists(json)) return null;
        using var d = JsonDocument.Parse(File.ReadAllText(json));
        var r = d.RootElement;
        return new BundledPython { Version = r.GetProperty("version").GetString(), Installer = r.GetProperty("installer").GetString(), Sha256 = r.GetProperty("sha256").GetString() };
    }

    IEnumerable<string> Candidates()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var hive in new[] { RegistryHive.LocalMachine, RegistryHive.CurrentUser })
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, RegistryView.Registry64);
            using var core = baseKey.OpenSubKey(@"SOFTWARE\Python\PythonCore");
            if (core == null) continue;
            foreach (var ver in core.GetSubKeyNames())
            {
                if (ver.EndsWith("-32")) continue;
                using var ip = core.OpenSubKey(ver + @"\InstallPath");
                string exe = ip?.GetValue("ExecutablePath") as string;
                if (string.IsNullOrEmpty(exe) && ip?.GetValue("") is string dir) exe = Path.Combine(dir, "python.exe");
                if (!string.IsNullOrEmpty(exe) && File.Exists(exe) && seen.Add(exe)) yield return exe;
            }
        }
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries))
        {
            string exe;
            try { exe = Path.Combine(dir.Trim().Trim('"'), "python.exe"); } catch { continue; }
            if (exe.Contains(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase)) continue;   // the Store stub, not a Python
            if (File.Exists(exe) && seen.Add(exe)) yield return exe;
        }
    }

    PyInfo Probe(string exe)
    {
        var (code, outp) = Capture(exe, "-c \"import sys, venv, ensurepip; print(sys.version_info[0], sys.version_info[1], sys.maxsize > 2**32, sys.prefix == sys.base_prefix)\"", 30000);
        if (code != 0) { log.Line($"  skipped (no venv/pip or not runnable): {exe}"); return null; }
        var p = outp.Trim().Split(' ');
        if (p.Length != 4 || !int.TryParse(p[0], out int major) || !int.TryParse(p[1], out int minor)) return null;
        if (major != 3 || minor < MinMinor || minor > MaxMinor) { log.Line($"  skipped (Python {major}.{minor}): {exe}"); return null; }
        if (p[2] != "True") { log.Line($"  skipped (32-bit): {exe}"); return null; }
        if (p[3] != "True") { log.Line($"  skipped (a virtual environment): {exe}"); return null; }
        return new PyInfo { Exe = exe, Major = major, Minor = minor };
    }

    /// <summary>The Python of the bundled wheels' version first, then one outside a user profile, then the newest.</summary>
    PyInfo FindPython(int? preferredMinor)
    {
        var found = new List<PyInfo>();
        foreach (var exe in Candidates()) { var p = Probe(exe); if (p != null) found.Add(p); }
        return found.OrderByDescending(p => p.Minor == preferredMinor)
                    .ThenByDescending(p => !p.Exe.Contains(@"\Users\", StringComparison.OrdinalIgnoreCase))
                    .ThenByDescending(p => p.Minor).FirstOrDefault();
    }

    void InstallBundledPython(BundledPython b)
    {
        string exe = Path.Combine(SetupDir, "python", b.Installer);
        if (!File.Exists(exe)) throw new Exception($"{exe} ?");
        string have = Sha256(exe);
        if (!string.Equals(have, b.Sha256, StringComparison.OrdinalIgnoreCase)) throw new Exception(Tr.T("Il programma di installazione di Python non corrisponde all'impronta attesa.", "The Python installer does not match its expected hash."));
        // For all users, no PATH change, no py launcher, nothing that TaDaMoo does not use.
        int code = Run(exe, "/quiet InstallAllUsers=1 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_dev=0 Include_tcltk=0 Include_pip=1 Shortcuts=0 AssociateFiles=0 CompileAll=0", o.Root, 1800000);
        if (code != 0 && code != 3010) throw new Exception(Tr.T($"L'installazione di Python è terminata con il codice {code}.", $"The Python installation ended with code {code}."));
    }

    // ------------------------------------------------------------------ virtual environment and packages
    void EnsureVenv(PyInfo py)
    {
        if (File.Exists(VenvPy) && !o.DryRun)
        {
            var (c, _) = Capture(VenvPy, "-c \"import sys\"", 30000);
            if (c == 0) { log.Line(Tr.T(".venv esiste già e funziona: lo riuso.", ".venv exists and works: reused.")); return; }
            log.Line(Tr.T(".venv esiste ma non parte: lo ricreo.", ".venv exists but does not start: recreating it."));
            if (!Venv.EndsWith(".venv", StringComparison.OrdinalIgnoreCase)) throw new Exception("refusing to delete " + Venv);
            Directory.Delete(Venv, true);
        }
        if (o.DryRun) { log.Line($"would run: {py.Exe} -m venv \"{Venv}\""); return; }
        int code = Run(py.Exe, $"-m venv \"{Venv}\"", o.Root, 600000);
        if (code != 0 || !File.Exists(VenvPy)) throw new Exception(Tr.T($"Creazione di .venv non riuscita (codice {code}).", $"Creating .venv failed (code {code})."));
    }

    void InstallPackages()
    {
        string req = Path.Combine(o.Root, "requirements.txt");
        string wheels = Path.Combine(SetupDir, "wheels");
        string common = "-m pip install --disable-pip-version-check --no-warn-script-location --only-binary=:all:";
        if (o.DryRun) { log.Line($"would run: pip install -r requirements.txt (from {wheels} when it has them, else the internet)"); return; }
        if (Directory.Exists(wheels) && Directory.EnumerateFiles(wheels, "*.whl").Any())
        {
            log.Line(Tr.T("Pacchetti dalla cartella setup\\wheels (senza internet)…", "Packages from setup\\wheels (no internet)…"));
            if (Run(VenvPy, $"{common} --no-index --find-links \"{wheels}\" -r \"{req}\"", o.Root, 1800000) == 0) return;
            log.Line(Tr.T("Le ruote incluse non bastano per questo Python: provo da internet.", "The bundled wheels do not fit this Python: trying the internet."));
        }
        else log.Line(Tr.T("Pacchetti da internet…", "Packages from the internet…"));
        int code = Run(VenvPy, $"{common} -r \"{req}\"", o.Root, 1800000);
        if (code != 0) throw new Exception(Tr.T($"Installazione dei pacchetti non riuscita (codice {code}); serve internet.", $"Installing the packages failed (code {code}); the internet is needed."));
    }

    void Verify()
    {
        if (o.DryRun) { log.Line("would check: import " + string.Join(", ", ImportCheck) + "; import serve"); return; }
        var (c1, o1) = Capture(VenvPy, $"-c \"import {string.Join(", ", ImportCheck)}; print('ok')\"", 120000);
        if (c1 != 0) throw new Exception(Tr.T("I pacchetti Python non si caricano: ", "The Python packages do not load: ") + o1.Trim());
        var (c2, o2) = Capture(VenvPy, "-c \"import serve.server; print('ok')\"", 120000, o.Root);
        if (c2 != 0) throw new Exception(Tr.T("Il server non si carica: ", "The server does not load: ") + o2.Trim());
    }

    void BuiltinSkills()
    {
        // The chosen skills become copies in the user's data folder; a failure is told and does not stop the setup (they can be installed later with the same command).
        string choice = o.Skills ?? "";
        string what = choice == "all" ? "--all-builtin" : choice.Length == 0 ? "--defaults" : choice.Replace(',', ' ');
        if (choice.Length > 0 && choice != "none" && !choice.Replace(",", "").All(c => char.IsLetterOrDigit(c) || c == '-')) throw new Exception("--skills: all | none | NAME,NAME");
        if (choice == "none") { log.Line(Tr.T("Nessuna skill inclusa richiesta.", "No built-in skills asked for.")); return; }
        // Only the copies are made here; the models of a skill are downloaded later from Settings > Skills (or `models NAME`), where the size is shown first.
        if (o.DryRun) { log.Line($"would run: {VenvPy} -m tadamoo.skillmanager install {what} --no-models"); return; }
        int code = Run(VenvPy, "-m tadamoo.skillmanager install " + what + " --no-models", o.Root, 600000);
        if (code != 0) log.Line(Tr.T($"Avviso: le skill incluse non sono state installate (codice {code}): si possono installare dopo da Impostazioni > Skill o con 'python -m tadamoo.skillmanager install'.",
                                     $"Warning: the built-in skills were not installed (code {code}): they can be installed later from Settings > Skills or with 'python -m tadamoo.skillmanager install'."));
    }

    const string CliBegin = "# >>> TaDaMoo command line (setup)", CliEnd = "# <<< TaDaMoo command line";

    void CommandLine()
    {
        // Only when asked for: a block in the person's own PowerShell profiles that loads the module and switches the prefix on. The same block replaces itself at the next setup.
        if (o.Cli != "yes") { log.Line(Tr.T("Riga di comando non richiesta.", "Command line not asked for.")); return; }
        string module = Path.Combine(o.Root, "tools", "terminal", "PSLLMKit", "PSLLMKit.psd1");
        if (!File.Exists(module)) { log.Line(Tr.T("Avviso: il modulo della riga di comando non c'è nel pacchetto.", "Warning: the command line module is not in the package.")); return; }
        string docs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        string quoted = module.Replace("'", "''");
        string block = CliBegin + "\r\n" + "if (Test-Path -LiteralPath '" + quoted + "') { Import-Module '" + quoted + "' -ErrorAction SilentlyContinue; if (Get-Command Enable-TaDaMooPrefix -ErrorAction SilentlyContinue) { Enable-TaDaMooPrefix } }" + "\r\n" + CliEnd + "\r\n";
        foreach (var profile in new[] { Path.Combine(docs, "WindowsPowerShell", "profile.ps1"), Path.Combine(docs, "PowerShell", "profile.ps1") })
        {
            if (o.DryRun) { log.Line("would write the TaDaMoo block in " + profile); continue; }
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(profile));
                string text = File.Exists(profile) ? File.ReadAllText(profile) : "";
                int a = text.IndexOf(CliBegin, StringComparison.Ordinal), b = a < 0 ? -1 : text.IndexOf(CliEnd, a, StringComparison.Ordinal);
                if (a >= 0 && b > a) text = text.Remove(a, b + CliEnd.Length - a).TrimStart('\r', '\n');
                if (text.Length > 0 && !text.EndsWith("\n")) text += "\r\n";
                File.WriteAllText(profile, text + block, new UTF8Encoding(true));
                log.Line(Tr.T("Riga di comando scritta in ", "Command line written in ") + profile);
            }
            catch (Exception e) { log.Line(Tr.T("Avviso: non riesco a scrivere ", "Warning: cannot write ") + profile + ": " + e.Message); }
        }
        WindowsTerminalProfiles(module);
        log.Line(Tr.T("Se PowerShell 5.1 non carica il profilo, serve: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned (non lo cambio io).",
                      "If Windows PowerShell does not load the profile, it needs: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned (the setup does not change it)."));
    }

    void WindowsTerminalProfiles(string module)
    {
        // Two profiles in the menu of Windows Terminal (the dropdown and the right click on its icon), written as a fragment: its own file. The one for PowerShell 7 (Windows PowerShell when 7 is not installed) also becomes the
        // default profile (the icon of Windows Terminal opens it): the only line of the person's settings.json that is changed, after a copy of the file.
        string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "Microsoft", "Windows Terminal", "Fragments", "TaDaMoo");
        string file = Path.Combine(dir, "tadamoo.json");
        if (o.DryRun) { log.Line("would write the Windows Terminal profiles in " + file); return; }
        try
        {
            string run = "Import-Module '" + module.Replace("'", "''") + "'; Enable-TaDaMooPrefix";
            string icon = Path.Combine(o.Root, "tools", "terminal", "tadamoo-cli.ico");
            if (!File.Exists(icon)) icon = null;
            var profiles = new object[]
            {
                new { name = "TaDaMoo CLI (PowerShell 7)", guid = Pwsh7Guid, commandline = "pwsh.exe -NoExit -Command \"" + run + "\"", icon = icon ?? "ms-appx:///ProfileIcons/{574e775e-4f2a-5b96-ac1e-a2962a402336}.png", hidden = false },
                new { name = "TaDaMoo CLI (Windows PowerShell)", guid = WinPsGuid, commandline = "powershell.exe -NoExit -Command \"" + run + "\"", icon = icon ?? "ms-appx:///ProfileIcons/{61c54bbd-c2c6-5271-96e7-009a87ff44bf}.png", hidden = false },
            };
            Directory.CreateDirectory(dir);
            File.WriteAllText(file, JsonSerializer.Serialize(new { profiles }, new JsonSerializerOptions { WriteIndented = true }), new UTF8Encoding(false));
            log.Line(Tr.T("Profili di Windows Terminal scritti in ", "Windows Terminal profiles written in ") + file);
        }
        catch (Exception e) { log.Line(Tr.T("Avviso: non riesco a scrivere i profili di Windows Terminal: ", "Warning: cannot write the Windows Terminal profiles: ") + e.Message); return; }
        DefaultTerminalProfile(Pwsh7Installed() ? Pwsh7Guid : WinPsGuid);
    }

    static bool Pwsh7Installed()
    {
        // pwsh.exe on the PATH (the store version is an alias in WindowsApps), or in Program Files\PowerShell\7
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try { if (File.Exists(Path.Combine(dir.Trim('"'), "pwsh.exe"))) return true; } catch { }
        }
        return File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "PowerShell", "7", "pwsh.exe"));
    }

    const string Pwsh7Guid = "{3b0f7f5e-6d52-4f2c-9a4d-7c1e5a2b8d03}", WinPsGuid = "{3b0f7f5e-6d52-4f2c-9a4d-7c1e5a2b8d04}";

    void DefaultTerminalProfile(string guid)
    {
        // The settings.json of Windows Terminal (store, preview and unpackaged installs), which may carry comments: only the value of "defaultProfile" is replaced (or the key added), after a copy.
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        foreach (var file in new[]
        {
            Path.Combine(local, "Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe", "LocalState", "settings.json"),
            Path.Combine(local, "Packages", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe", "LocalState", "settings.json"),
            Path.Combine(local, "Microsoft", "Windows Terminal", "settings.json"),
        })
        {
            if (!File.Exists(file)) continue;
            if (o.DryRun) { log.Line("would make the TaDaMoo profile the default in " + file); continue; }
            try
            {
                string text = File.ReadAllText(file), back = file + ".before-tadamoo.bak";
                var key = new System.Text.RegularExpressions.Regex("(\"defaultProfile\"\\s*:\\s*\")[^\"]*(\")");
                string now = key.IsMatch(text) ? key.Replace(text, m => m.Groups[1].Value + guid + m.Groups[2].Value, 1)
                                               : System.Text.RegularExpressions.Regex.Replace(text, "^\\s*\\{", m => m.Value + "\n    \"defaultProfile\": \"" + guid + "\",", System.Text.RegularExpressions.RegexOptions.None);
                if (now == text) continue;
                if (!File.Exists(back)) File.Copy(file, back);
                File.WriteAllText(file, now, new UTF8Encoding(false));
                log.Line(Tr.T("Profilo predefinito di Windows Terminal: TaDaMoo CLI (copia in ", "Windows Terminal default profile: TaDaMoo CLI (copy in ") + back + ")");
            }
            catch (Exception e) { log.Line(Tr.T("Avviso: non riesco a cambiare il profilo predefinito in ", "Warning: cannot change the default profile in ") + file + ": " + e.Message); }
        }
    }

    void Compile()
    {
        // Precompiled here because the program folder is not writable afterwards: without it Python recompiles at every start.
        if (o.DryRun) return;
        int code = Run(VenvPy, "-m compileall -q -j 0 serve", o.Root, 600000);
        if (code != 0) log.Line($"compileall: code {code} (not fatal)");
    }

    // ------------------------------------------------------------------ permissions
    void Permissions()
    {
        // The installed folder is read-only for users (programs, scripts, libraries cannot be replaced). The program writes
        // logs\, tadamoo.pid and tadamoo_config.json in it: logs\ is writable, and in the root a user can only create files.
        string users = "*S-1-5-32-545";
        string logs = Path.Combine(o.Root, "logs");
        if (o.DryRun) { log.Line($"would create {logs} writable for Users, and let Users create files in {o.Root} (no change to existing ones)"); return; }
        Directory.CreateDirectory(logs);
        IcaclsGrant(logs, $"{users}:(OI)(CI)M");
        IcaclsGrant(o.Root, $"{users}:(WD,AD)");
    }

    void IcaclsGrant(string path, string grant)
    {
        int code = Run("icacls.exe", $"\"{path}\" /grant \"{grant}\"", o.Root, 120000);
        if (code != 0) throw new Exception(Tr.T($"Permessi su {path} non impostati (codice {code}).", $"Permissions on {path} were not set (code {code})."));
    }

    void Cleanup()
    {
        if (o.KeepFiles || o.DryRun || !Directory.Exists(SetupDir)) return;
        try { Directory.Delete(SetupDir, true); log.Line(Tr.T("Tolti i file temporanei dell'installazione (setup\\).", "Removed the installer's temporary files (setup\\).")); }
        catch (Exception e) { log.Line("setup\\ not removed: " + e.Message); }
    }

    // ------------------------------------------------------------------ processes
    static string Sha256(string path)
    {
        using var f = File.OpenRead(path);
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(f)).ToLowerInvariant();
    }

    static ProcessStartInfo Psi(string exe, string args, string cwd)
    {
        var psi = new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, WorkingDirectory = cwd ?? Environment.CurrentDirectory, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        foreach (var v in new[] { "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV" }) psi.Environment.Remove(v);
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        return psi;
    }

    /// <summary>Runs a program, writes each output line to the log, returns its exit code (-1: timed out).</summary>
    int Run(string exe, string args, string cwd, int timeoutMs)
    {
        log.Line($"> {Path.GetFileName(exe)} {args}");
        using var p = Process.Start(Psi(exe, args, cwd));
        p.OutputDataReceived += (_, e) => { if (e.Data != null) log.Line("  " + e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) log.Line("  " + e.Data); };
        p.BeginOutputReadLine(); p.BeginErrorReadLine();
        if (!p.WaitForExit(timeoutMs)) { try { p.Kill(true); } catch { } log.Line("  timed out"); return -1; }
        p.WaitForExit();
        return p.ExitCode;
    }

    /// <summary>Runs a program quietly and returns its exit code and output (a missing program is code -2).</summary>
    static (int, string) Capture(string exe, string args, int timeoutMs, string cwd = null)
    {
        try
        {
            using var p = Process.Start(Psi(exe, args, cwd));
            var err = p.StandardError.ReadToEndAsync();
            var outp = p.StandardOutput.ReadToEndAsync();
            if (!p.WaitForExit(timeoutMs)) { try { p.Kill(true); } catch { } return (-1, ""); }
            return (p.ExitCode, outp.Result + err.Result);
        }
        catch (Exception) { return (-2, ""); }
    }
}
