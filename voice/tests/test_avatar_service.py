"""The avatar in the voice service: the function loads only when asked and says what is missing, the personality's avatar and voice are followed,
the routes (state, speak, preview stream, wizard, prepare, visemes, assets, delete), the camera that is off until the person turns it on.
The worker is a stand-in process, the GPU readings are fake, the network is blocked."""
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import time
import types
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from callbridge import avatar_output, avatar_weights, models_store as ms
from callbridge import viseme_gen
from callbridge.avatar_client import AvatarClient
from callbridge.avatar_store import AvatarStore
from voice import functions, persona_link, service

from avatar_fakes import photo_bytes, put_weights, video_file, wav_bytes

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}
FAKE = str(Path(__file__).resolve().parent / "fake_avatar_worker.py")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    orig = socket.socket.connect

    def only_loopback(self, address, *a, **k):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return orig(self, address, *a, **k)
        raise AssertionError("network access attempted")

    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", only_loopback)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


class FakeApi:
    """TaDaMoo's personalities as the service reads them."""

    def __init__(self, personas):
        self.personas = personas
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        return {"personas": self.personas}


class Rig:
    def write_settings(self, **over):
        data = {"models_dir": str(self.models), "brain": {"persona": "ada-p"},
                "services": {"avatar": {"selected": "musetalk-v15", "enabled": True}}, **over}
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        os.utime(self.settings, (time.time() + self.bump, time.time() + self.bump))
        self.bump += 1

    def get(self, path, **kw):
        return self.client.get(path, headers=AUTH, **kw)

    def post(self, path, body=None, **kw):
        return self.client.post(path, headers=AUTH, json=body if body is not None else {}, **kw) if "data" not in kw else self.client.post(path, headers=AUTH, **kw)

    def apply(self, on=True):
        return self.post("/functions/apply", {"functions": {"avatar": {"on": on}}})

    def wait(self, cond, seconds=10.0):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False


def mark_installed(models: Path):
    where = models / "avatar" / "musetalk-v15"
    (where / ".basalt-model.json").write_text(json.dumps({"files": [{"name": rel, "size": 1} for rel in avatar_weights.REQUIRED]}), encoding="utf-8")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    r.tmp, r.models, r.bump = tmp_path, tmp_path / "models", 0
    r.settings = tmp_path / "user" / "voice.json"
    r.settings.parent.mkdir()
    put_weights(r.models)
    mark_installed(r.models)
    monkeypatch.setenv("FAKE_AVATAR_LOG", str(tmp_path / "worker.jsonl"))
    r.write_settings()
    app, state = service.build_app(r.settings, TOKEN)
    state.config.avatar_python, state.config.avatar_worker = sys.executable, FAKE
    state.config.avatar_start_timeout_s = 20.0
    r.app, r.state, r.client = app, state, app.test_client()
    r.api = FakeApi([{"slug": "ada-p", "name": "Ada", "avatar": "ada", "voice": {"engine": "xtts", "voice": "ada-voice"}, "language": "it"},
                     {"slug": "no-avatar", "name": "None", "avatar": "", "voice": {"engine": "", "voice": ""}, "language": ""}])
    state.avatar._api_factory = lambda: r.api
    r.store = AvatarStore(state.settings.avatars_dir)
    folder = r.store.create("ada", "Ada")
    (folder / "clips" / "walk.mp4").write_bytes(b"x")
    (folder / "poster.jpg").write_bytes(photo_bytes())
    pool = {"free": 20000}
    monkeypatch.setattr(functions, "free_vram_mib", lambda gpu=0: pool["free"])

    @contextmanager
    def loading(name):
        yield
    state.functions.loading = loading
    yield r
    client = app.config.get("CB_AVATAR")
    if client is not None:
        client.stop()


def test_the_routes_need_the_token(rig):
    for method, path in (("get", "/avatar/state"), ("get", "/avatar/list"), ("get", "/avatar/preview.mjpg"), ("post", "/avatar/speak"), ("post", "/avatar/wizard/start"),
                         ("get", "/avatar/ada/assets.zip"), ("post", "/avatar/prepare"), ("get", "/personas/ada/poster")):
        assert getattr(rig.client, method)(path).status_code == 401, path


