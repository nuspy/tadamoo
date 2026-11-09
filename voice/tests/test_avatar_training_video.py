"""The training video of an avatar: what is refused before it is kept (extension, type, size, container, unreadable, picture size), how it is streamed and replaced, that
nothing outside the avatar's `training` folder is touched, and the service routes (token, list, upload, removal, refused while the avatar is being trained)."""
import io
import json

import pytest

from callbridge.avatar_store import AvatarStore
from voice import avatar_training_video as atv
from voice.avatar_training import TrainingError
from voice.avatar_training_video import TrainingVideos
from voice.tests.test_avatar_service import AUTH, _isolated, rig  # noqa: F401 (fixtures)

from avatar_fakes import video_file

ISO = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + b"m" * 64
EBML = b"\x1a\x45\xdf\xa3" + b"e" * 64
INFO = {"seconds": 240.0, "fps": 25.0, "width": 640, "height": 480}


class Storage:
    """What werkzeug hands over for an uploaded file."""

    def __init__(self, data=ISO, filename="me.mp4", mimetype="video/mp4"):
        self.stream, self.filename, self.mimetype = io.BytesIO(data), filename, mimetype


@pytest.fixture(autouse=True)
def _free():
    atv._busy.clear()
    yield
    atv._busy.clear()


@pytest.fixture
def store(tmp_path):
    s = AvatarStore(tmp_path / "avatars")
    s.create("ada", "Ada")
    return s


def videos(store, info=None, **kw):
    return TrainingVideos(store, probe=lambda path: dict(info or INFO), **kw)


def refused(call, status, code):
    with pytest.raises(TrainingError) as e:
        call()
    assert (e.value.status, e.value.code) == (status, code), e.value


def kept(store):
    folder = store.path("ada") / "training"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ---------------------------------------------------------------------------------------- what is kept
def test_a_real_mp4_is_measured_by_the_reader_the_plan_uses_and_kept_as_the_training_video(store, tmp_path):
    src = video_file(tmp_path / "me.mp4", frames=20)
    out = TrainingVideos(store).save("ada", Storage(src.read_bytes()), 1000)
    assert out["ok"] is True and out["file"] == "video.mp4" and out["fps"] == 10.0 and (out["width"], out["height"]) == (160, 160) and out["seconds"] == 2.0
    assert out["in_range"] is False and out["range"] == [120.0, 600.0] and out["bytes"] == src.stat().st_size
    assert kept(store) == ["video.json", "video.mp4"] and store.training_video("ada") == store.path("ada") / "training" / "video.mp4"
    assert (store.path("ada") / "training" / "video.mp4").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("ext, data, mime", [(".mp4", ISO, "video/mp4"), (".mov", ISO, "video/quicktime"), (".webm", EBML, "video/webm"), (".mkv", EBML, "video/x-matroska"),
                                              (".MKV", EBML, ""), (".mp4", ISO, "application/octet-stream")])
def test_each_container_is_kept_under_a_fixed_name_and_in_range_follows_the_length(store, ext, data, mime):
    out = videos(store).save("ada", Storage(data, "my video" + ext, mime))
    assert out["file"] == "video" + ext.lower() and out["in_range"] is True and kept(store) == ["video.json", "video" + ext.lower()]
    assert videos(store, {**INFO, "seconds": 90.0}).save("ada", Storage(data, "a" + ext, mime))["in_range"] is False
    assert videos(store, {**INFO, "seconds": 600.0}).save("ada", Storage(data, "a" + ext, mime))["in_range"] is True


def test_a_new_upload_replaces_the_old_one_whatever_its_container(store):
    videos(store).save("ada", Storage(ISO, "a.mp4"))
    videos(store).save("ada", Storage(EBML, "b.mkv", "video/x-matroska"))
    assert kept(store) == ["video.json", "video.mkv"] and store.training_video("ada").name == "video.mkv"
    assert json.loads((store.path("ada") / "training" / "video.json").read_text())["file"] == "video.mkv"


def test_info_lists_the_video_the_limits_and_measures_a_file_put_there_by_hand(store):
    v = videos(store)
    assert v.info("ada") == {"video": None, "limits": {"max_bytes": atv.LIMIT, "extensions": [".mp4", ".mov", ".webm", ".mkv"], "seconds": [120.0, 600.0], "best": [180, 300]}}
    folder = store.path("ada") / "training"
    folder.mkdir()
    (folder / "video.webm").write_bytes(EBML)
    by_hand = v.info("ada")["video"]
    assert by_hand["file"] == "video.webm" and by_hand["seconds"] == 240.0 and by_hand["in_range"] is True and by_hand["bytes"] == len(EBML)
    v.probe = lambda path: (_ for _ in ()).throw(ValueError("no"))
    assert v.info("ada")["video"] == {"file": "video.webm", "bytes": len(EBML), "in_range": False}
    refused(lambda: v.info("nobody"), 404, "not_found")


