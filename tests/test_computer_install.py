"""Installing agent-desktop (tadamoo/computer/install.py) from a release served by a local test server: the archive for this system is picked, its SHA-256 is checked against the digest or
the release's checksums.txt, only plain files inside the folder are unpacked, an upgrade replaces the old files and keeps the program's state, and a bad download installs nothing."""
import hashlib
import http.server
import io
import json
import tarfile
import threading
from pathlib import Path

import pytest

from tadamoo.computer.backend import ComputerError, find_agent_desktop
from tadamoo.computer.install import expected_hash, install, installed, pick_asset, target_triple

TRIPLE = "x86_64-pc-windows-msvc"


def archive(files, extra_members=()):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra_members:
            tar.addfile(info)
    return out.getvalue()


class Release:
    """A release on a local server: /api/latest, /api/tags/<tag>, and /dl/<name>."""

    def __init__(self, tag="v9.9.9", files=None, digest=True, checksums=True, wrong=False, extra_members=()):
        self.tag = tag
        self.name = f"agent-desktop-{tag}-{TRIPLE}.tar.gz"
        self.blob = archive(files or {"agent-desktop.exe": b"MZ program " + tag.encode()}, extra_members)
        real = hashlib.sha256(self.blob).hexdigest()
        self.sha = "0" * 64 if wrong else real
        me = self
        self.hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                me.hits.append(self.path)
                base = f"http://127.0.0.1:{me.server.server_address[1]}"
                if self.path in ("/api/latest", f"/api/tags/{me.tag}"):
                    asset = {"name": me.name, "browser_download_url": f"{base}/dl/{me.name}", "size": len(me.blob)}
                    if digest:
                        asset["digest"] = "sha256:" + me.sha
                    assets = [asset, {"name": f"agent-desktop-ffi-{me.tag}-{TRIPLE}.zip", "browser_download_url": f"{base}/dl/ffi.zip"}]
                    if checksums:
                        assets.append({"name": "checksums.txt", "browser_download_url": f"{base}/dl/checksums.txt"})
                    body = json.dumps({"tag_name": me.tag, "assets": assets}).encode()
                elif self.path == "/dl/checksums.txt":
                    body = f"{me.sha}  {me.name}\n{'1' * 64}  other.tar.gz\n".encode()
                elif self.path == f"/dl/{me.name}":
                    body = me.blob
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def options(self):
        return {"api_url": self.base + "/api/latest", "allowed_prefix": self.base + "/dl/", "triple": TRIPLE}

    def close(self):
        self.server.shutdown()


