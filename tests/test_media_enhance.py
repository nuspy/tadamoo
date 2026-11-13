"""Better videos and videos from a picture: the request limits, the two runners (frames in between, a larger picture) run by the worker against a stand-in LTX Studio and broker,
and `enhance_video`, `generate_video(image=...)` and `generate_image(refs=...)` as the model calls them. No GPU, no model."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from media_fake import MP4, PNG, Broker, make_ltx
from tadamoo.media import client, limits, worker
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import DEFAULTS, MediaSettings
from tadamoo.media.tools import register_media_tools
from tadamoo.tools.filesystem import WorkspaceSandbox
from tadamoo.tools.registry import EffectClass, ToolRegistry


@pytest.fixture
def world(tmp_path):
    ltx = make_ltx(tmp_path / "ltx")
    broker = Broker()
    settings = MediaSettings(tmp_path / "media.json")
    settings.write({"ltx_dir": str(ltx), "wait_s": 5, "idle_s": 60})
    sandbox = WorkspaceSandbox(tmp_path / "ws")
    registry = ToolRegistry()
    register_media_tools(registry, sandbox, settings, tmp_path / "work", broker.url, cancelled=threading.Event().is_set, kinds=("image", "video", "enhance"))
    (sandbox.root / "in").mkdir()
    (sandbox.root / "in" / "clip.mp4").write_bytes(MP4)
    (sandbox.root / "in" / "photo.png").write_bytes(PNG)
    client.forget_checks()
    yield {"registry": registry, "sandbox": sandbox, "settings": settings, "broker": broker, "tmp": tmp_path, "ltx": ltx}
    broker.stop()


def call(world, name, **args):
    return world["registry"].execute(name, args)


def test_the_enhance_request_wants_a_size_or_a_frame_rate_and_keeps_to_its_caps():
    r = limits.enhance_request("a.mp4", scale=2, factor=2)
    assert (r["video"], r["scale"], r["factor"], r["mode"]) == ("a.mp4", 2.0, 2, "turbo")
    assert limits.enhance_request("a.mp4", factor=4)["scale"] == 0.0 and limits.enhance_request("a.mp4", scale=1.5)["factor"] == 0
    for bad in (lambda: limits.enhance_request("a.mp4"), lambda: limits.enhance_request("a.mp4", scale=5), lambda: limits.enhance_request("a.mp4", scale=0.5),
                lambda: limits.enhance_request("a.mp4", factor=1), lambda: limits.enhance_request("a.mp4", factor=5), lambda: limits.enhance_request("a.mp4", factor=True),
                lambda: limits.enhance_request("", scale=2), lambda: limits.enhance_request("a.mp4", scale=2, mode="fast")):
        with pytest.raises(MediaError):
            bad()


def test_the_video_request_carries_the_first_picture_when_there_is_one():
    assert limits.video_request("a fox")["image"] == "" and limits.video_request("a fox", image=" p.png ")["image"] == "p.png"
    with pytest.raises(MediaError):
        limits.video_request("a fox", image=5)


def test_the_worker_gives_the_frame_counter_of_the_interpolation_runner_as_progress(world, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(MP4)
    seen = []
    got = client.run("interpolate", {"video": str(src), "factor": 3}, tmp_path / "job" / "out.mp4", world["settings"].read(), url=world["broker"].url, progress=seen.append)
    assert got["lease"]["granted_mib"] == DEFAULTS["interpolate_mib"] and (tmp_path / "job" / "out.mp4").read_bytes().endswith(b"xxx")
    assert (tmp_path / "job" / "out.mp4.args").read_text() == f"{src} {tmp_path / 'job' / 'out.mp4'} 3"
    assert [(e["step"], e["steps"]) for e in seen if e["event"] == "progress"] == [(20, 40), (40, 40)]


def test_the_upscale_runner_gets_the_scale_the_mode_and_the_folders_of_the_enlarger(world, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(MP4)
    got = client.run("upscale", {"video": str(src), "scale": 1.5, "mode": "base"}, tmp_path / "job" / "out.mp4", world["settings"].read(), url=world["broker"].url)
    args = (tmp_path / "job" / "out.mp4.args").read_text().split()
    assert got["lease"]["granted_mib"] == DEFAULTS["upscale_mib"] and args[2:6] == ["--mode", "base", "--scale", "1.5"]
    assert args[args.index("--code-dir") + 1] == str(world["ltx"] / "code") and args[args.index("--ckpt-root") + 1] == str(world["ltx"] / "ckpt")


def test_the_check_says_what_the_enhancers_lack(world, tmp_path):
    got = client.check(world["settings"].read(), force=True)
    assert got["upscale"]["ready"] and got["interpolate"]["ready"]
    other = make_ltx(tmp_path / "ltx2", rife=False, scaler_missing=["FlashVSR+ weights missing"])
    got = client.check({**world["settings"].read(), "ltx_dir": str(other)}, force=True)
    assert not got["interpolate"]["ready"] and got["interpolate"]["missing"][0].startswith("RIFE") and got["upscale"]["missing"] == ["FlashVSR+ weights missing"]


def test_the_presence_of_weights_is_asked_of_the_ltx_registry(world):
    done = subprocess.run([sys.executable, str(worker.__file__), "--present", "rife-flownet-v4,absent-one", "--ltx-dir", str(world["ltx"])], capture_output=True, text=True)
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"rife-flownet-v4": True, "absent-one": False}


def test_a_video_is_enlarged_first_and_then_given_frames_and_both_jobs_took_their_own_lease(world):
    got = call(world, "enhance_video", path="in/clip.mp4", scale=2, factor=2)
    assert got["status"] == "ok" and got["steps"] == ["upscale", "interpolate"] and got["path"].startswith("media/clip-enhanced-") and got["path"].endswith(".mp4")
    made = (world["sandbox"].root / got["path"]).read_bytes()
    assert made == MP4 + b"y" + b"xx"                                   # the enlarger ran on the original, the frame maker on the enlarged copy
    assert [c[1]["owner"] for c in world["broker"].lease_calls()] == ["upscale", "interpolate"]
    assert (world["sandbox"].root / "in" / "clip.mp4").read_bytes() == MP4 and not list((world["tmp"] / "work").glob("*"))
    assert world["sandbox"].changes[-1]["file"] == got["path"]


def test_only_what_was_asked_runs(world):
    got = call(world, "enhance_video", path="in/clip.mp4", factor=2, output="out/smooth.mp4")
    assert got["status"] == "ok" and got["steps"] == ["interpolate"] and got["path"] == "out/smooth.mp4" and got["scale"] is None
    assert [c[1]["owner"] for c in world["broker"].lease_calls()] == ["interpolate"]


def test_enhancing_says_why_it_cannot(world, tmp_path):
    assert call(world, "enhance_video", path="in/clip.mp4")["status"] == "error"
    assert call(world, "enhance_video", path="in/none.mp4", scale=2)["status"] == "error"
    other = make_ltx(tmp_path / "ltx2", rife=False)
    world["settings"].write({"ltx_dir": str(other)})
    client.forget_checks()
    got = call(world, "enhance_video", path="in/clip.mp4", factor=2)
    assert got["status"] == "unavailable" and "RIFE" in got["note"] and not world["broker"].lease_calls()


def test_a_failed_job_leaves_its_files_and_the_original(world):
    (world["sandbox"].root / "in" / "fail.mp4").write_bytes(MP4)
    got = call(world, "enhance_video", path="in/fail.mp4", factor=2)
    assert got["status"] == "error" and "rife failed" in got["note"] and (world["sandbox"].root / "in" / "fail.mp4").is_file()


def test_the_tool_is_offered_with_its_caps_and_is_reversible(world):
    tool = world["registry"].get("enhance_video")
    assert tool.effect is EffectClass.REVERSIBLE and tool.parameters["required"] == ["path"] and "4" in tool.parameters["properties"]["scale"]["description"]
    assert "refs" in world["registry"].get("generate_image").parameters["properties"] and "image" in world["registry"].get("generate_video").parameters["properties"]


def test_a_clip_starts_from_a_picture_of_the_working_folder_and_edits_take_their_pictures_from_it(world):
    got = call(world, "generate_video", prompt="the fox runs", image="in/photo.png", seed=3)
    assert got["status"] == "ok"
    assert call(world, "generate_video", prompt="the fox runs", image="in/none.png")["status"] == "error"
    got = call(world, "generate_image", prompt="image 1, in the snow", refs=["in/photo.png"], seed=4)
    assert got["status"] == "ok"
    assert call(world, "generate_image", prompt="image 1, in the snow", refs=["in/none.png"])["status"] == "error"


def test_the_picture_reaches_the_video_worker_as_its_first_picture(world, tmp_path):
    photo = tmp_path / "p.png"
    photo.write_bytes(PNG)
    request = {"prompt": "a fox", "width": 704, "height": 512, "frames": 65, "fps": 25, "seed": 1, "negative": ""}
    client.run("video", {**request, "image": str(photo)}, tmp_path / "a" / "out.mp4", world["settings"].read(), url=world["broker"].url)
    assert json.loads((tmp_path / "a" / "params.json").read_text())["image_path"] == str(photo)
    client.run("video", request, tmp_path / "b" / "out.mp4", world["settings"].read(), url=world["broker"].url)
    assert json.loads((tmp_path / "b" / "params.json").read_text())["image_path"] == ""
    with pytest.raises(MediaError, match="picture"):
        client.run("video", {**request, "image": str(tmp_path / "none.png")}, tmp_path / "c" / "out.mp4", world["settings"].read(), url=world["broker"].url)
