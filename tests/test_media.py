"""The picture and video maker: its settings, its caps, the worker run as a subprocess against a stand-in LTX Studio and a stand-in broker (the lease
asked and given back, a refusal, a cancel, silence, a model process that fails), without a GPU."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from media_fake import Broker, make_ltx
from tadamoo.media import client, limits
from tadamoo.media.errors import Cancelled, MediaError, Refused
from tadamoo.media.settings import DEFAULTS, MediaSettings


@pytest.fixture
def world(tmp_path):
    ltx = make_ltx(tmp_path / "ltx")
    broker = Broker()
    settings = {**DEFAULTS, "ltx_dir": str(ltx), "wait_s": 5, "idle_s": 30}
    client.forget_checks()
    yield {"ltx": ltx, "broker": broker, "settings": settings, "tmp": tmp_path}
    broker.stop()


def run(world, kind="image", prompt="a red fox", out=None, **kw):
    out = out or world["tmp"] / "job" / ("out.png" if kind == "image" else "out.mp4")
    request = {"prompt": prompt, "width": 1024, "height": 1024, "steps": 20, "seed": 7, "negative": ""} if kind == "image" else \
        {"prompt": prompt, "width": 704, "height": 512, "frames": 65, "fps": 25, "seed": 7, "negative": ""}
    return client.run(kind, request, out, world["settings"], url=world["broker"].url, **kw), out


def test_the_settings_keep_what_is_valid_and_refuse_the_rest(tmp_path):
    s = MediaSettings(tmp_path / "media.json")
    assert s.read() == DEFAULTS
    got = s.write({"enabled": False, "ltx_dir": str(tmp_path), "image_mib": 9000, "wait_s": 0})
    assert got["enabled"] is False and got["image_mib"] == 9000 and s.read() == got
    for bad in ({"enabled": "yes"}, {"ltx_dir": str(tmp_path / "none")}, {"image_python": str(tmp_path / "none.exe")}, {"image_mib": 5}, {"video_model": "a/b.safetensors"}, {"nope": 1}):
        with pytest.raises(MediaError):
            s.write(bad)
    (tmp_path / "media.json").write_text('{"enabled": 3, "image_mib": "x", "wait_s": 12}', encoding="utf-8")
    assert s.read()["wait_s"] == 12 and s.read()["enabled"] is True and s.read()["image_mib"] == DEFAULTS["image_mib"]


def test_the_requests_have_defaults_caps_and_roundings():
    r = limits.image_request("a fox")
    assert (r["width"], r["height"], r["steps"], r["negative"]) == (1024, 1024, 20, "") and 0 <= r["seed"] < 2 ** 31
    assert limits.image_request("a fox", 1000, 700)["width"] == 992 and limits.image_request("a fox", 1000, 700, seed=5)["notes"]
    v = limits.video_request("a fox running", frames=70)
    assert (v["width"], v["height"], v["frames"], v["fps"]) == (704, 512, 65, 25) and v["notes"]
    assert limits.video_request("x", 700, 500)["width"] == 704
    for call in (lambda: limits.image_request(""), lambda: limits.image_request("x", width=4096), lambda: limits.image_request("x", 2048, 2048, steps=51),
                 lambda: limits.image_request("x" * 5000), lambda: limits.image_request("x", steps=True), lambda: limits.video_request("x", frames=500),
                 lambda: limits.video_request("x", 1280, 1280), lambda: limits.video_request("x", fps=60), lambda: limits.video_request("x", frames=3),
                 lambda: limits.image_request("x", seed=-1), lambda: limits.image_request(None)):
        with pytest.raises(MediaError):
            call()


def test_the_check_reads_what_ltx_studio_has(world, tmp_path):
    got = client.check(world["settings"])
    assert got["ltx_dir"] == str(world["ltx"]) and got["image"]["ready"] and got["video"]["ready"]
    other = make_ltx(tmp_path / "ltx2", missing=["qwen21-te-nvfp4"], video_checkpoint=False)
    got = client.check({**world["settings"], "ltx_dir": str(other)})
    assert not got["image"]["ready"] and got["image"]["missing"] == ["qwen21-te-nvfp4"] and not got["video"]["ready"] and got["video"]["missing"][0].startswith("checkpoint")
    got = client.check({**world["settings"], "ltx_dir": str(tmp_path / "nothing")})
    assert not got["image"]["ready"] and "does not exist" in got["image"]["error"]
    got = client.check({**world["settings"], "image_python": str(tmp_path / "none.exe")}, force=True)
    assert got["image"]["python_ok"] is False and not got["image"]["ready"] and got["video"]["ready"]


def test_an_image_asks_for_the_lease_runs_the_model_and_gives_the_lease_back(world):
    seen = []
    got, out = run(world, progress=seen.append)
    assert out.read_bytes()[:4] == b"\x89PNG" and got["path"] == str(out) and got["bytes"] == out.stat().st_size and got["seconds"] >= 0
    assert got["lease"]["granted_mib"] == 11000 and got["lease"]["owner"] == "image"
    paths = world["broker"].paths()
    assert paths[:2] == ["/api/heavy/begin", "/api/vram/lease"] and paths[-1].endswith("/release") and paths.count("/api/vram/lease") == 1
    ask = world["broker"].lease_calls()[0][1]
    assert ask["owner"] == "image" and ask["mib"] == 11000 and ask["wait_s"] == 5
    assert [e["event"] for e in seen][:2] == ["start", "lease"] and [(e["step"], e["steps"]) for e in seen if e["event"] == "progress"] == [(1, 3), (2, 3), (3, 3)]
    params = json.loads((out.parent / "params.json").read_text(encoding="utf-8"))
    assert (params["prompt"], params["width"], params["height"], params["steps"], params["seed"]) == ("a red fox", 1024, 1024, 20, 7)
    assert params["arch"] == "qwen_image21" and params["qwen21_dit"] == "qwen21-dit-nvfp4" and params["qwen21_te"] == "nvfp4" and params["output"] == str(out)
    assert params["need_vram_gb"] == pytest.approx(11000 / 1024 - 2) and params["parent_pid"] > 0


def test_a_video_runs_the_release_25_checkpoint_with_its_own_lease_size(world):
    got, out = run(world, "video", "a fox runs")
    assert out.read_bytes()[4:8] == b"ftyp" and got["lease"]["granted_mib"] == 25000 and got["lease"]["owner"] == "video"
    params = json.loads((out.parent / "params.json").read_text(encoding="utf-8"))
    assert params["num_frames"] == 65 and params["fps"] == 25 and params["ltx_release"] == "2.5" and params["model"].endswith("ltx-2.5-22b-distilled-transformer-nvfp4.safetensors")
    assert world["broker"].lease_calls()[0][1]["mib"] == 25000


def test_a_refused_lease_stops_before_the_model_starts_and_carries_the_reason(world):
    world["broker"].answer = (409, {"ok": False, "reason": "not_enough", "message": "only 3 GB can be freed", "message_it": "si liberano solo 3 GB"})
    with pytest.raises(Refused) as e:
        run(world, "video")
    assert e.value.reason == "not_enough" and "3 GB" in str(e.value) and e.value.message_it.startswith("si liberano")
    assert not (world["tmp"] / "job" / "started.pid").exists() and not any(p.endswith("/release") for p in world["broker"].paths())


def test_no_server_is_a_refusal_with_its_reason_not_a_crash(world):
    world["broker"].stop()
    with pytest.raises(Refused) as e:
        run(world)
    assert e.value.reason == "no_engine"
    assert not (world["tmp"] / "job" / "started.pid").exists()


def test_missing_weights_are_told_before_any_lease_is_asked(tmp_path):
    ltx = make_ltx(tmp_path / "ltx", missing=["qwen21-dit-nvfp4"])
    broker = Broker()
    try:
        with pytest.raises(MediaError, match="lacks weights.*qwen21-dit-nvfp4"):
            client.run("image", {"prompt": "x", "width": 256, "height": 256, "steps": 1, "seed": 1, "negative": ""}, tmp_path / "j" / "o.png",
                       {**DEFAULTS, "ltx_dir": str(ltx)}, url=broker.url)
        assert broker.calls == []
    finally:
        broker.stop()


def test_a_model_that_fails_or_makes_no_file_is_an_error_with_its_last_words_and_the_lease_goes_back(world):
    with pytest.raises(MediaError, match="did not load"):
        run(world, prompt="please fail")
    assert world["broker"].paths()[-1].endswith("/release")
    world["broker"].calls.clear()
    with pytest.raises(MediaError, match="made no file"):
        run(world, prompt="nofile here")
    assert world["broker"].paths()[-1].endswith("/release")


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def model_alive(world) -> bool:
    pid_file = world["tmp"] / "job" / "started.pid"
    return pid_file.exists() and alive(int(pid_file.read_text()))


def wait_until(condition, seconds=10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def test_a_cancel_kills_the_model_process_and_gives_the_lease_back(world):
    stop = threading.Event()
    threading.Timer(2.5, stop.set).start()
    began = time.monotonic()
    with pytest.raises(Cancelled):
        run(world, prompt="slow one", cancel=stop.is_set)
    assert time.monotonic() - began < 20
    assert wait_until(lambda: not model_alive(world))
    assert wait_until(lambda: any(p.endswith("/release") for p in world["broker"].paths()))


def test_a_model_that_goes_silent_is_ended_after_the_idle_time(world):
    began = time.monotonic()
    with pytest.raises(MediaError, match="said nothing"):
        run(world, prompt="hang forever", idle_s=3)
    assert time.monotonic() - began < 30
    assert wait_until(lambda: not model_alive(world))


def test_a_job_that_runs_too_long_is_ended(world):
    with pytest.raises(MediaError, match="took longer"):
        run(world, prompt="slow one", total_s=3)
    assert wait_until(lambda: not model_alive(world))
    assert wait_until(lambda: any(p.endswith("/release") for p in world["broker"].paths()))


def test_a_worker_that_prints_nothing_useful_is_an_error():
    with pytest.raises(MediaError, match="ended before it made a file"):
        client._run([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n')"], "image", None, None, 10, 10)


def wav_bytes(seconds, rate=24000):
    import io
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"" * int(rate * seconds))
    return out.getvalue()


def test_the_avatar_request_is_checked():
    r = limits.avatar_request("a.jpg", "b.wav", "an anchor", "480p", seed=3)
    assert r["image"] == "a.jpg" and r["audio"] == "b.wav" and r["seed"] == 3
    for bad in (lambda: limits.avatar_request("", "b.wav"), lambda: limits.avatar_request("a.jpg", None), lambda: limits.avatar_request("a.jpg", "b.wav", resolution="4k"),
                lambda: limits.avatar_request("a.jpg", "b.wav", "x" * 5000)):
        with pytest.raises(MediaError):
            bad()


def avatar_job(world, **kw):
    image, audio = world["tmp"] / "p.jpg", world["tmp"] / "a.wav"
    image.write_bytes(b"x")
    audio.write_bytes(wav_bytes(1))
    out = world["tmp"] / "job" / "out.mp4"
    return client.run("avatar", limits.avatar_request(str(image), str(audio), "an anchor", seed=4), out, world["settings"], url=world["broker"].url, **kw), out


def test_a_talking_head_asks_for_its_own_lease_size_and_runs_the_environment_of_longcat(world):
    got, out = avatar_job(world)
    assert out.read_bytes()[4:8] == b"ftyp" and got["lease"]["owner"] == "avatar" and got["lease"]["granted_mib"] == DEFAULTS["avatar_mib"]
    params = json.loads((out.parent / "params.json").read_text(encoding="utf-8"))
    assert params["mode"] == "ai2v" and params["image_path"].endswith("p.jpg") and params["audio_path"].endswith("a.wav") and params["precision"] == "int8"
    assert params["prompt"] == "an anchor" and params["resolution"] == "480p" and params["seed"] == 4 and params["output"] == str(out) and params["vocal_separation"] is False
    assert world["broker"].paths()[0] == "/api/heavy/begin"                    # not held: it asks for the swap itself


def test_a_held_job_asks_for_the_vram_only(world):
    got, out = avatar_job(world, held=True)
    assert "/api/heavy/begin" not in world["broker"].paths() and world["broker"].lease_calls()[0][1]["owner"] == "avatar" and got["lease"]["granted_mib"] == DEFAULTS["avatar_mib"]


def test_what_a_talking_head_lacks_is_told_before_the_lease(tmp_path, world):
    other = make_ltx(tmp_path / "ltx2", avatar_missing=["lcav15-int8"])
    got = client.check({**world["settings"], "ltx_dir": str(other)})
    assert not got["avatar"]["ready"] and got["avatar"]["missing"] == ["weights lcav15-int8"] and got["video"]["ready"]
    not_installed = make_ltx(tmp_path / "ltx3", avatar=False)
    got = client.check({**world["settings"], "ltx_dir": str(not_installed)})
    assert got["avatar"]["missing"] == ["the LongCat Avatar environment"]
    with pytest.raises(MediaError, match="lacks weights.*LongCat"):
        client.run("avatar", limits.avatar_request(str(tmp_path / "a.jpg"), str(tmp_path / "a.wav")), tmp_path / "j" / "o.mp4", {**world["settings"], "ltx_dir": str(not_installed)}, url=world["broker"].url)
    with pytest.raises(MediaError, match="the file"):
        client.run("avatar", limits.avatar_request(str(tmp_path / "nope.jpg"), str(tmp_path / "nope.wav")), tmp_path / "j2" / "o.mp4", world["settings"], url=world["broker"].url)
