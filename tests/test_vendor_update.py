"""tools/vendor_update.py on a made-up source archive: the kept folders are replaced (a file upstream dropped goes), nothing outside them is kept or touched, a local patch is applied
again, a patch that no longer applies stops the update, and UPSTREAM.md says what was done."""
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import vendor_update as vu  # noqa: E402


def make_tarball(path: Path, files: dict) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo("vercel-labs-agent-desktop-abc1234/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def upstream(extra=None):
    files = {k: b"x" for k in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "clippy.toml", "deny.toml", "LICENSE", "README.md")}
    files.update({"crates/core/src/lib.rs": b"pub fn a() {}\n", "src/main.rs": b"fn main() {}\n", "skills/agent-desktop/SKILL.md": b"skill", "providers/macos/provider.sh": b"#!/bin/sh\n",
                  "docs/faq.md": b"not kept", "assets/big.png": b"not kept", "tests/x.rs": b"not kept", ".github/workflows/ci.yml": b"not kept"})
    files.update(extra or {})
    return files


def test_the_kept_folders_are_replaced_and_the_rest_is_left_out(tmp_path):
    dest = tmp_path / "third_party" / "agent-desktop"
    (dest / "src").mkdir(parents=True)
    (dest / "src" / "dropped.rs").write_text("old", encoding="utf-8")                     # a file upstream no longer has
    (dest / "NOTES.txt").write_text("ours", encoding="utf-8")                              # not a kept path: not touched
    tag, count, patches = vu.update("agent-desktop", "v9.9.9", make_tarball(tmp_path / "u.tgz", upstream()), "c" * 40, root=tmp_path)
    assert (tag, patches) == ("v9.9.9", []) and count == 11
    assert (dest / "src" / "main.rs").read_text(encoding="utf-8") == "fn main() {}\n" and not (dest / "src" / "dropped.rs").exists()
    assert (dest / "NOTES.txt").exists() and (dest / "LICENSE").exists() and (dest / "skills" / "agent-desktop" / "SKILL.md").exists()
    assert not (dest / "docs").exists() and not (dest / "assets").exists() and not (dest / "tests").exists() and not (dest / ".github").exists()
    text = (dest / "UPSTREAM.md").read_text(encoding="utf-8")
    assert "Release: v9.9.9" in text and "Commit: " + "c" * 40 in text and "vercel-labs/agent-desktop" in text and "none: the upstream code is used as it is" in text


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_local_patch_is_applied_again_and_one_that_no_longer_fits_stops_the_update(tmp_path):
    dest = tmp_path / "third_party" / "agent-desktop"
    patches = dest / "patches"
    patches.mkdir(parents=True)
    patch = ("diff --git a/src/main.rs b/src/main.rs\n--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1 @@\n-fn main() {}\n+fn main() { local(); }\n")
    (patches / "0001-local.patch").write_bytes(patch.encode("utf-8"))                      # bytes: a patch must keep its line ends
    tarball = make_tarball(tmp_path / "u.tgz", upstream())
    tag, count, applied = vu.update("agent-desktop", "v9.9.9", tarball, "c" * 40, root=tmp_path)
    assert applied == ["0001-local.patch"] and (dest / "src" / "main.rs").read_text(encoding="utf-8") == "fn main() { local(); }\n"
    assert "patches/0001-local.patch" in (dest / "UPSTREAM.md").read_text(encoding="utf-8")
    again = make_tarball(tmp_path / "v.tgz", upstream({"src/main.rs": b"fn main() { changed(); }\n"}))                 # upstream changed the line the patch touches
    with pytest.raises(SystemExit, match="0001-local.patch does not apply"):
        vu.update("agent-desktop", "v9.9.10", again, "d" * 40, root=tmp_path)
    assert (patches / "0001-local.patch").exists()                                                                  # the patch is kept for the person to update


def test_an_archive_that_lacks_a_kept_path_is_refused(tmp_path):
    files = upstream()
    del files["crates/core/src/lib.rs"]
    with pytest.raises(SystemExit, match="none of: crates"):
        vu.update("agent-desktop", "v1", make_tarball(tmp_path / "u.tgz", files), "c" * 40, root=tmp_path)


def test_both_projects_have_what_they_need_to_compile_listed():
    assert {"crates", "src", "skills", "Cargo.lock"} <= set(vu.VENDORED["agent-desktop"]["keep"])
    assert {"cli/src", "cli/build.rs", "cli/cdp-protocol", "cli/Cargo.lock"} <= set(vu.VENDORED["agent-browser"]["keep"])


def test_the_command_line_names_the_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vu, "ROOT", tmp_path)
    tarball = make_tarball(tmp_path / "u.tgz", upstream())
    monkeypatch.setattr(vu, "update", lambda name, tag, path, root=None: ("v1", 11, []))
    assert vu.main(["agent-desktop", "v1", "--from-tarball", str(tarball)]) == 0
    assert "agent-desktop v1: 11 files" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        vu.main(["nothing"])
