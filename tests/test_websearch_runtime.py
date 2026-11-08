"""The installers of the local web search servers: which system they are for, Node.js from the PC or from nodejs.org (checked), npm packages with install scripts
off, and a server that starts when needed and stops when idle. Nothing here downloads or starts a real program."""
import hashlib
import io
import json
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from tadamoo import websearch_runtime as rt


class Done:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def info(os_name="windows", arch="x64"):
    return {"os": os_name, "arch": arch, "supported": (os_name, arch) in rt.NODE_BUILDS, "note": ""}


def test_the_system_is_told_apart_by_itself(monkeypatch):
    for name, machine, want in (("Windows", "AMD64", ("windows", "x64")), ("Linux", "aarch64", ("linux", "arm64")), ("Darwin", "arm64", ("macos", "arm64")),
                                ("Darwin", "x86_64", ("macos", "x64")), ("Windows", "ARM64", ("windows", "arm64"))):
        monkeypatch.setattr(rt.platform, "system", lambda n=name: n)
        monkeypatch.setattr(rt.platform, "machine", lambda m=machine: m)
        monkeypatch.setattr(rt.glob, "glob", lambda pattern: [])
        got = rt.system()
        assert (got["os"], got["arch"]) == want and got["supported"] is True, got
    monkeypatch.setattr(rt.platform, "system", lambda: "FreeBSD")
    assert rt.system()["supported"] is False and "no official Node.js build" in rt.system()["note"]
    monkeypatch.setattr(rt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(rt.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(rt.glob, "glob", lambda pattern: ["/lib/ld-musl-x86_64.so.1"] if pattern == "/lib/ld-musl-*" else [])
    assert rt.system()["supported"] is False and "musl" in rt.system()["note"]


def test_the_browser_playwright_drives_is_the_one_already_on_the_system():
    assert rt.browser_channel("windows", exists=lambda p: p.endswith("msedge.exe")) == "msedge"
    assert rt.browser_channel("windows", exists=lambda p: p.endswith("chrome.exe")) == "chrome"
    assert rt.browser_channel("macos", exists=lambda p: p == "/Applications/Google Chrome.app") == "chrome"
    assert rt.browser_channel("linux", exists=lambda p: p == "/usr/bin/microsoft-edge") == "msedge"
    assert rt.browser_channel("linux", exists=lambda p: False) == "chromium"             # its own, which has to be downloaded


def test_a_recent_node_on_the_pc_is_used_and_nothing_is_downloaded(tmp_path):
    runtime = rt.Runtime(tmp_path, info(), run=lambda argv, t=0, env=None: Done(0, "v22.3.0\n"), which=lambda n: f"/usr/bin/{n}")
    found = runtime.find()
    assert found["source"] == "system" and found["version"] == "22.3.0" and found["npm"] == ["/usr/bin/npm"]
    assert runtime.steps() == []
    old = rt.Runtime(tmp_path, info(), run=lambda argv, t=0, env=None: Done(0, "v16.20.0\n"), which=lambda n: f"/usr/bin/{n}")
    assert old.find() is None                                                              # too old: the private copy is looked for, and there is none
    missing = rt.Runtime(tmp_path, info(), which=lambda n: None)
    assert missing.find() is None and len(missing.steps()) == 4


def archive_for(os_name, version, plat, ext):
    """A tiny archive laid out like the official one, with files where the runtime looks for them."""
    root = f"node-{version}-{plat}"
    files = ({f"{root}/node.exe": b"x", f"{root}/node_modules/npm/bin/npm-cli.js": b"y"} if os_name == "windows"
             else {f"{root}/bin/node": b"x", f"{root}/lib/node_modules/npm/bin/npm-cli.js": b"y"})
    buf = io.BytesIO()
    if ext == "zip":
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:xz" if ext == "tar.xz" else "w:gz") as t:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                t.addfile(member, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("os_name,arch,plat,ext", [("windows", "x64", "win-x64", "zip"), ("linux", "x64", "linux-x64", "tar.xz"), ("linux", "arm64", "linux-arm64", "tar.xz"),
                                                   ("macos", "arm64", "darwin-arm64", "tar.gz"), ("macos", "x64", "darwin-x64", "tar.gz")])