def test_the_avatar_listing_says_whether_it_has_a_training_video(store):
    assert store.info("ada")["training_video"] is False
    videos(store).save("ada", Storage())
    assert store.info("ada")["training_video"] is True and store.listing()["avatars"][0]["size_bytes"] >= len(ISO)


# ---------------------------------------------------------------------------------------- what is refused
@pytest.mark.parametrize("name", ["evil.exe", "movie.gif", "movie", "movie.mp4.exe", "", None, ".mp4x", "mp4"])
def test_a_name_that_does_not_end_in_a_video_extension_is_refused_and_nothing_is_kept(store, name):
    refused(lambda: videos(store).save("ada", Storage(ISO, name)), 415, "bad_type")
    assert kept(store) == []


@pytest.mark.parametrize("mime", ["text/html", "image/png", "application/zip", "audio/mpeg"])
def test_a_type_that_is_not_a_video_type_is_refused(store, mime):
    refused(lambda: videos(store).save("ada", Storage(ISO, "a.mp4", mime)), 415, "bad_type")
    assert kept(store) == []


def test_the_size_cap_stops_the_copy_and_removes_the_partial_file(store):
    v = videos(store, limit=1000)
    refused(lambda: v.save("ada", Storage(ISO + b"z" * 2000)), 413, "too_big")
    assert kept(store) == []
    videos(store, limit=len(ISO)).save("ada", Storage(ISO))                                 # exactly the cap is fine


def test_the_file_is_read_in_pieces_and_a_long_stream_is_not_read_to_its_end(store):
    class Endless:
        reads = 0

        def read(self, n):
            Endless.reads += 1
            return ISO[:12] + b"x" * (n - 12) if Endless.reads == 1 else b"x" * n
    s = Storage()
    s.stream = Endless()
    refused(lambda: videos(store, limit=3 << 20).save("ada", s), 413, "too_big")
    assert Endless.reads == 4 and kept(store) == []                                          # three pieces fit, the fourth is over the cap


def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(store):
    s = Storage()
    s.stream = type("Never", (), {"read": lambda self, n: pytest.fail("the body was read")})()
    refused(lambda: videos(store, limit=1000).save("ada", s, 1000 + atv.OVERHEAD + 1), 413, "too_big")


def test_the_cap_is_finite_and_generous():
    assert 1 << 30 <= atv.LIMIT < 2 << 30


def test_an_empty_file_is_refused(store):
    refused(lambda: videos(store).save("ada", Storage(b"")), 400, "empty")
    assert kept(store) == []


@pytest.mark.parametrize("name, data, mime", [("a.mp4", EBML, "video/mp4"), ("a.mkv", ISO, "video/x-matroska"), ("a.mp4", b"<html>not a video at all</html>", "video/mp4"),
                                              ("a.webm", b"x" * 20, "video/webm")])
def test_the_first_bytes_must_be_the_container_the_extension_names(store, name, data, mime):
    refused(lambda: videos(store).save("ada", Storage(data, name, mime)), 422, "bad_container")
    assert kept(store) == []


def test_a_video_the_reader_cannot_open_or_with_a_wrong_picture_is_refused_and_the_old_one_stays(store):
    videos(store).save("ada", Storage(ISO + b"old"))
    before = (store.path("ada") / "training" / "video.mp4").read_bytes()
    bad = lambda path: (_ for _ in ()).throw(ValueError("the video cannot be read"))
    refused(lambda: TrainingVideos(store, probe=bad).save("ada", Storage(ISO + b"new")), 422, "unreadable")
    refused(lambda: videos(store, {**INFO, "width": 64, "height": 64}).save("ada", Storage(ISO + b"new")), 422, "bad_size")
    refused(lambda: videos(store, {**INFO, "width": 8000, "height": 5000}).save("ada", Storage(ISO + b"new")), 422, "bad_size")
    assert kept(store) == ["video.json", "video.mp4"] and (store.path("ada") / "training" / "video.mp4").read_bytes() == before


def test_a_missing_opencv_is_said_plainly(store):
    def no_cv2(path):
        raise ImportError("cv2")
    refused(lambda: TrainingVideos(store, probe=no_cv2).save("ada", Storage()), 501, "needs_opencv")
    assert kept(store) == []


def test_a_second_upload_for_the_same_avatar_at_once_is_refused(store):
    atv._busy.add("ada")
    refused(lambda: videos(store).save("ada", Storage()), 409, "busy")
    atv._busy.clear()
    videos(store).save("ada", Storage())


# ---------------------------------------------------------------------------------------- paths
def test_the_client_file_name_is_never_used_and_nothing_leaves_the_training_folder(store, tmp_path):
    videos(store).save("ada", Storage(ISO, "../../../../evil/../../x.mp4"))
    assert kept(store) == ["video.json", "video.mp4"]
    names = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert all(n.startswith("avatars/ada/") for n in names), names


