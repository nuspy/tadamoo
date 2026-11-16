// The servers the desktop window can show: "this PC" (the server this app starts) and the remote ones the person adds (name, address, key), with a test of the connection
// and the key of each entry editable. The list is kept per user next to desktop.json (servers.json); a key is protected with the user's Windows account (DPAPI) when it can be.
// An address is http://name[:port] or https://name[:port], or just name[:port] (http).
using System.Net;
using System.Text.RegularExpressions;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace TaDaMooDesktop;

sealed class ServerEntry
{
    public string Name = "", Address = "", Key = "";
    public bool Local;

    /// <summary>The address as a base URL without a trailing slash; empty when the address is not usable.</summary>
    public string Base => ServerAddress.Normalize(Address, out _);
}

static class ServerAddress
{
    public const int DefaultPort = 7232;

    /// <summary>http://host[:port] or https://host[:port] (no path, query or user), or host[:port] taken as http; a name without a port is the default port of its scheme, a bare name
    /// without a port is TaDaMoo's own port. The result has no trailing slash. Empty with the reason in `error` when it is not an address.</summary>
    public static string Normalize(string text, out string error)
    {
        error = "";
        text = (text ?? "").Trim();
        if (text.Length == 0) { error = Tr.T("Scrivi l'indirizzo.", "Write the address."); return ""; }
        bool scheme = text.Contains("://");
        string full = scheme ? text : "http://" + text;
        if (!Uri.TryCreate(full, UriKind.Absolute, out var u) || (u.Scheme != "http" && u.Scheme != "https") || string.IsNullOrEmpty(u.Host))
        {
            error = Tr.T("L'indirizzo è http://nome[:porta], https://nome[:porta] oppure nome[:porta].", "The address is http://name[:port], https://name[:port] or name[:port].");
            return "";
        }
        if ((u.AbsolutePath != "/" && u.AbsolutePath != "") || u.Query.Length > 0 || u.Fragment.Length > 0 || u.UserInfo.Length > 0)
        {
            error = Tr.T("L'indirizzo è solo il nome (e la porta): niente percorso, chiave o altro.", "The address is only the name (and the port): no path, key or anything else.");
            return "";
        }
        bool explicitPort = Regex.IsMatch(text.Substring(scheme ? text.IndexOf("://") + 3 : 0), @"^(\[[0-9a-fA-F:.]+\]|[^/:?#]+):\d+");
        int port = explicitPort ? u.Port : (scheme ? (u.Scheme == "https" ? 443 : 80) : DefaultPort);
        string host = u.HostNameType == UriHostNameType.IPv6 ? "[" + u.Host.Trim('[', ']') + "]" : u.Host;
        bool plain = (u.Scheme == "http" && port == 80) || (u.Scheme == "https" && port == 443);
        return $"{u.Scheme}://{host}{(plain ? "" : ":" + port)}";
    }
}

sealed class ServerStore
{
    readonly string path;
    public readonly List<ServerEntry> Remote = new();
    public string Last = "";

    public ServerStore(string path) { this.path = path; Load(); }

