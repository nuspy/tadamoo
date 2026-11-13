"""`modify_video`: a change made to what a video shows, through the worker against a stand-in LTX Studio (its editing LoRAs, its tracking and its LTX 2.3 checkpoints are stand-ins that do no work)
and a stand-in broker. What is checked is what the job is given: the checkpoint, the LoRA, the tracked object, the segments and the sound put back; nothing here touches a GPU."""
import json
import threading

import pytest

from media_fake import Broker, make_ltx
from tadamoo.media import client, limits
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import DEFAULTS, MediaSettings
from tadamoo.media.tools import register_media_tools
from tadamoo.tools.filesystem import WorkspaceSandbox
from tadamoo.tools.registry import EffectClass, ToolRegistry


@pytest.fixture
def world(tmp_path):
    ltx = make_ltx(tmp_path / "ltx")
    broker = Broker()
    settings = {**DEFAULTS, "ltx_dir": str(ltx), "wait_s": 5, "idle_s": 30}
    client.forget_checks()
    video = tmp_path / "src" / "street.mp4"
    video.parent.mkdir()
    video.write_bytes(b"frames=97 audio=1")
    (tmp_path / "src" / "ferrari.png").write_bytes(b"png")
    yield {"ltx": ltx, "broker": broker, "settings": settings, "tmp": tmp_path, "video": video, "photo": tmp_path / "src" / "ferrari.png"}
    broker.stop()


def modify(world, tag="job", **kw):
    args = {"video": str(world["video"]), "prompt": "a red Ferrari drives along the street", "mode": "replace", "obj": "yellow Fiat 500", "seed": 3, **kw}
    request = limits.modify_request(**args)
    out = world["tmp"] / tag / "out.mp4"
    got = client.run("modify", {k: v for k, v in request.items() if k != "notes"}, out, world["settings"], url=world["broker"].url)
    return got, out, json.loads((out.parent / "params.json").read_text(encoding="utf-8"))


def test_the_request_has_its_defaults_and_refuses_what_does_not_belong_to_the_mode():
    r = limits.modify_request("a.mp4", "a red Ferrari", "replace", "yellow Fiat 500", seed=2)
    assert (r["strength"], r["guide"], r["grow"], r["object"], r["reference"], r["seconds_max"]) == (0.8, 0.4, 24, "yellow Fiat 500", "", 20)
    assert limits.modify_request("a.mp4", "make it night", "edit")["strength"] == 1.0
    assert limits.modify_request("a.mp4", "", "restyle", reference="b.png")["prompt"] == ""
    for bad in (lambda: limits.modify_request("a.mp4", "x", "replace"), lambda: limits.modify_request("a.mp4", "", "replace", "car"), lambda: limits.modify_request("a.mp4", "x", "edit", "car"),
                lambda: limits.modify_request("a.mp4", "x", "edit", reference="b.png"), lambda: limits.modify_request("a.mp4", "x", "restyle"), lambda: limits.modify_request("a.mp4", "x", "paint"),
                lambda: limits.modify_request("a.mp4", "x", "edit", guide=0.5), lambda: limits.modify_request("a.mp4", "x", "edit", grow=3), lambda: limits.modify_request("a.mp4", "x", "replace", "car", strength=2),
                lambda: limits.modify_request("a.mp4", "x", "replace", "car", grow=100), lambda: limits.modify_request("", "x", "replace", "car"), lambda: limits.modify_request("a.mp4", "x" * 5000, "edit")):
        with pytest.raises(MediaError):
            bad()