def test_nothing_starts_until_the_function_is_loaded(rig, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("a process was started"))
    for path in ("/avatar/state", "/avatar/list", "/avatar/output", "/functions", "/status"):
        assert rig.get(path).status_code == 200, path
    state = rig.get("/avatar/state").get_json()
    assert state["loaded"] is False and state["running"] is False and state["outputs"]["camera"]["running"] is False
    assert rig.post("/avatar/start").status_code == 409 and rig.post("/avatar/start").get_json()["code"] == "not_loaded"
    assert rig.post("/avatar/speak", {"text": "hi"}).get_json()["code"] == "not_loaded"


def test_a_missing_weight_leaves_the_function_unloaded_and_names_the_file(rig):
    where = rig.models / "avatar" / "musetalk-v15"
    (where / "s3fd" / "s3fd-619a316812.pth").unlink()
    (where / "face-parse-bisent" / "79999_iter.pth").unlink()
    # the download records only what it fetched; the weights put there by hand are the ones the worker's start checks
    (where / ".basalt-model.json").write_text(json.dumps({"files": [{"name": "musetalkV15/unet.pth", "size": 1}]}), encoding="utf-8")
    fn = rig.get("/functions").get_json()["functions"]["avatar"]
    assert fn["installed"] is True and "s3fd/s3fd-619a316812.pth" in fn["problem"] and "face-parse-bisent/79999_iter.pth" in fn["problem"]
    out = rig.apply().get_json()["report"]["avatar"]
    assert out["state"] == "unloaded" and "s3fd/s3fd-619a316812.pth" in out["error"] and "2 weight file" in out["error"]
    assert rig.app.config.get("CB_AVATAR")._proc is None                                # no process was started for it
    assert "s3fd" in rig.get("/avatar/state").get_json()["problem"]
    (where / "s3fd" / "s3fd-619a316812.pth").write_bytes(b"x")
    (where / "face-parse-bisent" / "79999_iter.pth").write_bytes(b"x")
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"             # once they are there it loads


def test_a_model_that_is_not_installed_stays_unloaded_with_the_plain_reason(rig):
    marker = rig.models / "avatar" / "musetalk-v15" / ".basalt-model.json"
    marker.write_text(json.dumps({"files": [{"name": "musetalkV15/unet.pth", "size": 99}]}), encoding="utf-8")
    out = rig.apply().get_json()["report"]["avatar"]
    assert out["state"] == "unloaded" and "is not installed: install it from Settings > Voice" in out["error"]
    assert rig.app.config.get("CB_AVATAR") is None or rig.app.config["CB_AVATAR"]._proc is None


def test_loading_the_function_starts_the_worker_with_the_personalitys_avatar_and_unloading_stops_it(rig):
    out = rig.apply().get_json()
    assert out["report"]["avatar"]["state"] == "loaded" and out["report"]["avatar"]["measured_mib"] == 0
    client = rig.app.config["CB_AVATAR"]
    assert client._proc.poll() is None and client.is_ready("ada")
    assert rig.state.config.avatar_persona == "ada" and rig.state.config.avatar_voice_engine == "xtts" and rig.state.config.avatar_voice_name == "ada-voice"
    state = rig.get("/avatar/state").get_json()
    assert state["loaded"] and state["running"] and state["persona"] == "ada" and state["link"]["avatar"] == "ada"
    proc = client._proc
    out = rig.apply(False).get_json()
    assert out["report"]["avatar"]["state"] == "unloaded" and proc.poll() is not None


def test_a_personality_without_an_avatar_loads_the_worker_and_waits(rig):
    rig.write_settings(brain={"persona": "no-avatar"})
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"
    client = rig.app.config["CB_AVATAR"]
    assert client._proc.poll() is None and not client.is_ready()
    state = rig.get("/avatar/state").get_json()
    assert state["loaded"] and not state["running"] and state["link"]["avatar"] == ""
    assert rig.post("/avatar/start").get_json()["code"] == "no_avatar"
    assert rig.post("/avatar/start", {"persona": "ada"}).status_code == 202                  # choosing one by name works
    assert rig.wait(lambda: client.is_ready("ada"))