    static string Protect(string key)
    {
        if (key.Length == 0) return "";
        try { return "dpapi:" + Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(key), null, DataProtectionScope.CurrentUser)); }
        catch { return key; }
    }

    static string Unprotect(string stored)
    {
        if (!stored.StartsWith("dpapi:")) return stored;
        try { return Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(stored[6..]), null, DataProtectionScope.CurrentUser)); }
        catch { return ""; }
    }

    void Load()
    {
        Remote.Clear();
        try
        {
            if (!File.Exists(path)) return;
            var root = JsonDocument.Parse(File.ReadAllText(path)).RootElement;
            if (root.TryGetProperty("last", out var l) && l.ValueKind == JsonValueKind.String) Last = l.GetString();
            if (root.TryGetProperty("servers", out var list) && list.ValueKind == JsonValueKind.Array)
                foreach (var s in list.EnumerateArray())
                    Remote.Add(new ServerEntry
                    {
                        Name = s.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                        Address = s.TryGetProperty("address", out var a) ? a.GetString() ?? "" : "",
                        Key = s.TryGetProperty("key", out var k) && k.ValueKind == JsonValueKind.String ? Unprotect(k.GetString()) : "",
                    });
        }
        catch { }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            var rows = Remote.Select(e => new { name = e.Name, address = e.Address, key = Protect(e.Key) }).ToList();
            File.WriteAllText(path, JsonSerializer.Serialize(new { last = Last, servers = rows }, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }

    public ServerEntry Find(string name) => Remote.FirstOrDefault(e => string.Equals(e.Name, name, StringComparison.OrdinalIgnoreCase));

    /// <summary>The new key of an entry (the person changed it, or the page did after changing the server's key).</summary>
    public void SetKey(ServerEntry entry, string key) { entry.Key = key; Save(); }
}

static class ServerCheck
{
    static HttpClient Client() => new() { Timeout = TimeSpan.FromSeconds(8) };

    static HttpRequestMessage Get(string url, string key)
    {
        var r = new HttpRequestMessage(HttpMethod.Get, url);
        if (key.Length > 0) r.Headers.TryAddWithoutValidation("Authorization", "Bearer " + key);
        return r;
    }

    static string Why(Exception e)
    {
        var inner = e;
        while (inner.InnerException != null) inner = inner.InnerException;
        return inner is TaskCanceledException ? Tr.T("nessuna risposta in 8 secondi", "no answer in 8 seconds") : inner.Message;
    }

    /// <summary>Does the address answer, is it a TaDaMoo server, does the Host rule let this name through, and is the key accepted. Returns (ok, the words for the person).</summary>
    public static async Task<(bool ok, string text)> Test(ServerEntry e)
    {
        string b = e.Base;
        if (b.Length == 0) { ServerAddress.Normalize(e.Address, out string why); return (false, why); }
        using var http = Client();
        string engine = "";
        try
        {
            using var h = await http.SendAsync(Get(b + "/health", ""));
            string body = await h.Content.ReadAsStringAsync();
            if ((int)h.StatusCode == 403 && body.Contains("host_not_allowed"))
                return (false, Tr.T("Il server risponde ma rifiuta questo nome (regola Host): aggiungilo ai nomi ammessi in Impostazioni > Rete, sul server.", "The server answers but refuses this name (Host rule): add it to the allowed host names in Settings > Network, on the server."));
            if (!h.IsSuccessStatusCode) return (false, Tr.T($"{b} risponde con l'errore {(int)h.StatusCode}.", $"{b} answers with error {(int)h.StatusCode}."));
            var j = JsonDocument.Parse(body).RootElement;
            if (!j.TryGetProperty("service", out var s) || s.GetString() != "tadamoo")
                return (false, Tr.T($"{b} risponde ma non è un server TaDaMoo.", $"{b} answers but it is not a TaDaMoo server."));
            bool keyed = j.TryGetProperty("api_key", out var k) && k.ValueKind == JsonValueKind.True;
            if (j.TryGetProperty("engine_installed", out var ei) && ei.ValueKind == JsonValueKind.False) engine = Tr.T(" Nessun motore installato su questo server.", " No engine is installed on this server.");
            using var a = await http.SendAsync(Get(b + "/api/server-key", e.Key));
            if ((int)a.StatusCode == 401)
                return (false, keyed ? Tr.T("Il server risponde ma la chiave manca o non è giusta.", "The server answers but the key is missing or wrong.") : Tr.T("Il server risponde ma ha rifiutato la richiesta (401).", "The server answers but refused the request (401)."));
            if (!a.IsSuccessStatusCode) return (false, Tr.T($"Il server risponde ({(int)a.StatusCode}) ma non accetta la richiesta.", $"The server answers ({(int)a.StatusCode}) but does not accept the request."));
            return (true, (keyed ? Tr.T("Il server risponde e la chiave è accettata.", "The server answers and the key is accepted.") : Tr.T("Il server risponde (non chiede una chiave).", "The server answers (it asks for no key).")) + engine);
        }
        catch (Exception ex) { return (false, Tr.T($"Non si raggiunge {b}: ", $"{b} cannot be reached: ") + Why(ex)); }
    }

    /// <summary>Changes the server's key (POST /api/server-key with the current key and the new one); the new key is in force on the server at once.</summary>
    public static async Task<(bool ok, string text)> ChangeKey(ServerEntry e, string current, string next)
    {
        string b = e.Base;
        if (b.Length == 0) return (false, Tr.T("L'indirizzo non è valido.", "The address is not valid."));
        try
        {
            using var http = Client();
            var req = new HttpRequestMessage(HttpMethod.Post, b + "/api/server-key") { Content = new StringContent(JsonSerializer.Serialize(new { current, @new = next }), Encoding.UTF8, "application/json") };
            if (current.Length > 0) req.Headers.TryAddWithoutValidation("Authorization", "Bearer " + current);
            using var r = await http.SendAsync(req);
            string body = await r.Content.ReadAsStringAsync();
            if (r.IsSuccessStatusCode) return (true, Tr.T("La chiave del server è cambiata e vale subito.", "The server's key was changed and is in force now."));
            string msg = "";
            try { msg = JsonDocument.Parse(body).RootElement.GetProperty("error").GetProperty("message").GetString(); } catch { }
            return (false, Tr.T("La chiave non è cambiata: ", "The key was not changed: ") + (msg.Length > 0 ? msg : "HTTP " + (int)r.StatusCode));
        }
        catch (Exception ex) { return (false, Tr.T("La chiave non è cambiata: ", "The key was not changed: ") + Why(ex)); }
    }
}

/// <summary>The list of servers: choose one to show, add, edit (the key too), remove, test the connection, change the key on the server.</summary>
sealed class ServerDialog : Form
{
    readonly ServerStore store;
    readonly bool withLocal;
    readonly ListView list = new() { View = View.Details, FullRowSelect = true, MultiSelect = false, HideSelection = false, Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11f), MinimumSize = new Size(560, 200) };
    readonly Label info = new() { AutoSize = false, Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10.5f), Padding = new Padding(0, 6, 0, 6) };
    readonly Button useBtn = Btn(Tr.T("Usa questo", "Use this"), true), addBtn = Btn(Tr.T("Aggiungi…", "Add…"), false), editBtn = Btn(Tr.T("Modifica…", "Edit…"), false), delBtn = Btn(Tr.T("Togli", "Remove"), false),
        testBtn = Btn(Tr.T("Prova", "Test"), false), keyBtn = Btn(Tr.T("Cambia la chiave sul server…", "Change the key on the server…"), false), closeBtn = Btn(Tr.T("Chiudi", "Close"), false);

    /// <summary>The entry the person chose to show (null: closed without choosing). The local entry has Local = true.</summary>
    public ServerEntry Chosen;

    static Button Btn(string text, bool primary) => new()
    {
        Text = text, AutoSize = true, MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System, Font = new Font("Segoe UI", 10.5f, primary ? FontStyle.Bold : FontStyle.Regular), Margin = new Padding(0, 0, 8, 0),
    };

    public ServerDialog(ServerStore store, bool withLocal, string heading)
    {
        this.store = store; this.withLocal = withLocal;
        Text = Program.AppName + " — " + Tr.T("server", "servers");
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(640, 440);
        Size = new Size(760, 500);
        list.Columns.Add(Tr.T("Nome", "Name"), 180);
        list.Columns.Add(Tr.T("Indirizzo", "Address"), 320);
        list.Columns.Add(Tr.T("Chiave", "Key"), 100);
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 1, RowCount = 4 };
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 74));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.Controls.Add(new Label { Text = heading, AutoSize = true, Font = new Font("Segoe UI", 11.5f, FontStyle.Bold), Margin = new Padding(0, 0, 0, 8) }, 0, 0);
        root.Controls.Add(list, 0, 1);
        root.Controls.Add(info, 0, 2);
        var bar = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = true };
        bar.Controls.AddRange(new Control[] { useBtn, addBtn, editBtn, delBtn, testBtn, keyBtn, closeBtn });
        root.Controls.Add(bar, 0, 3);
        Controls.Add(root);
        AcceptButton = useBtn; CancelButton = closeBtn;
        list.SelectedIndexChanged += (_, _) => Enable();
        list.DoubleClick += (_, _) => Use();
        useBtn.Click += (_, _) => Use();
        closeBtn.Click += (_, _) => Close();
        addBtn.Click += (_, _) => Edit(null);
        editBtn.Click += (_, _) => Edit(Selected());
        delBtn.Click += (_, _) => Remove();
        testBtn.Click += async (_, _) => await Test();
        keyBtn.Click += async (_, _) => await ChangeKey();
        Fill(store.Last);
        if (list.Items.Count == 0 || (!withLocal && store.Remote.Count == 0)) Shown += (_, _) => Edit(null);
    }

    ServerEntry Selected() => list.SelectedItems.Count == 1 ? (ServerEntry)list.SelectedItems[0].Tag : null;

    void Fill(string select)
    {
        list.Items.Clear();
        if (withLocal) list.Items.Add(new ListViewItem(new[] { Tr.T("Questo PC", "This PC"), Tr.T("il server di questa app", "the server this app starts"), "" }) { Tag = new ServerEntry { Name = Tr.T("Questo PC", "This PC"), Local = true } });
        foreach (var e in store.Remote)
            list.Items.Add(new ListViewItem(new[] { e.Name, e.Address, e.Key.Length > 0 ? "••••••" : "" }) { Tag = e });
        var pick = list.Items.Cast<ListViewItem>().FirstOrDefault(i => ((ServerEntry)i.Tag).Name == select) ?? (list.Items.Count > 0 ? list.Items[0] : null);
        if (pick != null) { pick.Selected = true; pick.Focused = true; }
        Enable();
    }

    void Enable()
    {
        var s = Selected();
        bool remote = s != null && !s.Local;
        useBtn.Enabled = s != null;
        editBtn.Enabled = delBtn.Enabled = keyBtn.Enabled = remote;
        testBtn.Enabled = remote;
    }

    void Use() { var s = Selected(); if (s != null) { Chosen = s; DialogResult = DialogResult.OK; Close(); } }

    void Remove()
    {
        var s = Selected();
        if (s == null || s.Local) return;
        if (MessageBox.Show(this, Tr.T($"Togliere «{s.Name}» dall'elenco?", $"Remove “{s.Name}” from the list?"), Program.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        store.Remote.Remove(s);
        if (store.Last == s.Name) store.Last = "";
        store.Save();
        Fill("");
    }

    async Task Test()
    {
        var s = Selected();
        if (s == null || s.Local) return;
        info.ForeColor = SystemColors.GrayText; info.Text = Tr.T("Provo…", "Testing…");
        testBtn.Enabled = false;
        var (ok, text) = await ServerCheck.Test(s);
        info.ForeColor = ok ? Color.FromArgb(40, 140, 70) : Color.FromArgb(200, 60, 80);
        info.Text = text;
        Enable();
    }

    async Task ChangeKey()
    {
        var s = Selected();
        if (s == null || s.Local) return;
        using var dlg = new KeyChangeDialog(s.Key);
        if (dlg.ShowDialog(this) != DialogResult.OK) return;
        var (ok, text) = await ServerCheck.ChangeKey(s, dlg.Current, dlg.New);
        info.ForeColor = ok ? Color.FromArgb(40, 140, 70) : Color.FromArgb(200, 60, 80);
        info.Text = text;
        if (ok) { store.SetKey(s, dlg.New); Fill(s.Name); }
    }

    void Edit(ServerEntry entry)
    {
        using var dlg = new ServerEditDialog(entry);
        while (dlg.ShowDialog(this) == DialogResult.OK)
        {
            string name = dlg.NameText.Trim();
            string address = ServerAddress.Normalize(dlg.AddressText, out string error);
            if (name.Length == 0) { MessageBox.Show(this, Tr.T("Scrivi un nome.", "Write a name."), Program.AppName); continue; }
            if (address.Length == 0) { MessageBox.Show(this, error, Program.AppName); continue; }
            var same = store.Find(name);
            if (same != null && !ReferenceEquals(same, entry)) { MessageBox.Show(this, Tr.T("C'è già un server con questo nome.", "A server with this name exists already."), Program.AppName); continue; }
            if (entry == null) { entry = new ServerEntry(); store.Remote.Add(entry); }
            if (store.Last == entry.Name) store.Last = name;
            entry.Name = name; entry.Address = address; entry.Key = dlg.KeyText;
            store.Save();
            Fill(name);
            return;
        }
    }
}

/// <summary>A server's name, address and key; the key can be shown, and the connection tested before the entry is kept.</summary>
sealed class ServerEditDialog : Form
{
    readonly TextBox name = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11f) }, address = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11f) },
        key = new() { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 11f), UseSystemPasswordChar = true };
    readonly CheckBox show = new() { Text = Tr.T("Mostra la chiave", "Show the key"), AutoSize = true, MinimumSize = new Size(0, 32) };
    readonly Label result = new() { AutoSize = false, Dock = DockStyle.Fill };
    public string NameText => name.Text;
    public string AddressText => address.Text;
    public string KeyText => key.Text.Trim();

    public ServerEditDialog(ServerEntry e)
    {
        Text = e == null ? Tr.T("Nuovo server", "New server") : Tr.T("Modifica il server", "Edit the server");
        StartPosition = FormStartPosition.CenterParent; FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = MinimizeBox = false;
        Size = new Size(560, 360);
        name.Text = e?.Name ?? ""; address.Text = e?.Address ?? ""; key.Text = e?.Key ?? "";
        var t = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 2, RowCount = 6 };
        t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 110)); t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        void Row(int r, string label, Control c) { t.Controls.Add(new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Font = new Font("Segoe UI", 10.5f) }, 0, r); t.Controls.Add(c, 1, r); t.RowStyles.Add(new RowStyle(SizeType.Absolute, 44)); }
        Row(0, Tr.T("Nome", "Name"), name);
        Row(1, Tr.T("Indirizzo", "Address"), address);
        Row(2, Tr.T("Chiave", "Key"), key);
        t.Controls.Add(show, 1, 3); t.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        t.Controls.Add(new Label { Text = Tr.T("Esempi: http://192.168.1.5:7232  ·  https://hrns.tadamoo.com  ·  mio-server:7232", "Examples: http://192.168.1.5:7232  ·  https://hrns.tadamoo.com  ·  my-server:7232"), AutoSize = true, ForeColor = SystemColors.GrayText }, 1, 4);
        t.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
        t.Controls.Add(result, 1, 5); t.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 64, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(16, 8, 16, 8) };
        var ok = new Button { Text = Tr.T("Salva", "Save"), DialogResult = DialogResult.OK, MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System };
        var cancel = new Button { Text = Tr.T("Annulla", "Cancel"), DialogResult = DialogResult.Cancel, MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System };
        var test = new Button { Text = Tr.T("Prova", "Test"), MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System };
        bar.Controls.AddRange(new Control[] { ok, cancel, test });
        Controls.Add(t); Controls.Add(bar);
        AcceptButton = ok; CancelButton = cancel;
        show.CheckedChanged += (_, _) => key.UseSystemPasswordChar = !show.Checked;
        test.Click += async (_, _) =>
        {
            result.ForeColor = SystemColors.GrayText; result.Text = Tr.T("Provo…", "Testing…");
            var (good, text) = await ServerCheck.Test(new ServerEntry { Name = name.Text, Address = address.Text, Key = KeyText });
            result.ForeColor = good ? Color.FromArgb(40, 140, 70) : Color.FromArgb(200, 60, 80);
            result.Text = text;
        };
    }
}