def test_an_avatar_name_that_is_not_plain_or_unknown_never_builds_a_path(store):
    for name in ("../ada", "ada/../ada", "nobody", "", None, "train", "ada\\x"):
        refused(lambda: videos(store).save(name, Storage()), 404, "not_found")
        refused(lambda: videos(store).remove(name), 404, "not_found")
        refused(lambda: videos(store).info(name), 404, "not_found")


def test_a_training_folder_that_is_a_link_is_not_followed(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.path("ada") / "training").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make a symbolic link here")
    refused(lambda: videos(store).save("ada", Storage()), 409, "not_usable")
    assert list(outside.iterdir()) == [] and store.training_video("ada") is None


def test_a_link_in_place_of_the_video_is_not_a_training_video(store, tmp_path):
    folder = store.path("ada") / "training"
    folder.mkdir()
    target = tmp_path / "secret.mp4"
    target.write_bytes(ISO)
    try:
        (folder / "video.mp4").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make a symbolic link here")
    assert store.training_video("ada") is None


def test_training_is_not_a_name_an_avatar_can_take_as_a_route_but_is_a_plain_folder_inside(store):
    assert (store.path("ada") / "training").name == "training" and store.path("ada").name == "ada"


# ---------------------------------------------------------------------------------------- removal
def test_removal_deletes_the_video_and_its_note_and_the_empty_folder_and_then_says_there_is_none(store):
    v = videos(store)
    v.save("ada", Storage())
    assert v.remove("ada") == {"ok": True} and not (store.path("ada") / "training").exists() and store.training_video("ada") is None
    refused(lambda: v.remove("ada"), 404, "not_found")


def test_removal_leaves_any_other_file_in_the_folder_alone(store):
    v = videos(store)
    v.save("ada", Storage())
    (store.path("ada") / "training" / "notes.txt").write_text("mine")
    v.remove("ada")
    assert kept(store) == ["notes.txt"]


# ---------------------------------------------------------------------------------------- the service routes
def multipart(data=ISO, name="me.mp4", mime="video/mp4"):
    return {"data": {"file": (io.BytesIO(data), name, mime)}, "content_type": "multipart/form-data"}


@pytest.fixture
def routed(rig):
    rig.app.config["CB_AVATAR_TRAINING_VIDEOS"] = TrainingVideos(rig.store, probe=lambda path: dict(INFO))
    return rig


def test_the_routes_need_the_token(routed):
    for method in ("get", "post", "delete"):
        assert getattr(routed.client, method)("/avatar/ada/training-video").status_code == 401, method


def test_upload_list_and_remove_through_the_routes(routed):
    r = routed.client.get("/avatar/ada/training-video", headers=AUTH)
    assert r.status_code == 200 and r.get_json()["video"] is None and r.get_json()["limits"]["max_bytes"] == atv.LIMIT
    up = routed.client.post("/avatar/ada/training-video", headers=AUTH, **multipart())
    assert up.status_code == 200 and up.get_json()["file"] == "video.mp4" and up.get_json()["seconds"] == 240.0 and up.get_json()["in_range"] is True
    got = routed.get("/avatar/ada/training-video").get_json()["video"]
    assert got["file"] == "video.mp4" and got["bytes"] == len(ISO) and got["width"] == 640
    assert [a["training_video"] for a in routed.get("/avatar/list").get_json()["avatars"]] == [True]
    gone = routed.client.delete("/avatar/ada/training-video", headers=AUTH)
    assert gone.status_code == 200 and routed.get("/avatar/ada/training-video").get_json()["video"] is None
    assert routed.client.delete("/avatar/ada/training-video", headers=AUTH).status_code == 404


def test_the_routes_refuse_a_missing_field_a_bad_type_and_an_unknown_avatar(routed):
    assert routed.client.post("/avatar/ada/training-video", headers=AUTH, data={}, content_type="multipart/form-data").status_code == 400
    bad = routed.client.post("/avatar/ada/training-video", headers=AUTH, **multipart(name="x.exe"))
    assert bad.status_code == 415 and bad.get_json()["code"] == "bad_type"
    assert routed.client.post("/avatar/nobody/training-video", headers=AUTH, **multipart()).status_code == 404
    assert routed.client.post("/avatar/ada/training-video", headers=AUTH, **multipart(mime="text/html")).status_code == 415
    assert routed.get("/avatar/nobody/training-video").status_code == 404


def test_a_video_cannot_be_changed_while_the_avatar_is_being_trained(routed):
    routed.client.post("/avatar/ada/training-video", headers=AUTH, **multipart())

    class Training:
        def busy_with(self, name):
            return name == "ada"
    routed.app.config["CB_AVATAR_TRAINING"] = Training()
    up = routed.client.post("/avatar/ada/training-video", headers=AUTH, **multipart(ISO + b"2"))
    assert up.status_code == 409 and up.get_json()["code"] == "in_use"
    assert routed.client.delete("/avatar/ada/training-video", headers=AUTH).status_code == 409
    assert routed.get("/avatar/ada/training-video").get_json()["video"]["bytes"] == len(ISO)
    assert routed.get("/avatar/ada/training-video").status_code == 200                       # reading is fine
