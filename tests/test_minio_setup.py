"""MinIO in the setup: finding one that is already here, the choices offered, the release shipped with TaDaMoo, our own copy as a process, and the settings it leaves in the archives."""
import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from tadamoo import minio_setup as ms
from tadamoo.rag import RagStore


def program(folder: Path, name: str = ms.NAMES[0]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"MZ" + b"0" * 100)
    return p


def test_a_minio_on_the_path_or_under_winget_is_found_once_and_a_dead_link_is_reported(tmp_path):
    a = program(tmp_path / "bin")
    b = program(tmp_path / "local" / "Microsoft" / "WinGet" / "Packages" / "MinIO.Server_x", ms.NAMES[1])
    env = {"PATH": str(tmp_path / "bin") + os.pathsep + str(tmp_path / "bin"), "LOCALAPPDATA": str(tmp_path / "local")}
    found = ms.detect(env, listening=lambda h, p: False)
    assert sorted(Path(x["path"]).name for x in found["programs"]) == sorted([a.name, b.name]) and found["listening"] == []
    links = tmp_path / "local" / "Microsoft" / "WinGet" / "Links"
    links.mkdir(parents=True)
    try:
        os.symlink(tmp_path / "gone" / ms.NAMES[0], links / ms.NAMES[0])
    except OSError:
        pytest.skip("this system does not allow a symbolic link")
    assert ms.detect(env, listening=lambda h, p: False)["broken"][0]["path"].endswith(ms.NAMES[0])


def test_an_address_that_answers_on_the_usual_port_counts_as_an_existing_one():
    found = ms.detect({"PATH": ""}, listening=lambda h, p: p == 9000)
    assert found["programs"] == [] and found["listening"] == ["127.0.0.1:9000"]


def test_with_one_present_the_person_is_offered_both_ways_and_nothing_is_recommended_with_none_install_is():
    here = ms.plan({"programs": [{"path": "x"}], "broken": [], "listening": []})
    assert [o["id"] for o in here["options"]] == ["use_existing", "install_separate", "skip"] and here["exists"] and here["recommended"] is None
    assert "left alone" in here["options"][1]["text"] and set(here["options"][0]["needs"]) == {"endpoint", "access_key", "secret_key"}
    none = ms.plan({"programs": [], "broken": [], "listening": []})
    assert [o["id"] for o in none["options"]] == ["install_separate", "skip"] and none["recommended"] == "install_separate"


def test_our_own_port_is_in_the_9400s_and_never_8080_nor_a_usual_minio_port():
    assert ms.free_port() in ms.OWN_PORTS and ms.free_port(listening=lambda h, p: p < 9410) == 9410
    assert ms.free_port(taken=[9400, 9401], listening=lambda h, p: False) == 9402
    with pytest.raises(RuntimeError):
        ms.free_port(listening=lambda h, p: True)
    with pytest.raises(ValueError):
        ms.MinioService(Path("x"), Path("d"), 8080, "a", "b")


def test_the_release_is_downloaded_checked_and_unpacked_and_a_wrong_hash_removes_it(tmp_path):
    exe = tmp_path / "pkg" / "silo.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ-the-service")
    pkg = tmp_path / "silo.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.write(exe, "silo_1/silo.exe")
        z.writestr("silo_1/LICENSE", "AGPL")
    release = {"url": "https://example.invalid/silo.zip", "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest()}
    fetch = lambda url, dest: dest.write_bytes(pkg.read_bytes())                                                  # noqa: E731
    got = ms.install_release(tmp_path / "own", release, fetch)
    assert got == tmp_path / "own" / "silo.exe" and got.read_bytes() == b"MZ-the-service" and not (tmp_path / "own" / "download.zip").exists()
    with pytest.raises(ValueError, match="SHA-256 differs"):
        ms.install_release(tmp_path / "own2", {**release, "sha256": "0" * 64}, fetch)
    assert not list((tmp_path / "own2").glob("*"))
    assert len(ms.RELEASE["sha256"]) == 64 and ms.RELEASE["url"].startswith("https://github.com/pgsty/silo/releases/download/")


def test_a_bundled_minio_is_found_without_any_network_address(tmp_path, monkeypatch):
    from serve import addons
    assert ms.bundled_program() is None
    bundled = tmp_path / "bundled-addons"
    exe = bundled / "minio" / ms.EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (bundled / "addons.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(addons, "addons_dir", lambda: bundled)
    assert ms.bundled_program() == exe


def test_a_program_the_person_points_at_is_copied_and_a_download_needs_its_hash(tmp_path):
    src = program(tmp_path / "theirs")
    got = ms.install_from(tmp_path / "own", program=src)
    assert got.read_bytes() == src.read_bytes() and got.parent == tmp_path / "own"
    with pytest.raises(ValueError, match="SHA-256"):
        ms.install_from(tmp_path / "own", url="https://example.invalid/x")
    with pytest.raises(ValueError):
        ms.install_from(tmp_path / "own")
    with pytest.raises(FileNotFoundError):
        ms.install_from(tmp_path / "own", program=tmp_path / "nope")


class Proc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


def test_our_own_copy_starts_on_this_pc_only_with_its_keys_in_the_environment_and_stops(tmp_path):
    exe = program(tmp_path / "own")
    seen = []
    keys = ms.new_keys()
    svc = ms.MinioService(exe, tmp_path / "data", 9402, keys["access_key"], keys["secret_key"], launcher=lambda cmd, env: (seen.append((cmd, env)), Proc())[1])
    assert svc.start() == "127.0.0.1:9402" and svc.running() and svc.start() == "127.0.0.1:9402" and len(seen) == 1
    cmd, env = seen[0]
    assert cmd[1] == "server" and "127.0.0.1:9402" in cmd and "127.0.0.1:9403" in cmd and env["MINIO_ROOT_USER"] == keys["access_key"] and env["MINIO_ROOT_PASSWORD"] == keys["secret_key"]
    svc.stop()
    assert not svc.running()
    with pytest.raises(FileNotFoundError):
        ms.MinioService(tmp_path / "none", tmp_path / "d", 9402, "a", "b", launcher=lambda c, e: Proc()).start()
    assert ms.new_keys()["secret_key"] != keys["secret_key"]


def test_provisioning_puts_the_secret_in_the_vault_and_only_the_rest_in_the_settings(tmp_path):
    store = RagStore(tmp_path / "rag")
    vault = {}
    ms.provision(store, vault.__setitem__, "127.0.0.1:9402", "tadamoo-ab12", "the-secret")
    cfg = store.settings.read()
    assert vault == {"images-s3": "the-secret"} and cfg["images_store"] == "s3" and cfg["images_s3_endpoint"] == "127.0.0.1:9402" and cfg["images_s3_vault"] == "images-s3"
    assert "the-secret" not in (tmp_path / "rag" / "settings.json").read_text(encoding="utf-8")