def test_the_check_says_what_the_kind_needs(world, tmp_path):
    got = client.check(world["settings"])
    assert got["modify"]["ready"] and got["modify"]["python_ok"] and not got["modify"]["missing"]
    no_checkpoint = make_ltx(tmp_path / "ltx2", edit_checkpoint=False)
    assert client.check({**world["settings"], "ltx_dir": str(no_checkpoint)}, force=True)["modify"]["missing"] == ["checkpoint ltx-2.3-22b-dev-fp8.safetensors (an LTX 2.3 dev or distilled one)"]
    no_tracker = make_ltx(tmp_path / "ltx3", missing=["sam2-hiera-large"])
    assert client.check({**world["settings"], "ltx_dir": str(no_tracker)}, force=True)["modify"]["missing"] == ["weights sam2-hiera-large"]
    no_lora = make_ltx(tmp_path / "ltx4", edit_loras=("edit",))
    assert "t2v" in client.check({**world["settings"], "ltx_dir": str(no_lora)}, force=True)["modify"]["missing"][0]


def test_a_fine_tune_is_never_taken_unless_the_settings_name_it(world):
    got, out, params = modify(world)
    assert params["model"].endswith("ltx-2.3-22b-dev-fp8.safetensors") and params["ltx_release"] == "2.3"
    world["settings"]["edit_model"] = "PinkCherry_FineTune_fp8scaled_v1_8_LTX23.safetensors"
    client.forget_checks()
    got, out, params = modify(world, "named")
    assert params["model"].endswith("PinkCherry_FineTune_fp8scaled_v1_8_LTX23.safetensors")
    world["settings"]["edit_model"] = "nope.safetensors"
    with pytest.raises(MediaError, match="nope.safetensors"):
        modify(world, "none")


def test_a_replace_tracks_the_object_in_a_copy_and_puts_the_sound_back(world):
    got, out, params = modify(world)
    assert got["lease"]["owner"] == "modify" and got["lease"]["granted_mib"] == DEFAULTS["modify_mib"]
    assert out.read_bytes().endswith(b"AUDIO") and got["bytes"] == out.stat().st_size
    assert params["edit_lora"].endswith("t2v_rank128_v1_10000steps.safetensors") and "image_path" not in params and params["prompt"] == "a red Ferrari drives along the street"
    assert (params["width"], params["height"], params["num_frames"], params["fps"], params["mask_dilate"], params["control_strength"]) == (1920, 1088, 97, 25, 24, 0.4)
    assert params["control_video"] == str(out.parent / "source.mp4") and params["inpaint_masks_dir"].endswith("source_track")
    assert (out.parent / "source_tracked.txt").read_text() == "yellow Fiat 500 24"
    assert not [p for p in world["video"].parent.iterdir() if p.name not in ("street.mp4", "ferrari.png")]            # nothing is left beside the original
    assert world["video"].read_bytes() == b"frames=97 audio=1"


def test_a_replace_from_a_photograph_uses_the_lora_that_takes_one(world):
    got, out, params = modify(world, reference=str(world["photo"]), strength=0.6, grow=10)
    assert params["edit_lora"].endswith("r2v_rank32_v1_3000steps.safetensors") and params["image_path"] == str(world["photo"]) and params["ref_strength"] == 0.6 and params["mask_dilate"] == 10
    no_r2v = make_ltx(world["tmp"] / "ltx5", edit_loras=("t2v",))
    with pytest.raises(MediaError, match="weight missing"):
        client.run("modify", {k: v for k, v in limits.modify_request(str(world["video"]), "x", "replace", "car", str(world["photo"])).items() if k != "notes"}, world["tmp"] / "j5" / "o.mp4",
                   {**world["settings"], "ltx_dir": str(no_r2v)}, url=world["broker"].url)


def test_an_edit_of_the_whole_video_keeps_the_size_within_the_cap_and_cuts_a_long_clip_in_segments(world):
    got, out, params = modify(world, mode="edit", obj="", prompt="the same street at night", strength=0.9)
    assert params["edit_lora"].endswith("edit_anything_global_rank128_v1_9000steps_adamw.safetensors") and (params["width"], params["height"]) == (1280, 704) and params["control_strength"] == 0.9
    assert params["num_frames"] == 97 and "chunk_frames" not in params and out.read_bytes().endswith(b"AUDIO")
    world["video"].write_bytes(b"frames=401 audio=0")
    got, out, params = modify(world, "long", mode="edit", obj="", prompt="the same street at night")
    assert params["num_frames"] == 401 and (params["chunk_frames"], params["overlap_frames"]) == (121, 9) and not out.read_bytes().endswith(b"AUDIO")


