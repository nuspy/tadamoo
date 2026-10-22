// BasaltSetup.exe: the step the installer runs once the files are copied. It makes the installed folder ready to use
// with nothing done by hand: finds a Python the packages support (or installs the bundled one), creates .venv, installs
// requirements.txt (from the bundled wheels, else from the internet), checks that the server imports, and sets the few
// write permissions the program needs inside its folder. It needs no Python and no .NET of its own: it is published
// self-contained next to BasaltWinGUI.exe, so it runs on a PC that has neither.
//
//   BasaltSetup.exe [--root DIR] [--lang it|en] [--no-ui] [--dry-run] [--keep-files]
//
// --root      the installed folder (default: found above this exe, the folder that holds Avvia-Basalt.ps1)
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

namespace BasaltSetup;

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
        else MessageBox.Show(message, Tr.T("Basalt Win+GUI - configurazione", "Basalt Win+GUI - setup"), MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}

sealed class Options
{
    public string Root, Lang, Error;
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
                case "--no-ui": o.NoUi = true; break;
                case "--dry-run": o.DryRun = true; break;
                case "--keep-files": o.KeepFiles = true; break;
                case "--test-no-admin-check": o.TestNoAdminCheck = true; break;
                default: o.Error = $"? {args[i]}\nBasaltSetup.exe [--root DIR] [--lang it|en] [--no-ui] [--dry-run] [--keep-files]"; break;
            }
        }
        if (o.Root == null) o.Root = FindRoot(AppContext.BaseDirectory);
        if (o.Error == null && (o.Root == null || !File.Exists(Path.Combine(o.Root, "Avvia-Basalt.ps1")) || !Directory.Exists(Path.Combine(o.Root, "serve"))))
            o.Error = Tr.T("Cartella di Basalt non trovata (serve Avvia-Basalt.ps1 e serve\\): ", "Basalt folder not found (Avvia-Basalt.ps1 and serve\\ are needed): ") + o.Root;
        return o;
    }

    static string FindRoot(string from)
    {
        for (var d = new DirectoryInfo(from); d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "Avvia-Basalt.ps1")) && Directory.Exists(Path.Combine(d.FullName, "serve")))
                return d.FullName;
        return null;
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
        Text = Tr.T("Basalt Win+GUI - preparazione", "Basalt Win+GUI - setup");
        Size = new Size(640, 420);
        MinimumSize = new Size(420, 320);
        StartPosition = FormStartPosition.CenterScreen;
        ControlBox = false;
        step.Text = Tr.T("Preparazione di Basalt…", "Preparing Basalt…");
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
        step.Text = ok ? Tr.T("Basalt è pronto.", "Basalt is ready.")
                       : Tr.T("Preparazione non riuscita. Il dettaglio è in ", "Setup did not finish. The details are in ") + logPath;
        if (ok) { var t = new System.Windows.Forms.Timer { Interval = 1500 }; t.Tick += (_, _) => Close(); t.Start(); }
        else { close.Visible = true; ControlBox = true; }
    }
}

/// <summary>What the setup does, in order. Run() logs every decision and returns whether Basalt is ready.</summary>
sealed class Installer
{
    // The Python versions the pinned packages (requirements.txt) have wheels for.
    const int MinMinor = 10, MaxMinor = 14;
    static readonly string[] ImportCheck = { "numpy", "jinja2", "markupsafe", "regex", "yaml", "tqdm", "PIL", "psutil" };

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
            log.Line($"Basalt Win+GUI setup{(o.DryRun ? " (dry run)" : "")} - {o.Root}");
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
            if (!File.Exists(Path.Combine(o.Root, f))) throw new Exception(Tr.T($"Manca {f} nella cartella di Basalt.", $"{f} is missing from the Basalt folder."));
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
        // For all users, no PATH change, no py launcher, nothing that Basalt does not use.
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
        // logs\, basalt.pid and basalt_config.json in it: logs\ is writable, and in the root a user can only create files.
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
