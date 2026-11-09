"""The engines at the gateway: each device gets the engine the person chose for its kind of device first among the modes it declares, `ready` and
`render_mode` name the engine and the output height, a device may declare its own, and the emergency video stays last. Fake WebSocket and host."""
import json

from callbridge import voice_gateway as vg
from voice.avatar_engines import registry, selection
from voice.tests.test_gateway import CLOSED, TOKEN
from voice.tests.test_gateway_render_modes import FakeWs, auth, connect, host_with

BOTH = {"avatar": "ada", "render_modes": ["wav2lip", "visemes"]}


def configured(host, **chosen):
    sel = selection.parse({"engines": chosen.get("engines", {}), "resolution": chosen.get("resolution", {})})
    host.cfg.avatar_engines, host.cfg.avatar_resolution = dict(sel.engines), dict(sel.resolution)
    return host


def ready_of(host, *modes, device="android", **extra):
    first = {**auth(*modes, device=device), **extra}
    return connect(host, first, [CLOSED]).json("ready")[0]


def test_the_default_android_device_gets_wav2lip_first_with_its_engine_and_height():
    ready = ready_of(configured(host_with(BOTH)), "visemes", "wav2lip")
    assert ready["render_modes"] == ["wav2lip", "visemes"] and ready["render_mode"] == "wav2lip"
    assert (ready["engine"], ready["resolution"]) == ("wav2lip_gan96", 96)


def test_the_person_can_choose_visemes_for_the_phone_and_it_goes_first_even_when_the_device_listed_wav2lip_first():
    host = configured(host_with(BOTH), engines={"android": "visemes"}, resolution={"android": 144})
    ready = ready_of(host, "wav2lip", "visemes")
    assert ready["render_modes"] == ["visemes", "wav2lip"] and (ready["engine"], ready["resolution"]) == ("visemes", 144)


def test_the_esp32_gets_visemes_and_each_device_kind_has_its_own_choice():
    host = configured(host_with(BOTH), engines={"esp32": "visemes", "android": "wav2lip_gan96"}, resolution={"esp32": 96})
    ready = ready_of(host, "visemes", device="esp32")
    assert (ready["render_mode"], ready["engine"], ready["resolution"]) == ("visemes", "visemes", 96)
    ready = ready_of(host, "visemes", "wav2lip", device="android")
    assert (ready["render_mode"], ready["engine"]) == ("wav2lip", "wav2lip_gan96")


def test_a_configured_engine_the_device_did_not_declare_or_the_avatar_cannot_serve_changes_nothing():
    host = configured(host_with({"avatar": "ada", "render_modes": ["visemes"]}), engines={"android": "wav2lip_gan96"})
    ready = ready_of(host, "wav2lip", "visemes")                                    # no wav2lip files: visemes
    assert ready["render_modes"] == ["visemes"] and ready["engine"] == "visemes"
    ready = ready_of(configured(host_with(BOTH), engines={"android": "wav2lip_gan96"}), "visemes")        # the device does not draw wav2lip
    assert ready["render_modes"] == ["visemes"] and ready["engine"] == "visemes"


def test_the_emergency_video_stays_last_whatever_the_choice_is():
    host = configured(host_with(BOTH, avatar=True), engines={"pc": "musetalk_15", "android": "wav2lip_gan96"})
    ready = ready_of(host, "video", "visemes", "wav2lip", device="pc")
    assert ready["render_modes"] == ["visemes", "wav2lip", "video"] and ready["engine"] == "visemes"
    ready = ready_of(host, "video", device="pc")
    assert ready["render_modes"] == ["video"] and (ready["engine"], ready["resolution"]) == ("musetalk_15", 480)


def test_an_engine_that_is_not_in_this_build_serves_the_integrated_one_of_its_mode():
    host = configured(host_with(BOTH), engines={"android": "wav2lip_v288"})
    ready = ready_of(host, "visemes", "wav2lip")
    assert ready["render_mode"] == "wav2lip" and (ready["engine"], ready["resolution"]) == ("wav2lip_gan96", 96)