def test_avatar_preload_follows_the_function_being_on(rig):
    assert rig.state.config.avatar_preload is True and rig.state.config.avatar_enabled is True
    rig.write_settings(services={"avatar": {"selected": "musetalk-v15", "enabled": False}})
    rig.get("/status")
    assert rig.state.config.avatar_preload is False and rig.state.config.avatar_enabled is False
    rig.write_settings(services={"avatar": {"selected": "musetalk-v15", "enabled": True}})
    rig.get("/status")
    assert rig.state.config.avatar_preload is True


def test_the_avatar_follows_the_personality_of_settings_voice(rig):
    rig.get("/avatar/state")
    assert (rig.state.config.avatar_persona, rig.state.config.avatar_voice_engine, rig.state.config.avatar_voice_name) == ("ada", "xtts", "ada-voice")
    rig.write_settings(brain={"persona": "no-avatar"})
    link = rig.get("/avatar/state").get_json()["link"]
    assert link["avatar"] == "" and rig.state.config.avatar_persona == "" and rig.state.config.avatar_voice_engine == ""
    rig.write_settings(brain={"persona": "gone"})
    assert "does not exist any more" in rig.get("/avatar/state").get_json()["link"]["problem"]
    rig.write_settings(brain={})
    assert "no personality" in rig.get("/avatar/state").get_json()["link"]["problem"]
    rig.api.personas[0]["avatar"] = "missing-folder"
    rig.write_settings()
    assert "not in the avatars folder" in rig.get("/avatar/state").get_json()["link"]["problem"]
    assert rig.state.config.avatar_persona == ""
    assert set(rig.api.asked) == {"/api/personas"}                                      # the only route of the server that it uses


def test_the_tts_router_speaks_the_avatar_with_the_personalitys_voice(rig):
    from callbridge.engines import tts_router
    rig.get("/avatar/state")
    fake_router = types.SimpleNamespace(_cfg=rig.state.config)
    assert tts_router.TtsRouter.voce_avatar_attivo(fake_router) == {"engine": "xtts", "voice": "ada-voice"}
    assert tts_router.TtsRouter._engine_avatar_attivo(types.SimpleNamespace(voce_avatar_attivo=lambda: {"engine": "xtts", "voice": "ada-voice"}), "ada-voice") == "xtts"
    rig.state.config.avatar_voice_engine = ""
    assert tts_router.TtsRouter.voce_avatar_attivo(fake_router) == {}


def test_server_unreachable_does_not_break_the_state(rig):
    class Down:
        def get(self, path):
            from callbridge.tadamoo_api import ApiError
            raise ApiError("TaDaMoo does not answer (ConnectionRefusedError)")
    rig.state.avatar._api_factory = lambda: Down()
    link = rig.get("/avatar/state").get_json()["link"]
    assert link["avatar"] == "" and "could not be read" in link["problem"]


def test_speaking_uses_the_personalitys_engine_and_voice_and_needs_the_avatar_loaded(rig, monkeypatch):
    said = []

    def fake(self, text, engine, voice, lang):
        wav = rig.tmp / "said.wav"
        wav.write_bytes(wav_bytes(1.0))
        said.append((text, engine, voice, lang))
        return str(wav), 1000
    monkeypatch.setattr("voice.avatar_service.AvatarService.synthesize", fake)
    assert rig.post("/avatar/speak", {"text": "ciao"}).status_code == 409
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"
    out = rig.post("/avatar/speak", {"text": "  ciao  ", "lang": "it"})
    assert out.status_code == 200 and out.get_json()["voice"] == "ada-voice" and out.get_json()["engine"] == "xtts" and out.get_json()["avatar"] == "ada"
    assert said == [("ciao", "xtts", "ada-voice", "it")]
    client = rig.app.config["CB_AVATAR"]
    assert client.wait_until_playing(5) and client.wait_until_spoken(5)
    rig.api.personas[0]["voice"] = {"engine": "", "voice": ""}
    rig.store.write_metadata("ada", voice={"engine": "cosyvoice", "voice": "made"})
    rig.post("/avatar/speak", {"text": "again"})
    assert said[-1][1:3] == ("cosyvoice", "made")                                      # no voice on the personality: the one the wizard made
    for bad in ({}, {"text": ""}, {"text": "x" * 601}, {"text": 5}, {"text": "hi", "lang": "../x"}):
        assert rig.post("/avatar/speak", bad).status_code == 400, bad


