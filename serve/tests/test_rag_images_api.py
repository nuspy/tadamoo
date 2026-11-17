"""Where the images are kept, from the settings page: the folder moved from the GUI, a MinIO that is already here, and a separate one of our own installed on request."""
import hashlib
import io
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
    assert got["minio"]["release"]["bytes"] == minio_setup.RELEASE["bytes"] and got["minio"]["own"] == {"installed": False, "running": False, "endpoint": ""}
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