def test_a_device_may_declare_its_own_engine_and_height_when_they_are_valid_for_it():
    host = configured(host_with(BOTH), engines={"android": "wav2lip_gan96"})
    ready = ready_of(host, "wav2lip", "visemes", engine="visemes", resolution=192)
    assert ready["render_mode"] == "visemes" and (ready["engine"], ready["resolution"]) == ("visemes", 192)
    ready = ready_of(host, "wav2lip", "visemes", engine="musetalk_15", resolution=7)                   # does not run on a phone: the person's choice stands
    assert (ready["engine"], ready["resolution"]) == ("wav2lip_gan96", 96)
    ready = ready_of(host, "wav2lip", "visemes", engine="wav2lip_gan96", resolution=77)                  # a height it does not offer: the default
    assert (ready["engine"], ready["resolution"]) == ("wav2lip_gan96", 96)
    ready = ready_of(host, "wav2lip", "visemes", engine=7, resolution="x")
    assert (ready["engine"], ready["resolution"]) == ("wav2lip_gan96", 96)


def test_nothing_configured_keeps_the_devices_own_order_and_still_names_the_engine():
    host = host_with(BOTH)                                                           # the fake host's config has no engine fields at all
    ready = ready_of(host, "visemes", "wav2lip")
    assert ready["render_modes"] == ["visemes", "wav2lip"] and ready["engine"] == "visemes"
    host.cfg.avatar_engines = {}
    assert ready_of(host, "visemes", "wav2lip")["render_modes"] == ["visemes", "wav2lip"]
    ready = ready_of(host, "wav2lip", device="toaster")
    assert (ready["render_mode"], ready["engine"]) == ("wav2lip", "wav2lip_gan96")


def test_voice_only_has_no_engine():
    ready = ready_of(configured(host_with({"avatar": "", "render_modes": []})), "wav2lip", "visemes")
    assert (ready["render_mode"], ready["engine"], ready["resolution"]) == ("none", "", 0)
    assert ready_of(host_with(), device="android")["engine"] == ""


def test_the_render_mode_messages_carry_the_engine_and_the_height():
    host = configured(host_with(BOTH), engines={"android": "wav2lip_gan96"}, resolution={"android": 192})
    ws = connect(host, auth("wav2lip", "visemes"), [{"type": "render_mode", "mode": "visemes"}, {"type": "render_failed", "mode": "wav2lip", "why": "onnx_load_failed"}, CLOSED])
    answers = ws.json("render_mode")
    assert (answers[0]["mode"], answers[0]["engine"], answers[0]["resolution"]) == ("visemes", "visemes", 96)
    assert (answers[1]["mode"], answers[1]["previous"], answers[1]["engine"]) == ("visemes", "wav2lip", "visemes")
    host = configured(host_with(BOTH), engines={"android": "wav2lip_gan96"}, resolution={"android": 192})
    ws = connect(host, auth("wav2lip", "visemes"), [{"type": "render_failed", "mode": "wav2lip", "why": "x"}, {"type": "render_failed", "mode": "visemes", "why": "y"}, CLOSED])
    last = ws.json("render_mode")[-1]
    assert (last["mode"], last["engine"], last["resolution"]) == ("none", "", 0)


def test_an_unknown_field_in_auth_is_tolerated_and_the_ready_message_keeps_its_old_fields():
    ws = FakeWs([{"type": "auth", "token": TOKEN, "render_modes": ["visemes"], "device": "esp32", "future": [1, 2]}, CLOSED])
    vg.Connection(ws, configured(host_with(BOTH)), authed=True).run()
    ready = json.loads(next(m for m in ws.sent if isinstance(m, str) and json.loads(m)["type"] == "ready"))
    assert {"voice", "missing", "avatar", "brain", "persona", "render_mode", "render_modes", "avatar_name"} <= set(ready)
    assert registry.info_of(ready["engine"]) is not None
