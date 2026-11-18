"""Where the images are kept, from the settings page: the folder moved from the GUI, a MinIO that is already here, and a separate one of our own installed on request."""
import hashlib
import io
import time
import zipfile
from pathlib import Path

import pytest

from serve.rag_images_api import RagImagesApi
from tadamoo import minio_setup
from tadamoo.rag import RagError, RagStore


class FakeS3:
    refuse = False

    def __init__(self, endpoint, bucket, access, secret, secure=False):
        self.args = (endpoint, bucket, access, secret, secure)

    def ensure_bucket(self):
        from tadamoo.rag.s3store import S3Error
        if FakeS3.refuse:
            raise S3Error("the image service refused the access key or secret", 403)


class Proc:
    alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


def package(tmp_path):
    pkg = tmp_path / "silo.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("silo/silo.exe" if minio_setup.NAMES[0].endswith(".exe") else "silo/silo", b"MZ-service")
    return pkg


def make(tmp_path, monkeypatch, found=None):
    store = RagStore(tmp_path / "rag")
    vault, started = {}, []
    pkg = package(tmp_path)
    monkeypatch.setattr(minio_setup, "RELEASE", {**minio_setup.RELEASE, "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest()})
    api = RagImagesApi(store, tools_dir=tmp_path / "tools", detect=lambda: found or {"programs": [], "broken": [], "listening": []},
                       fetch=lambda url, dest: dest.write_bytes(pkg.read_bytes()), launcher=lambda cmd, env: (started.append((cmd, env)), Proc())[1], s3_factory=FakeS3,
                       put_secret=vault.__setitem__, get_secret=lambda entry: vault[entry])
    return store, api, vault, started


def test_the_overview_says_where_the_images_are_and_what_minio_there_is(tmp_path, monkeypatch):
    store, api, _, _ = make(tmp_path, monkeypatch)
    got = api.route_get(["images"])
    assert got["store"] == "folder" and got["count"] == 0 and got["folder"].endswith("images") and got["minio"]["recommended"] == "install_separate"
    assert got["minio"]["release"]["bytes"] == minio_setup.RELEASE["bytes"]
    assert got["minio"]["own"] == {"installed": False, "running": False, "endpoint": "", "data_dir": str(tmp_path / "tools" / "data")}
    there = make(tmp_path, monkeypatch, {"programs": [{"path": "x"}], "broken": [], "listening": []})[1].overview()["minio"]
    assert [o["id"] for o in there["options"]] == ["use_existing", "install_separate", "skip"] and there["recommended"] is None


def test_the_folder_can_be_moved_from_the_gui_with_a_check_and_the_old_one_is_left(tmp_path, monkeypatch):
    store, api, _, _ = make(tmp_path, monkeypatch)
    store.image_store().put("ab" + "1" * 22, b"one")
    store.image_store().put("cd" + "2" * 22, b"two")
    where = tmp_path / "pictures"
    got = api.route_post(["images", "folder"], {"path": str(where)})
    assert got["moved"] == 2 and got["folder"] == str(where) and got["count"] == 2
    assert store.image_store().get("ab" + "1" * 22) == b"one" and (tmp_path / "rag" / "images").is_dir() and len(list((tmp_path / "rag" / "images").rglob("*.webp"))) == 2
    with pytest.raises(RagError, match="already"):
        api.route_post(["images", "folder"], {"path": str(where)})
    for bad in (None, "", "x" * 600):
        with pytest.raises(RagError):
            api.route_post(["images", "folder"], {"path": bad})


def test_an_existing_minio_is_checked_first_then_its_secret_goes_to_the_vault_and_the_settings_follow(tmp_path, monkeypatch):
    store, api, vault, _ = make(tmp_path, monkeypatch)
    with pytest.raises(RagError, match="needed"):
        api.route_post(["images", "minio", "existing"], {"endpoint": "127.0.0.1:9000"})
    FakeS3.refuse = True
    try:
        with pytest.raises(RagError, match="did not accept"):
            api.route_post(["images", "minio", "existing"], {"endpoint": "127.0.0.1:9000", "access_key": "k", "secret_key": "s"})
        assert store.settings.read()["images_store"] == "folder" and vault == {}
    finally:
        FakeS3.refuse = False
    got = api.route_post(["images", "minio", "existing"], {"endpoint": "127.0.0.1:9000", "access_key": "k", "secret_key": "the-secret", "bucket": "mine"})
    assert got["store"] == "s3" and got["s3"] == {"endpoint": "127.0.0.1:9000", "bucket": "mine", "access_key": "k", "secure": False} and vault == {"images-s3": "the-secret"}
    assert "the-secret" not in (tmp_path / "rag" / "settings.json").read_text(encoding="utf-8")
    assert api.route_post(["images", "use-folder"], {})["store"] == "folder"


def test_a_separate_minio_is_installed_on_request_started_on_its_own_port_and_used(tmp_path, monkeypatch):
    store, api, vault, started = make(tmp_path, monkeypatch)
    got = api.route_post(["images", "minio", "install"], {})
    own = got["minio"]["own"]
    assert own["installed"] and own["running"] and own["endpoint"].startswith("127.0.0.1:94") and got["store"] == "s3" and got["s3"]["endpoint"] == own["endpoint"]
    cmd, env = started[0]
    assert "127.0.0.1:" in cmd[cmd.index("--address") + 1] and env["MINIO_ROOT_PASSWORD"] == vault["images-s3"] and got["s3"]["access_key"].startswith("tadamoo-")
    assert api.route_post(["images", "minio", "stop"], {})["minio"]["own"]["running"] is False
    again = api.route_post(["images", "minio", "start"], {})
    assert again["minio"]["own"]["running"] and len(started) == 2 and started[1][1]["MINIO_ROOT_PASSWORD"] == vault["images-s3"]
    with pytest.raises(RagError):
        api.route_post(["images", "nothing"], {})
    assert api.route_post(["other"], {}) is None


def test_our_own_minios_data_folder_moves_from_the_gui_and_it_restarts_there(tmp_path, monkeypatch):
    store, api, vault, started = make(tmp_path, monkeypatch)
    with pytest.raises(RagError, match="install it first"):
        api.route_post(["images", "minio", "folder"], {"path": str(tmp_path / "nope")})
    api.route_post(["images", "minio", "install"], {})
    old_dir = tmp_path / "tools" / "data"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "bucket.dat").write_bytes(b"stuff")
    where = tmp_path / "elsewhere"
    got = api.route_post(["images", "minio", "folder"], {"path": str(where)})
    assert got["moved"] is True and got["minio"]["own"]["data_dir"] == str(where)
    assert not old_dir.is_dir() and (where / "bucket.dat").read_bytes() == b"stuff"
    assert got["minio"]["own"]["running"] is True and len(started) == 2                 # it was running: stopped, moved, started again
    cmd, _ = started[1]
    assert str(where) in cmd
    with pytest.raises(RagError, match="already"):
        api.route_post(["images", "minio", "folder"], {"path": str(where)})
    occupied = tmp_path / "occupied"
    occupied.mkdir(parents=True, exist_ok=True)
    (occupied / "junk").write_text("x", encoding="utf-8")
    with pytest.raises(RagError, match="not empty"):
        api.route_post(["images", "minio", "folder"], {"path": str(occupied)})
    for bad in (None, "", "x" * 600):
        with pytest.raises(RagError):
            api.route_post(["images", "minio", "folder"], {"path": bad})


def test_a_bundled_minio_is_used_in_place_with_no_download_and_still_found_without_installing_it_here(tmp_path, monkeypatch):
    store, api, vault, started = make(tmp_path, monkeypatch)
    bundled = tmp_path / "bundled" / minio_setup.EXE
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"MZ-bundled")
    monkeypatch.setattr(minio_setup, "bundled_program", lambda: bundled)
    assert api.overview()["minio"]["own"]["installed"] is True                       # found straight away, before any "install" was pressed
    got = api.route_post(["images", "minio", "install"], {})
    assert got["minio"]["own"]["installed"] and got["minio"]["own"]["running"]
    assert not (tmp_path / "tools" / minio_setup.EXE).exists()                       # nothing downloaded into the tools folder: run from the bundled copy
    assert not (tmp_path / "tools" / "download.zip").exists()


