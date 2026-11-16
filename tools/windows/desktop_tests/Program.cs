// Runs the checks of the server list (ServerList.cs) that need no window. Prints "ok" and returns 0, or the failures and 1. The keys are made up.
namespace TaDaMooDesktop;

static class Tr { public static string T(string it, string en) => en; }

static class Program
{
    public const string AppName = "test";

    static readonly List<string> failures = new();

    static void Same(string what, string want, string got) { if (want != got) failures.Add($"{what}: wanted [{want}], got [{got}]"); }

    static async Task<int> Main(string[] args)
    {
        string Norm(string a) => ServerAddress.Normalize(a, out _);
        Same("http name", "http://hrns.tadamoo.com", Norm("http://hrns.tadamoo.com"));
        Same("https name", "https://hrns.tadamoo.com", Norm("https://hrns.tadamoo.com"));
        Same("bare name: http and the program's port", "http://hrns.tadamoo.com:7232", Norm("hrns.tadamoo.com"));
        Same("bare name and port", "http://hrns.tadamoo.com:7232", Norm("hrns.tadamoo.com:7232"));
        Same("a trailing slash goes", "http://192.168.1.5:7232", Norm("http://192.168.1.5:7232/"));
        Same("an IP address", "http://100.84.1.2:7232", Norm("100.84.1.2"));
        Same("https and another port", "https://x.example.org:8443", Norm("https://x.example.org:8443"));
        Same("IPv6", "http://[fe80::1]:7000", Norm("http://[fe80::1]:7000"));
        Same("the default ports are left out", "http://x", Norm("http://x:80"));
        Same("the default https port is left out", "https://x", Norm("https://x:443"));
        foreach (var bad in new[] { "", "ftp://x", "http://x/path", "http://x?a=1", "http://u:p@x", "http://" })
            Same("refused: " + bad, "", Norm(bad));

        var path = Path.Combine(Path.GetTempPath(), "tadamoo-servers-test-" + Guid.NewGuid().ToString("N") + ".json");
        try
        {
            var store = new ServerStore(path);
            var entry = new ServerEntry { Name = "Linux", Address = "https://hrns.tadamoo.com", Key = "test-fake-key-1" };
            store.Remote.Add(entry);
            store.Last = "Linux";
            store.Save();
            var raw = File.ReadAllText(path);
            if (raw.Contains("test-fake-key-1")) failures.Add("the key is written in the clear");
            var again = new ServerStore(path);
            Same("the entry is read back", "Linux|https://hrns.tadamoo.com|test-fake-key-1|Linux", string.Join("|", again.Remote[0].Name, again.Remote[0].Address, again.Remote[0].Key, again.Last));
            again.SetKey(again.Remote[0], "test-fake-key-2");                              // the page changed the server's key
            Same("the new key is kept", "test-fake-key-2", new ServerStore(path).Remote[0].Key);
            Same("find by name", "Linux", again.Find("linux")?.Name ?? "");
            Same("the entry's base", "https://hrns.tadamoo.com", again.Remote[0].Base);
        }
        finally { try { File.Delete(path); } catch { } }

        if (args.Length > 0)                               // a running server with the key test-live-key-1: the connection test and the change of its key
        {
            var e = new ServerEntry { Name = "live", Address = args[0], Key = "test-live-key-1" };
            var (ok, text) = await ServerCheck.Test(e);
            if (!ok || !text.Contains("key is accepted")) failures.Add("test with the right key: " + ok + " " + text);
            (ok, text) = await ServerCheck.Test(new ServerEntry { Name = "x", Address = args[0], Key = "test-wrong-key" });
            if (ok || !text.Contains("key")) failures.Add("test with a wrong key: " + ok + " " + text);
            (ok, text) = await ServerCheck.Test(new ServerEntry { Name = "x", Address = "http://127.0.0.1:1", Key = "" });
            if (ok || !text.Contains("cannot be reached")) failures.Add("test of an address that does not answer: " + ok + " " + text);
            (ok, text) = await ServerCheck.ChangeKey(e, "test-wrong-key", "test-live-key-2");
            if (ok) failures.Add("the key was changed with a wrong current key");
            (ok, text) = await ServerCheck.ChangeKey(e, "test-live-key-1", "test-live-key-2");
            if (!ok) failures.Add("the key was not changed: " + text);
            (ok, text) = await ServerCheck.Test(new ServerEntry { Name = "x", Address = args[0], Key = "test-live-key-2" });
            if (!ok) failures.Add("the new key is not accepted: " + text);
        }

        if (failures.Count == 0) { Console.WriteLine("ok"); return 0; }
        foreach (var f in failures) Console.WriteLine("FAIL " + f);
        return 1;
    }
}