def test_the_synthesis_goes_through_the_router_and_writes_a_wav(rig, monkeypatch):
    import numpy as np
    from callbridge import engines
    calls = []

    class Router:
        def synth(self, text, voice, lang, speed, engine):
            calls.append((text, voice, lang, engine))
            return np.zeros(8000, np.float32), 16000
    monkeypatch.setattr(engines, "_get_tts_engine", lambda cfg: Router())
    path, ms_ = rig.state.avatar.synthesize("ciao", "xtts", "ada-voice", "it")
    assert calls == [("ciao", "ada-voice", "it", "xtts")] and ms_ == 500 and Path(path).read_bytes()[:4] == b"RIFF"
    assert Path(path).parent == Path(rig.state.settings.avatars_dir).parent / "avatar-state" / "speech"


def test_the_preview_is_an_mjpeg_stream_with_framing_and_a_viewer_count(rig):
    assert rig.get("/avatar/preview.mjpg").status_code == 409                           # not loaded
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"
    resp = rig.client.get("/avatar/preview.mjpg", headers=AUTH, buffered=False)
    assert resp.status_code == 200 and resp.headers["Content-Type"] == avatar_output.CONTENT_TYPE and "no-store" in resp.headers["Cache-Control"]
    stream = iter(resp.response)
    first = next(stream)
    head, _sep, body = first.partition(b"\r\n\r\n")
    assert head.startswith(b"--avatarframe\r\nContent-Type: image/jpeg\r\nContent-Length: ")
    size = int(head.split(b"Content-Length: ")[1])
    assert body[:2] == b"\xff\xd8" and body[size - 2:size] == b"\xff\xd9" and body[size:] == b"\r\n"
    assert rig.get("/avatar/output").get_json()["preview"]["viewers"] == 1
    assert next(stream).startswith(b"--avatarframe")
    resp.close()
    assert rig.wait(lambda: rig.get("/avatar/output").get_json()["preview"]["viewers"] == 0)
    assert rig.app.config["CB_AVATAR"].frames.count == 0                                # nothing is encoded or sent with nobody watching


def test_only_a_few_can_watch_at_once(rig):
    rig.apply()
    streams = [rig.client.get("/avatar/preview.mjpg", headers=AUTH, buffered=False) for _ in range(avatar_output.MAX_VIEWERS)]
    assert all(s.status_code == 200 for s in streams)
    assert rig.get("/avatar/preview.mjpg").status_code == 503
    for s in streams:
        s.close()


def test_the_preview_uses_the_jpeg_encoder_only_while_someone_watches():
    from callbridge.avatar_client import AvatarFrames, Frame
    frames, encoded = AvatarFrames(), []
    now = [0.0]
    prev = avatar_output.Preview(frames, fps=10, clock=lambda: now[0])
    class Counting(Frame):
        __slots__ = ()

        def jpeg(self, quality=80):
            encoded.append(quality)
            return b"\xff\xd8jpeg\xff\xd9"
    frame = Counting(16, 16, bytes([100]) * 384)
    frames.publish(frame)
    assert encoded == []                                                                # nobody watches
    assert prev.open() and frames.count == 1
    frames.publish(frame)
    now[0] = 0.05
    frames.publish(frame)                                                               # too soon after the last: dropped
    now[0] = 0.2
    frames.publish(frame)
    assert encoded == [75, 75]
    prev.close()
    assert frames.count == 0


def test_the_camera_is_off_by_default_installs_nothing_and_never_imports_the_driver_library(rig, monkeypatch):
    source = Path(avatar_output.__file__).read_text(encoding="utf-8")
    for word in ("subprocess", "os.system", "pip ", "ensurepip", "sudo", "modprobe", "regsvr32", "msiexec", "urllib", "requests", "socket"):
        assert word not in source, word
    assert rig.state.settings.avatar.virtual_camera is False and rig.state.config.avatar_virtual_camera is False
    sys.modules.pop("pyvirtualcam", None)
    rig.apply()
    cam = rig.get("/avatar/output").get_json()["camera"]
    assert cam["enabled"] is False and cam["running"] is False
    assert "pyvirtualcam" not in sys.modules or sys.modules["pyvirtualcam"] is None