def test_our_own_minio_is_one_process_for_every_request_and_comes_up_by_itself_when_the_images_are_kept_in_it(tmp_path, monkeypatch):
    from serve import rag_images_api
    store, api, vault, started = make(tmp_path, monkeypatch)
    try:
        api.route_post(["images", "minio", "install"], {})
        assert len(started) == 1
        again = RagImagesApi(store, tools_dir=tmp_path / "tools", launcher=api._launcher, put_secret=api._put, get_secret=api._get)          # the API is made again at each request
        assert again.overview()["minio"]["own"]["running"] and again.route_post(["images", "minio", "start"], {})["minio"]["own"]["running"] and len(started) == 1
        assert again.route_post(["images", "minio", "stop"], {})["minio"]["own"]["running"] is False and api.overview()["minio"]["own"]["running"] is False
        assert again.autostart() is True and len(started) == 2 and api.overview()["minio"]["own"]["running"]            # kept in MinIO and not running: it is started
        assert again.autostart() is False and len(started) == 2                                                         # already running: nothing is started twice
        store.settings.write({"images_store": "folder"})
        again.route_post(["images", "minio", "stop"], {})
        assert again.autostart() is False and len(started) == 2                                                         # the images are in the folder: MinIO is left alone
        store.settings.write({"images_store": "s3"})
        assert again.autostart() is True
        rag_images_api.stop_all()
        assert api.overview()["minio"]["own"]["running"] is False
    finally:
        rag_images_api.stop_all()


def test_nothing_is_started_when_there_is_no_minio_of_our_own(tmp_path, monkeypatch):
    store, api, _, started = make(tmp_path, monkeypatch)
    store.settings.write({"images_store": "s3"})
    assert api.autostart() is False and started == []


def test_idle_minio_is_stopped_by_the_sweep_and_a_real_use_of_the_store_touches_it_and_starts_it_again(tmp_path, monkeypatch):
    from serve import rag_images_api
    store, api, vault, started = make(tmp_path, monkeypatch)
    try:
        api.route_post(["images", "minio", "install"], {})
        key = str(tmp_path / "tools")
        assert key in rag_images_api._LAST_USED and api.overview()["minio"]["own"]["running"]
        rag_images_api.sweep(clock=lambda: time.time() + rag_images_api.IDLE_S + 1)
        assert api.overview()["minio"]["own"]["running"] is False                      # stopped: idle longer than IDLE_S

        api.wire()
        store.on_image_store_use()                                                     # a real use of the store: touches it and starts it again since it is configured
        assert api.overview()["minio"]["own"]["running"] is True and len(started) == 2
        rag_images_api.sweep()                                                         # just touched: the ordinary sweep leaves it alone
        assert api.overview()["minio"]["own"]["running"] is True
    finally:
        rag_images_api.stop_all()


def test_wiring_the_store_calls_the_hook_only_when_the_images_are_actually_kept_in_s3(tmp_path, monkeypatch):
    store, api, _, started = make(tmp_path, monkeypatch)
    api.wire()
    assert store.on_image_store_use is not None
    store.image_store()                                                                # the folder store: the hook is never called (nothing to start)
    assert started == []