@pytest.fixture
def release():
    made = []

    def make(**kw):
        r = Release(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def test_the_release_is_read_the_archive_for_this_system_picked_and_the_checksum_checked(tmp_path, release):
    r = release()
    done = install(tmp_path / "tools", **r.options())
    assert done["version"] == "9.9.9" and done["sha256"] == r.sha and done["sha256_verified"] is True and done["note"] == ""
    exe = tmp_path / "tools" / "agent-desktop.exe"
    assert exe.read_bytes() == b"MZ program v9.9.9" and done["path"] == str(exe)
    info = installed(tmp_path / "tools")
    assert info["found"] and info["where"] == "installed" and info["version"] == "9.9.9" and info["sha256_verified"]
    assert find_agent_desktop(tmp_path / "tools") == (str(exe), "installed")
    assert not [p for p in (tmp_path / "tools").iterdir() if p.name.startswith(".new-")]           # nothing left of the unpacking
    assert not any("ffi" in h for h in r.hits)                                                        # the C library is not downloaded


def test_the_checksum_may_come_from_the_releases_checksums_file(tmp_path, release):
    r = release(digest=False)
    assert install(tmp_path / "tools", **r.options())["sha256_verified"] is True
    assert "/dl/checksums.txt" in r.hits


def test_a_release_with_no_checksum_is_installed_with_that_said(tmp_path, release):
    r = release(digest=False, checksums=False)
    done = install(tmp_path / "tools", **r.options())
    assert done["sha256_verified"] is False and "no checksum" in done["note"]
    assert installed(tmp_path / "tools")["sha256_verified"] is False


def test_a_download_that_does_not_match_installs_nothing(tmp_path, release):
    r = release(wrong=True)
    with pytest.raises(ComputerError, match="does not match the checksum"):
        install(tmp_path / "tools", **r.options())
    assert not (tmp_path / "tools" / "agent-desktop.exe").exists() and not (tmp_path / "tools" / "install.json").exists()


def test_a_download_address_off_the_projects_releases_is_refused(tmp_path, release):
    r = release()
    options = {**r.options(), "allowed_prefix": "https://github.com/vercel-labs/agent-desktop/releases/download/"}
    with pytest.raises(ComputerError, match="not on the project's GitHub releases"):
        install(tmp_path / "tools", **options)


def test_only_plain_files_inside_the_folder_are_unpacked(tmp_path, release):
    link = tarfile.TarInfo("agent-desktop-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    r = release(extra_members=[link])
    with pytest.raises(ComputerError, match="not a plain file"):
        install(tmp_path / "tools", **r.options())
    r2 = release(files={"../escape.txt": b"x", "agent-desktop.exe": b"MZ"})
    with pytest.raises(ComputerError, match="not a plain file"):
        install(tmp_path / "tools", **r2.options())
    assert not (tmp_path / "escape.txt").exists()
    r3 = release(files={"readme.txt": b"no program here"})
    with pytest.raises(ComputerError, match="no agent-desktop program"):
        install(tmp_path / "tools", **r3.options())


def test_a_release_with_no_archive_for_this_system_says_so(tmp_path, release):
    r = release()
    with pytest.raises(ComputerError, match="no archive for aarch64-apple-darwin"):
        install(tmp_path / "tools", **{**r.options(), "triple": "aarch64-apple-darwin"})


def test_an_upgrade_replaces_the_old_files_and_keeps_the_programs_state(tmp_path, release):
    tools = tmp_path / "tools"
    install(tools, **release(tag="v9.9.8", files={"agent-desktop.exe": b"MZ old", "helper-old.dll": b"h"}).options())
    (tools / "state").mkdir()
    (tools / "state" / "keep.txt").write_text("snapshots", encoding="utf-8")
    install(tools, **release(tag="v9.9.9").options())
    assert (tools / "agent-desktop.exe").read_bytes() == b"MZ program v9.9.9" and not (tools / "helper-old.dll").exists()
    assert (tools / "state" / "keep.txt").read_text(encoding="utf-8") == "snapshots"
    assert installed(tools)["version"] == "9.9.9"


def test_the_download_has_a_size_limit(tmp_path, release, monkeypatch):
    r = release()
    monkeypatch.setattr("tadamoo.computer.install.MAX_BYTES", 10)
    with pytest.raises(ComputerError, match="is larger than"):
        install(tmp_path / "tools", **r.options())


def test_what_is_picked_and_checked_without_a_network():
    assert target_triple("win32", "AMD64") == "x86_64-pc-windows-msvc" and target_triple("win32", "ARM64") == "aarch64-pc-windows-msvc"
    assert target_triple("linux", "x86_64") == "x86_64-unknown-linux-gnu" and target_triple("linux", "aarch64") == "aarch64-unknown-linux-gnu"
    assert target_triple("darwin", "arm64") == "aarch64-apple-darwin"
    with pytest.raises(ComputerError):
        target_triple("linux", "riscv64")
    with pytest.raises(ComputerError):
        target_triple("freebsd", "x86_64")
    release = {"tag_name": "v1.3.1", "assets": [{"name": "agent-desktop-ffi-v1.3.1-x86_64-pc-windows-msvc.zip"}, {"name": "agent-desktop-v1.3.1-x86_64-pc-windows-msvc.tar.gz", "digest": "sha256:" + "AB" * 32}]}
    assert pick_asset(release, "x86_64-pc-windows-msvc")["name"].endswith(".tar.gz")
    assert expected_hash(release["assets"][1], None) == "ab" * 32
    assert expected_hash({"name": "x.tar.gz"}, f"{'c' * 64} *x.tar.gz\n{'d' * 64}  y.tar.gz") == "c" * 64
    assert expected_hash({"name": "z.tar.gz"}, f"{'c' * 64} *x.tar.gz") == ""