def test_turning_the_camera_on_sends_the_frames_to_a_virtual_camera_and_off_closes_it(rig, monkeypatch):
    opened, sent = [], []

    class Cam:
        device = "Fake Virtual Camera"

        def send(self, frame):
            sent.append(frame.shape)

        def close(self):
            opened.append("closed")
    monkeypatch.setattr(avatar_output, "_open_camera", lambda w, h, fps: opened.append((w, h, fps)) or Cam())
    monkeypatch.setattr(avatar_output, "camera_support", lambda: (True, ""))
    rig.apply()
    rig.write_settings(avatar={"virtual_camera": True})
    assert rig.get("/avatar/output").get_json()["camera"]["enabled"] is True
    assert rig.wait(lambda: rig.get("/avatar/output").get_json()["camera"]["running"])
    assert opened[0][:2] == (16, 16) and rig.get("/avatar/output").get_json()["camera"]["device"] == "Fake Virtual Camera"
    assert rig.wait(lambda: len(sent) >= 2) and sent[0] == (16, 16, 3)
    rig.write_settings(avatar={"virtual_camera": False})
    assert rig.get("/avatar/output").get_json()["camera"]["running"] is False
    assert opened[-1] == "closed"


def test_the_camera_follows_the_avatar_being_loaded(rig, monkeypatch):
    monkeypatch.setattr(avatar_output, "_open_camera", lambda w, h, fps: types.SimpleNamespace(device="d", send=lambda f: None, close=lambda: None))
    monkeypatch.setattr(avatar_output, "camera_support", lambda: (True, ""))
    rig.write_settings(avatar={"virtual_camera": True})
    assert rig.get("/avatar/output").get_json()["camera"]["running"] is False         # nothing loaded: nothing to send
    rig.apply()
    assert rig.wait(lambda: rig.get("/avatar/output").get_json()["camera"]["running"])
    rig.apply(False)
    assert rig.get("/avatar/output").get_json()["camera"]["running"] is False