def test_a_restyle_takes_the_look_of_the_picture_and_the_sound_comes_back(world):
    got, out, params = modify(world, mode="restyle", obj="", prompt="", reference=str(world["photo"]))
    assert params["control_type"] == "restyle" and params["ic_frame0_image"] == str(world["photo"]) and params["control_video"].endswith("source.mp4") and out.read_bytes().endswith(b"AUDIO")


def test_a_clip_that_is_too_long_or_missing_is_refused_before_the_gpu_is_asked_for(world):
    world["video"].write_bytes(b"frames=600 audio=1")
    with pytest.raises(MediaError, match="at most 20 seconds"):
        modify(world)
    assert world["broker"].calls == []
    with pytest.raises(MediaError, match="the video"):
        modify(world, "gone", video=str(world["tmp"] / "none.mp4"))
    assert world["broker"].calls == []


@pytest.fixture
def tool(tmp_path):
    ltx = make_ltx(tmp_path / "ltx")
    broker = Broker()
    settings = MediaSettings(tmp_path / "media.json")
    settings.write({"ltx_dir": str(ltx), "wait_s": 5, "idle_s": 60})
    sandbox = WorkspaceSandbox(tmp_path / "ws")
    registry = ToolRegistry()
    register_media_tools(registry, sandbox, settings, tmp_path / "work", broker.url, cancelled=threading.Event().is_set, kinds=("modify",))
    client.forget_checks()
    (sandbox.root / "street.mp4").write_bytes(b"frames=65 audio=1")
    (sandbox.root / "ferrari.png").write_bytes(b"png")
    yield {"registry": registry, "sandbox": sandbox, "broker": broker, "settings": settings, "tmp": tmp_path}
    broker.stop()


def test_the_tool_is_offered_as_reversible_and_changes_a_copy(tool):
    registry = tool["registry"]
    assert registry.names() == ["modify_video"] and registry.get("modify_video").effect is EffectClass.REVERSIBLE and registry.timeout_for("modify_video") is None
    assert registry.get("modify_video").parameters["required"] == ["path"] and "20 seconds" in registry.get("modify_video").description
    got = registry.execute("modify_video", {"path": "street.mp4", "prompt": "a red Ferrari", "target": "yellow Fiat 500", "reference": "ferrari.png", "seed": 4})
    assert got["status"] == "ok" and got["path"].startswith("media/street-replace-") and got["path"].endswith("-4.mp4") and got["lease"]["owner"] == "modify"
    assert (tool["sandbox"].root / got["path"]).read_bytes().endswith(b"AUDIO") and (tool["sandbox"].root / "street.mp4").read_bytes() == b"frames=65 audio=1"
    assert not list((tool["tmp"] / "work").glob("*")) and tool["broker"].paths()[-1].endswith("/release")


def test_the_tool_says_what_is_wrong_without_asking_for_the_gpu(tool):
    registry = tool["registry"]
    assert "reference" in registry.execute("modify_video", {"path": "street.mp4", "mode": "restyle"})["note"]
    assert "object" in registry.execute("modify_video", {"path": "street.mp4", "prompt": "x", "mode": "edit", "target": "car"})["note"]
    assert "not a file" in registry.execute("modify_video", {"path": "gone.mp4", "prompt": "x", "target": "car"})["note"]
    tool["settings"].write({"edit_model": "nope.safetensors"})
    client.forget_checks()
    got = registry.execute("modify_video", {"path": "street.mp4", "prompt": "x", "target": "car"})
    assert got["status"] == "unavailable" and "checkpoint nope.safetensors" in got["note"] and tool["broker"].calls == []