def test_node_is_downloaded_for_this_system_checked_and_unpacked(tmp_path, os_name, arch, plat, ext):
    version = "v22.9.0"
    blob = archive_for(os_name, version, plat, ext)
    name = f"node-{version}-{plat}.{ext}"
    asked = []

    def fetch(url):
        asked.append(url)
        if url.endswith("index.json"):
            return json.dumps([{"version": "v23.1.0", "lts": False}, {"version": version, "lts": "Jod"}]).encode()
        return f"{hashlib.sha256(blob).hexdigest()}  {name}\n{'0' * 64}  other-file.zip\n".encode()

    def download(url, target, say, timeout=0):
        asked.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        return hashlib.sha256(blob).hexdigest()
    runtime = rt.Runtime(tmp_path, info(os_name, arch), fetch=fetch, download=download, which=lambda n: None)
    lines = []
    for label, detail, run in runtime.steps():
        run(lines.append)
    assert any(u.endswith(name) for u in asked) and any("long-term release is v22.9.0" in l for l in lines)       # the LTS, not the newest
    found = runtime.find()
    assert found["source"] == "private" and found["version"] == version and Path(found["node"]).is_file() and Path(found["npm"][1]).is_file()
    assert not (tmp_path / "node" / "download" / name).exists()                           # the archive is not kept
    assert runtime.env(found)["PATH"].startswith(found["bin"])


def test_a_download_that_does_not_match_the_published_checksum_is_removed(tmp_path):
    blob = archive_for("windows", "v22.9.0", "win-x64", "zip")
    name = "node-v22.9.0-win-x64.zip"

    def fetch(url):
        return json.dumps([{"version": "v22.9.0", "lts": "Jod"}]).encode() if url.endswith("index.json") else f"{'a' * 64}  {name}\n".encode()

    def download(url, target, say, timeout=0):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        return hashlib.sha256(blob).hexdigest()
    runtime = rt.Runtime(tmp_path, info(), fetch=fetch, download=download, which=lambda n: None)
    steps = runtime.steps()
    steps[0][2](lambda t: None)
    with pytest.raises(rt.RuntimeProblem, match="checksum"):
        steps[1][2](lambda t: None)
    assert not (tmp_path / "node" / "download" / name).exists() and runtime.find() is None


def test_an_unsupported_system_says_so_instead_of_downloading(tmp_path):
    runtime = rt.Runtime(tmp_path, {"os": "linux", "arch": "x64", "supported": False, "note": "This Linux uses musl"}, which=lambda n: None)
    (label, detail, run), = runtime.steps()
    assert "cannot be installed" in label and "musl" in detail
    with pytest.raises(rt.RuntimeProblem):
        run(lambda t: None)


def test_an_archive_cannot_write_outside_its_folder(tmp_path):
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(rt.RuntimeProblem, match="outside"):
        rt.extract(bad_zip, tmp_path / "out")
    bad_tar = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as t:
        link = tarfile.TarInfo("pkg/escape")
        link.type, link.linkname = tarfile.SYMTYPE, "../../../etc"
        t.addfile(link)
    with pytest.raises(rt.RuntimeProblem, match="outside"):
        rt.extract(bad_tar, tmp_path / "out2")
    assert not (tmp_path / "evil.txt").exists()


def fake_found(tmp_path):
    return {"node": "node", "npm": ["npm"], "bin": str(tmp_path), "version": "22.0.0", "source": "system"}


