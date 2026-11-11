"""`generate_image` and `generate_video` as the model calls them: through the registry, into a real working folder, against a stand-in LTX Studio and broker."""
import threading
import time

import pytest

from media_fake import Broker, make_ltx
from tadamoo.media import client
from tadamoo.media.settings import MediaSettings
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
    stop = threading.Event()
    register_media_tools(registry, sandbox, settings, tmp_path / "work", broker.url, cancelled=stop.is_set)
    client.forget_checks()
    yield {"registry": registry, "sandbox": sandbox, "settings": settings, "broker": broker, "tmp": tmp_path, "stop": stop, "ltx": ltx}
    broker.stop()


def call(world, name, **args):
    return world["registry"].execute(name, args)


def test_the_two_tools_are_offered_as_reversible_with_no_registry_time_limit_and_their_own_caps_in_the_text(world):
    registry = world["registry"]
    assert registry.names() == ["generate_image", "generate_video"]
    for name in registry.names():
        tool = registry.get(name)
        assert tool.effect is EffectClass.REVERSIBLE and not tool.untrusted and not tool.outward and tool.parameters["required"] == ["prompt"]
        assert registry.timeout_for(name) is None
    assert "2048" in registry.get("generate_image").parameters["properties"]["width"]["description"] and "161" in registry.get("generate_video").description
    assert "frames" in registry.get("generate_video").parameters["properties"] and "steps" not in registry.get("generate_video").parameters["properties"]


def test_an_image_is_made_into_the_working_folder_with_the_lease_it_used(world):
    got = call(world, "generate_image", prompt="a red fox in the snow", seed=11)
    assert got["status"] == "ok" and got["path"].startswith("media/image-") and got["path"].endswith("-11.png") and got["seed"] == 11
    made = world["sandbox"].root / got["path"]
    assert made.read_bytes()[:4] == b"\x89PNG" and got["bytes"] == made.stat().st_size
    assert got["lease"]["owner"] == "image" and got["lease"]["granted_mib"] == 11000 and got["seconds"] >= 0
    assert (got["width"], got["height"], got["steps"]) == (1024, 1024, 20) and f"tadamoo-path:{got['path']}" in got["show"] and got["show"].count("![") == 1
    assert world["sandbox"].changes[-1]["file"] == got["path"] and world["sandbox"].changes[-1]["kind"] == "added"
    assert not list((world["tmp"] / "work").glob("*"))
    assert world["broker"].paths()[-1].endswith("/release")


def test_a_video_is_made_with_its_caps_and_says_what_it_rounded(world):
    got = call(world, "generate_video", prompt="a fox runs through the snow", frames=70, path="clips/fox.mp4")
    assert got["status"] == "ok" and got["path"] == "clips/fox.mp4" and (got["frames"], got["fps"], got["width"], got["height"]) == (65, 25, 704, 512)
    assert got["adjusted"] and got["lease"]["owner"] == "video" and got["lease"]["granted_mib"] == 25000 and got["show"].startswith("show it in the answer with [fox.mp4]")
    assert (world["sandbox"].root / "clips" / "fox.mp4").read_bytes()[4:8] == b"ftyp"


def test_a_value_over_a_cap_is_refused_without_asking_for_the_gpu(world):
    for name, args in (("generate_image", {"width": 4096}), ("generate_image", {"steps": 500}), ("generate_video", {"frames": 1000}), ("generate_video", {"width": 1280, "height": 1280}),
                       ("generate_image", {"prompt": ""})):
        got = call(world, name, **{"prompt": "x", **args})
        assert got["status"] == "error" and got["note"]
    assert world["broker"].calls == []


def test_a_path_never_replaces_a_file_and_stays_in_the_folder_and_keeps_the_extension(world):
    (world["sandbox"].root / "mine.png").write_bytes(b"keep me")
    assert "already exists" in call(world, "generate_image", prompt="x", path="mine.png")["note"]
    assert (world["sandbox"].root / "mine.png").read_bytes() == b"keep me"
    assert "must end in .mp4" in call(world, "generate_video", prompt="x", path="v.png")["note"]
    with pytest.raises(PermissionError):
        call(world, "generate_image", prompt="x", path="../outside.png")
    assert world["broker"].calls == []


def test_when_the_server_cannot_lend_the_vram_the_answer_is_refused_with_the_reason_and_nothing_is_made(world):
    world["broker"].answer = (409, {"ok": False, "reason": "not_enough", "message": "only 3 GB can be freed", "message_it": "si liberano solo 3 GB"})
    got = call(world, "generate_video", prompt="a fox")
    assert got["status"] == "refused" and got["reason"] == "not_enough" and "3 GB" in got["note"] and "Tell the person" in got["note"]
    assert not (world["sandbox"].root / "media").exists() and world["sandbox"].changes == []


def test_off_or_missing_weights_are_told_before_anything_runs(world, tmp_path):
    world["settings"].write({"enabled": False})
    assert call(world, "generate_image", prompt="x")["status"] == "unavailable"
    world["settings"].write({"enabled": True, "ltx_dir": str(make_ltx(tmp_path / "other", missing=["qwen21-vae"]))})
    client.forget_checks()
    got = call(world, "generate_image", prompt="x")
    assert got["status"] == "unavailable" and "qwen21-vae" in got["note"]
    assert call(world, "generate_video", prompt="x")["status"] == "ok"
    assert world["broker"].paths().count("/api/vram/lease") == 1


def test_stop_ends_the_job_and_keeps_nothing(world):
    threading.Timer(2.5, world["stop"].set).start()
    got = call(world, "generate_image", prompt="slow one")
    assert got["status"] == "cancelled" and not (world["sandbox"].root / "media").exists()
    deadline = time.monotonic() + 10
    while not world["broker"].paths()[-1].endswith("/release") and time.monotonic() < deadline:
        time.sleep(0.2)
    assert world["broker"].paths()[-1].endswith("/release")


def test_a_failed_job_says_why_and_where_its_working_files_are(world):
    got = call(world, "generate_image", prompt="please fail")
    assert got["status"] == "error" and "did not load" in got["note"] and str(world["tmp"] / "work") in got["note"]
    assert not (world["sandbox"].root / "media").exists()