/// <summary>The current key, the new one and the new one again, for the key of the server.</summary>
sealed class KeyChangeDialog : Form
{
    readonly TextBox current = new() { Dock = DockStyle.Fill, UseSystemPasswordChar = true }, next = new() { Dock = DockStyle.Fill, UseSystemPasswordChar = true }, again = new() { Dock = DockStyle.Fill, UseSystemPasswordChar = true };
    public string Current => current.Text.Trim();
    public string New => next.Text.Trim();

    public KeyChangeDialog(string currentKey)
    {
        Text = Tr.T("Cambia la chiave del server", "Change the server's key");
        StartPosition = FormStartPosition.CenterParent; FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = MinimizeBox = false;
        Size = new Size(520, 320);
        current.Text = currentKey;
        var t = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 2, RowCount = 3 };
        t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150)); t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        void Row(int r, string label, Control c) { t.Controls.Add(new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left }, 0, r); t.Controls.Add(c, 1, r); t.RowStyles.Add(new RowStyle(SizeType.Absolute, 44)); }
        Row(0, Tr.T("Chiave attuale", "Current key"), current);
        Row(1, Tr.T("Chiave nuova", "New key"), next);
        Row(2, Tr.T("Chiave nuova, di nuovo", "New key again"), again);
        var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 64, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(16, 8, 16, 8) };
        var ok = new Button { Text = Tr.T("Cambia", "Change"), MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System };
        var cancel = new Button { Text = Tr.T("Annulla", "Cancel"), DialogResult = DialogResult.Cancel, MinimumSize = new Size(110, 44), FlatStyle = FlatStyle.System };
        bar.Controls.AddRange(new Control[] { ok, cancel });
        Controls.Add(t); Controls.Add(bar);
        AcceptButton = ok; CancelButton = cancel;
        ok.Click += (_, _) =>
        {
            if (New.Length == 0) { MessageBox.Show(this, Tr.T("Scrivi la chiave nuova.", "Write the new key."), Program.AppName); return; }
            if (New != again.Text.Trim()) { MessageBox.Show(this, Tr.T("Le due chiavi nuove non sono uguali.", "The two new keys are not the same."), Program.AppName); return; }
            DialogResult = DialogResult.OK;
        };
    }
}