def test_a_system_without_a_virtual_camera_says_unsupported_and_a_missing_driver_says_what_to_install(rig, monkeypatch):
    monkeypatch.setattr(avatar_output.sys, "platform", "darwin")
    assert avatar_output.camera_support() == (False, "the virtual camera is unsupported on this system")
    rig.apply()
    rig.write_settings(avatar={"virtual_camera": True})
    cam = rig.get("/avatar/output").get_json()["camera"]
    assert cam["enabled"] and not cam["supported"] and not cam["running"] and "unsupported" in cam["reason"]
    monkeypatch.setattr(avatar_output.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pyvirtualcam", None)
    assert avatar_output.camera_support() == (False, "pyvirtualcam is not installed in the voice environment")
    monkeypatch.undo()


def test_a_camera_that_cannot_be_opened_is_reported_and_the_avatar_goes_on(rig, monkeypatch):
    def no_driver(w, h, fps):
        raise RuntimeError("no virtual camera found")
    monkeypatch.setattr(avatar_output, "_open_camera", no_driver)
    monkeypatch.setattr(avatar_output, "camera_support", lambda: (True, ""))
    rig.apply()
    rig.write_settings(avatar={"virtual_camera": True})
    rig.get("/avatar/output")
    assert rig.wait(lambda: not rig.state.avatar.camera.running and "installs no driver" in rig.state.avatar.camera.reason)
    assert rig.app.config["CB_AVATAR"]._proc.poll() is None


def test_the_visemes_of_a_sentence(rig):
    out = rig.post("/avatar/visemes", {"text": "Ciao a tutti", "duration_s": 2.0, "lang": "it"}).get_json()
    assert out["type"] == "visemes" and out["duration_ms"] == 2000 and out["text"] == "Ciao a tutti" and out["visemes"][0]["t"] == 0
    assert abs(sum(v["dur"] for v in out["visemes"]) - 2000) < 50
    assert all(set(v) == {"t", "v", "dur"} and v["v"] in viseme_gen.VISEMI for v in out["visemes"])
    for bad in ({}, {"text": "hi"}, {"text": "hi", "duration_s": 0}, {"text": "hi", "duration_s": 601}, {"text": "hi", "duration_s": True}, {"text": "", "duration_s": 1},
                {"text": "x" * 2001, "duration_s": 1}, {"text": "hi", "duration_s": 1, "lang": "../x"}):
        assert rig.post("/avatar/visemes", bad).status_code == 400, bad


def test_the_phones_assets_come_as_one_zip_with_a_manifest_of_the_persons_own_avatar(rig):
    resp = rig.get("/avatar/ada/assets.zip")
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    files = ["poster.jpg", "wav2lip/face.png", "wav2lip/box.json", "visemes/frame.json"] + [f"visemes/{v}.png" for v in viseme_gen.VISEMI]
    assert sorted(z.namelist()) == sorted(files + ["manifest.json"])
    assert json.loads(z.read("wav2lip/box.json"))["ver"] == 2 and z.read("wav2lip/face.png")[:4] == b"\x89PNG" and z.read("visemes/aa.png")[:4] == b"\x89PNG"
    man = json.loads(z.read("manifest.json"))
    assert man["version"] == 1 and man["avatar"] == "ada" and man["created"] and man["render_modes"] == ["wav2lip", "visemes"]
    assert sorted(man["files"]) == sorted(files) and all(hashlib.sha256(z.read(path)).hexdigest() == digest for path, digest in man["files"].items())
    assert rig.get("/avatar/ada/manifest.json").get_json() == man
    assert rig.get("/avatar/nobody/assets.zip").status_code == 404 and rig.get("/avatar/nobody/manifest.json").status_code == 404
    for bad in ("..", "a%2Fb", "a b"):
        assert rig.get(f"/avatar/{bad}/assets.zip").status_code in (404, 308, 400)
    rig.store.create("blank")
    assert rig.get("/avatar/blank/assets.zip").get_json()["code"] == "no_poster"
    assert rig.get("/avatar/blank/manifest.json").get_json()["code"] == "no_poster"


def test_the_manifest_lists_the_video_only_once_a_clip_is_prepared(rig):
    assert rig.get("/avatar/ada/manifest.json").get_json()["render_modes"] == ["wav2lip", "visemes"]
    rig.store.record_prepared("ada", "walk.mp4", 5)
    assert rig.get("/avatar/ada/manifest.json").get_json()["render_modes"] == ["wav2lip", "visemes", "video"]


def test_an_avatar_made_before_the_sprites_gets_them_from_its_photo_and_keeps_the_mouth_the_person_marked(rig):
    folder = rig.store.existing("ada")
    assert rig.post("/personas/ada/wav2lip/bocca", {"p1": [70, 110], "p2": [130, 110]}).get_json()["ok"]
    shutil.rmtree(folder / "visemes")
    man = rig.get("/avatar/ada/manifest.json").get_json()
    assert man["render_modes"] == ["wav2lip", "visemes"]
    frame = json.loads((folder / "visemes" / "frame.json").read_text(encoding="utf-8"))
    assert frame["mouth"]["w"] > 0 and abs(frame["inv"][2] + frame["inv"][0] * frame["size"][0] / 2 - 100) < 2         # centred on the marked mouth (100, 110)


def test_the_avatar_of_a_personality_and_its_render_modes_are_told_to_the_gateway(rig):
    assert rig.get("/avatar/phone").get_json() == {"persona": "ada-p", "avatar": "ada", "render_modes": ["wav2lip", "visemes"], "problem": ""}
    none = rig.get("/avatar/phone?persona=no-avatar").get_json()
    assert none["avatar"] == "" and none["render_modes"] == []
    assert rig.get("/avatar/phone?persona=../x").status_code == 400
    assert rig.client.get("/avatar/phone").status_code == 401


def test_the_wav2lip_model_is_served_with_ranges_an_etag_of_its_sha256_and_a_plain_404_when_it_is_not_installed(rig):
    resp = rig.get("/avatar/models/wav2lip.onnx")
    assert resp.status_code == 404 and resp.get_json()["code"] == "model_not_installed" and "not installed" in resp.get_json()["error"]
    where = rig.models / "avatar" / "wav2lip-int8"
    where.mkdir(parents=True)
    blob = bytes(range(256)) * 40
    (where / "wav2lip_int8.onnx").write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    resp = rig.get("/avatar/models/wav2lip.onnx")
    assert resp.status_code == 200 and resp.data == blob and resp.headers["ETag"] == f'"{digest}"' and resp.headers["X-Content-SHA256"] == digest
    assert resp.headers["Accept-Ranges"] == "bytes" and "no-cache" in resp.headers["Cache-Control"]
    part = rig.client.get("/avatar/models/wav2lip.onnx", headers={**AUTH, "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.data == blob[10:20] and part.headers["Content-Range"] == f"bytes 10-19/{len(blob)}"
    assert rig.client.get("/avatar/models/wav2lip.onnx", headers={**AUTH, "If-None-Match": f'"{digest}"'}).status_code == 304
    assert rig.client.get("/avatar/models/wav2lip.onnx").status_code == 401


def test_the_sprite_sheet_shows_every_mouth_over_the_photo_in_one_picture(rig):
    import cv2
    import numpy as np
    resp = rig.get("/avatar/ada/sprites.png")
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    sheet = cv2.imdecode(np.frombuffer(resp.data, np.uint8), cv2.IMREAD_COLOR)
    frame = json.loads((rig.store.existing("ada") / "visemes" / "frame.json").read_text(encoding="utf-8"))
    assert sheet.shape[:2] == (3 * frame["size"][1], 5 * frame["size"][0])                  # 15 sprites in 5 columns and 3 rows
    assert rig.get("/avatar/nobody/sprites.png").status_code == 404 and rig.client.get("/avatar/ada/sprites.png").status_code == 401


def test_the_images_are_served_on_callapicalls_paths(rig):
    assert rig.get("/personas/ada/poster").mimetype == "image/jpeg"
    assert rig.get("/personas/ada/wav2lip/face").mimetype == "image/png"
    assert rig.get("/personas/ada/wav2lip/box").get_json()["ver"] == 2
    assert set(rig.get("/personas/ada/mouthbox").get_json()) == {"cx", "cy", "w"}
    assert rig.get("/personas/nobody/poster").status_code == 404
    new = rig.post("/personas/ada/wav2lip/bocca", {"p1": [80, 120], "p2": [120, 120]}).get_json()
    assert new["ok"] and new["box"]["bocca"]["p1"] == [80.0, 120.0]
    assert rig.post("/personas/ada/wav2lip/bocca", {"p1": [1], "p2": "x"}).status_code == 400
    assert rig.post("/personas/ada/poster", data={"file": (io.BytesIO(photo_bytes(side=120)), "x.png")}).get_json() == {"ok": True}
    assert rig.post("/personas/ada/poster", data={"file": (io.BytesIO(b"junk"), "x.png")}).status_code == 400
    assert rig.post("/personas/ada/poster", data={}).status_code == 400


def test_the_wizard_over_http_from_the_photo_to_the_avatar(rig, monkeypatch):
    from callbridge.engines import catalog
    monkeypatch.setattr(catalog, "disponibile", lambda cfg, spec, router=None: spec.id == "cosyvoice")
    monkeypatch.setenv(ms.ROOT_ENV, str(rig.models))
    draft = rig.post("/avatar/wizard/start").get_json()["draft_id"]
    up = lambda step, data, name="f": rig.post(f"/avatar/wizard/{draft}/{step}", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
    assert up("foto", photo_bytes(), "me.png").get_json()["file"] == "foto.jpg"
    assert up("foto", b"junk").status_code == 400
    assert up("video", video_file(rig.tmp / "v.mp4").read_bytes()).get_json()["frames"] == 12
    assert up("voce", wav_bytes(4.0)).get_json()["format"] == "wav"
    assert up("nonsense", b"x").status_code == 400
    assert rig.post(f"/avatar/wizard/{draft}/foto", data={}).status_code == 400
    assert rig.get(f"/avatar/wizard/{draft}").get_json()["video"] is True
    assert rig.post(f"/avatar/wizard/{'0' * 32}/foto", data={"file": (io.BytesIO(photo_bytes()), "x")}).status_code == 404
    assert rig.post(f"/avatar/wizard/{draft}/elabora", {"name": ""}).status_code == 400
    out = rig.post(f"/avatar/wizard/{draft}/elabora", {"name": "Zoe", "language": "it"}).get_json()
    assert out["avatar"] == "Zoe" and out["clips"] == ["Zoe.mp4"] and out["voice"] == {"engine": "cosyvoice", "voice": "Zoe"} and out["can_prepare"] is False
    assert rig.get("/avatar/list").get_json()["avatars"][-1]["name"] in ("Zoe", "ada")
    assert [a["name"] for a in rig.get("/avatar/list").get_json()["avatars"]] == ["Zoe", "ada"]
    assert rig.get(f"/avatar/wizard/{draft}").status_code == 404                        # the draft is gone once it is an avatar
    d2 = rig.post("/avatar/wizard/start").get_json()["draft_id"]
    assert rig.client.delete(f"/avatar/wizard/{d2}", headers=AUTH).get_json() == {"ok": True}


def test_preparing_a_clip_over_http_shows_progress_and_can_be_cancelled(rig, monkeypatch):
    monkeypatch.setenv("FAKE_AVATAR_STEP_S", "0.2")
    monkeypatch.setenv("FAKE_AVATAR_PREP_STEPS", "30")
    assert rig.post("/avatar/prepare", {"avatar": "ada", "clip": "walk.mp4"}).get_json()["code"] == "not_loaded"
    rig.apply()
    assert rig.post("/avatar/prepare", {"avatar": "nobody", "clip": "walk.mp4"}).status_code == 404
    assert rig.post("/avatar/prepare", {"avatar": "ada", "clip": "../x.mp4"}).status_code == 404
    started = rig.post("/avatar/prepare", {"avatar": "ada"})                              # one clip: no need to name it
    assert started.status_code == 202 and started.get_json()["clip"] == "walk.mp4"
    assert rig.post("/avatar/prepare", {"avatar": "ada", "clip": "walk.mp4"}).get_json()["code"] == "busy"
    assert rig.wait(lambda: (rig.get("/avatar/prepare").get_json()["job"] or {}).get("progress"))
    assert rig.get("/avatar/prepare").get_json()["job"]["progress"]["phase"] == "latents"
    assert rig.get("/avatar/state").get_json()["prepare"]["state"] == "running"
    assert rig.client.delete("/avatar/ada", headers=AUTH).get_json()["code"] == "in_use"
    assert rig.post("/avatar/prepare/cancel").get_json() == {"cancelling": True}
    assert rig.wait(lambda: rig.get("/avatar/prepare").get_json()["job"]["state"] == "cancelled")
    assert rig.post("/avatar/prepare/cancel").get_json() == {"cancelling": False}


def test_deleting_an_avatar_frees_its_space_and_refuses_one_that_is_in_use(rig):
    rig.store.create("old")
    (rig.store.path("old") / "poster.jpg").write_bytes(b"x" * 500)
    assert rig.client.delete("/avatar/old", headers=AUTH).get_json()["freed_bytes"] >= 500
    assert rig.client.delete("/avatar/old", headers=AUTH).status_code == 404
    assert rig.client.delete("/avatar/..", headers=AUTH).status_code in (404, 308, 405)
    rig.apply()
    assert rig.client.delete("/avatar/ada", headers=AUTH).get_json()["code"] == "in_use"
    assert rig.get("/avatar/list").get_json()["size_bytes"] > 0


def test_lipsync_interrupt_and_stream_for_the_gateway(rig):
    assert rig.post("/avatar/lipsync", {"wav": str(rig.tmp / "x.wav")}).status_code == 409
    rig.apply()
    wav = rig.tmp / "x.wav"
    wav.write_bytes(wav_bytes(1.0))
    out = rig.post("/avatar/lipsync", {"wav": str(wav), "mute": True}).get_json()
    assert out["status"] == "ok" and out["partito"] is True and out["preparazione_s"] == pytest.approx(0.12)
    assert rig.post("/avatar/lipsync", {"wav": str(rig.tmp / "none.wav")}).status_code == 400
    assert rig.post("/avatar/lipsync", {}).status_code == 400
    assert rig.post("/avatar/interrupt", {"svuota": True}).get_json() == {"status": "ok"}
    assert rig.post("/avatar/stream", {"on": False}).get_json() == {"status": "ok", "streaming": False}
    assert rig.post("/avatar/stream", {"port": 99999}).status_code == 400
    assert rig.post("/avatar/stream", {"port": "x"}).status_code == 400


def test_the_service_stops_the_worker_it_started(rig):
    rig.apply()
    proc = rig.app.config["CB_AVATAR"]._proc
    assert proc.poll() is None
    assert "app.config.get(\"CB_AVATAR\")" in Path(service.__file__).read_text(encoding="utf-8")
    rig.app.config["CB_AVATAR"].stop()
    assert proc.poll() is not None