def test_a_package_is_installed_with_scripts_off_and_started_from_the_installed_copy(tmp_path):
    seen = []

    def run(argv, timeout=0, env=None):
        seen.append(argv)
        prefix = Path(argv[argv.index("--prefix") + 1])
        pkg = prefix / "node_modules" / "open-websearch"
        (pkg / "build").mkdir(parents=True)
        (pkg / "build" / "index.js").write_text("//", encoding="utf-8")
        (pkg / "package.json").write_text(json.dumps({"name": "open-websearch", "version": "2.1.4", "bin": {"open-websearch": "build/index.js"}}), encoding="utf-8")
        return Done(0, "added 1 package")
    runtime = rt.Runtime(tmp_path, info(), which=lambda n: None)
    runtime.find = lambda: fake_found(tmp_path)
    packages = rt.Packages(tmp_path, runtime, run=run)
    assert packages.installed("open-websearch") == "" and packages.entry("open-websearch") is None
    with pytest.raises(rt.RuntimeProblem, match="not installed"):
        packages.command("open-websearch")
    lines = []
    for label, detail, step in packages.steps("open-websearch"):
        step(lines.append)
    argv = seen[0]
    assert argv[:2] == ["npm", "install"] and "--ignore-scripts" in argv and argv[-1] == "open-websearch" and "latest" not in " ".join(argv)
    assert packages.installed("open-websearch") == "2.1.4"
    cmd, env = packages.command("open-websearch", "--flag")
    assert cmd[0] == "node" and cmd[1].endswith("index.js") and cmd[2] == "--flag" and "PATH" in env
    packages.remove("open-websearch")
    assert packages.installed("open-websearch") == ""


def test_an_npm_failure_stops_the_install_with_its_reason(tmp_path):
    runtime = rt.Runtime(tmp_path, info(), which=lambda n: None)
    runtime.find = lambda: fake_found(tmp_path)
    packages = rt.Packages(tmp_path, runtime, run=lambda argv, timeout=0, env=None: Done(1, "", "E404 not found"))
    install = packages.steps("nothing-here")[0][2]
    lines = []
    with pytest.raises(rt.RuntimeProblem, match="exit 1"):
        install(lines.append)
    assert any("E404" in l for l in lines)


class FakeClient:
    made = 0

    def __init__(self):
        FakeClient.made += 1
        self.closed, self.calls, self.boom = False, [], False

    def call_tool(self, name, args, timeout=0):
        if self.boom:
            raise OSError("pipe broke")
        self.calls.append((name, args))
        return "ok"

    def close(self):
        self.closed = True


def test_a_server_starts_on_the_first_call_stops_when_idle_and_starts_again():
    FakeClient.made = 0
    clients = []

    def make(argv, env):
        client = FakeClient()
        clients.append(client)
        return client
    server = rt.ManagedServer("t", lambda: (["x"], {}), idle=0.15, make_client=make)
    assert not server.running
    assert server.call("search", {"query": "q"}) == "ok" and server.running
    server.call("search", {"query": "q2"})
    assert FakeClient.made == 1 and len(clients[0].calls) == 2                            # one process for the calls that follow each other
    time.sleep(0.5)
    assert not server.running and clients[0].closed                                       # nobody asked for a while: it is stopped
    server.call("search", {"query": "q3"})
    assert FakeClient.made == 2 and server.running
    server.stop()
    assert not server.running and clients[1].closed


def test_a_server_that_fails_is_stopped_and_a_missing_program_is_reported_as_it_is():
    client = FakeClient()
    server = rt.ManagedServer("t", lambda: (["x"], {}), idle=60, make_client=lambda a, e: client)
    server.call("search", {})
    client.boom = True
    with pytest.raises(OSError):
        server.call("search", {})
    assert not server.running and client.closed

    def not_installed():
        raise rt.RuntimeProblem("Node.js is not installed")
    with pytest.raises(rt.RuntimeProblem, match="not installed"):
        rt.ManagedServer("t2", not_installed, make_client=lambda a, e: client).call("search", {})
    started = rt.ManagedServer("t3", lambda: (["x"], {}), idle=60, make_client=lambda a, e: FakeClient())
    started.call("search", {})
    rt.stop_all()
    assert not started.running
