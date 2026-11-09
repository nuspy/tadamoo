"""The choice of engine and output height per target: reading a stored value never fails, a request that the engine cannot honour is refused,
voice.json reaches the service's Config, and the server's copy of the table agrees with the registry."""
import json

import pytest

from callbridge.config import Config
from voice import settings_bridge as sb
from voice.avatar_engines import registry, selection
from voice.avatar_engines.base import TARGETS
from voice.avatar_engines.selection import SelectionError


def test_nothing_stored_is_the_defaults_of_every_target():
    got = selection.parse(None)
    assert got.engines == registry.DEFAULT_ENGINES and got.resolution == registry.DEFAULT_RESOLUTION and got.fps_hint == 25
    assert selection.parse({}) == selection.parse("x") == selection.parse([1]) == got


def test_each_damaged_value_takes_its_default_and_the_rest_is_kept():
    got = selection.parse({"engines": {"pc": "er_nerf", "web": "er_nerf", "android": 5, "esp32": "wav2lip_gan96", "moon": "visemes"},
                           "resolution": {"pc": 384, "web": 7, "android": True, "esp32": "96"}, "fps_hint": 99})
    assert got.engines == {"pc": "er_nerf", "web": "visemes", "android": "wav2lip_gan96", "esp32": "visemes"}       # web and esp32 cannot run those
    assert got.resolution == {"pc": 384, "web": 240, "android": 96, "esp32": 96} and got.fps_hint == 25
    assert selection.parse({"fps_hint": 60}).fps_hint == 60 and selection.parse({"fps_hint": 4}).fps_hint == 25 and selection.parse({"fps_hint": True}).fps_hint == 25


def test_a_height_the_engine_does_not_offer_becomes_the_targets_default_then_the_engines_own():
    assert selection.parse({"engines": {"web": "visemes"}, "resolution": {"web": 100}}).resolution["web"] == 240
    got = selection.parse({"engines": {"pc": "sadtalker"}, "resolution": {"pc": 480}})                          # sadtalker offers 256, 384, 512 and the target default is 480
    assert got.resolution["pc"] == 256


def test_an_engine_that_does_not_run_on_the_target_is_refused_with_both_languages():
    for target, engine in (("android", "musetalk_15"), ("esp32", "wav2lip_gan96"), ("web", "sadtalker"), ("pc", "no_such"), ("moon", "visemes"), ("pc", 3)):
        with pytest.raises(SelectionError) as e:
            selection.select({}, target, engine)
        assert e.value.code in ("engine", "target") and e.value.en and e.value.it


def test_a_height_the_engine_does_not_offer_is_refused():
    for height in (100, 0, True, "96", 97.0 + 0.5):
        with pytest.raises(SelectionError) as e:
            selection.select({}, "android", "wav2lip_gan96", height)
        assert e.value.code == "resolution"
    assert selection.select({}, "android", "wav2lip_gan96", 192).resolution["android"] == 192


def test_choosing_an_engine_keeps_the_height_when_the_new_engine_offers_it_else_takes_a_default():
    stored = {"engines": {"android": "visemes"}, "resolution": {"android": 192}}
    assert selection.select(stored, "android", "wav2lip_gan96").resolution["android"] == 192
    assert selection.select(stored, "android", "feathertalk").resolution["android"] == 192
    stored = {"engines": {"pc": "visemes"}, "resolution": {"pc": 240}}
    assert selection.select(stored, "pc", "musetalk_15").resolution["pc"] == 480
    assert selection.select(stored, "pc", "sadtalker").resolution["pc"] == 256


def test_what_a_device_gets_is_its_own_declaration_else_the_choice_for_its_kind_of_device():
    sel = selection.parse({"engines": {"android": "visemes"}, "resolution": {"android": 144}})
    info, height = selection.wanted(sel, "android")
    assert (info.id, height) == ("visemes", 144)
    info, height = selection.wanted(sel, "android", "wav2lip_gan96", 192)
    assert (info.id, height) == ("wav2lip_gan96", 192)
    info, height = selection.wanted(sel, "android", "wav2lip_gan96", 77)                                            # a height the engine does not offer is not used
    assert (info.id, height) == ("wav2lip_gan96", None)
    assert selection.wanted(sel, "android", "sadtalker", 256)[0].id == "visemes"                                    # it does not run on a phone
    assert selection.wanted(sel, "toaster") == (None, None) and selection.wanted(None, "android") == (None, None)
    assert selection.wanted(None, "toaster", "wav2lip_gan96", 96)[0].id == "wav2lip_gan96"


def test_the_engine_that_draws_a_mode_is_the_configured_one_when_it_is_integrated_else_the_one_of_the_mode():
    v288 = registry.info_of("wav2lip_v288")
    assert selection.for_mode("wav2lip", v288, 288, "android") == ("wav2lip_gan96", 96)                             # not integrated: the integrated Wav2Lip is served
    gan = registry.info_of("wav2lip_gan96")
    assert selection.for_mode("wav2lip", gan, 192, "android") == ("wav2lip_gan96", 192)
    assert selection.for_mode("visemes", gan, 192, "android") == ("visemes", 96)                                    # a fall-back takes the target's default height
    assert selection.for_mode("video", None, None, "pc") == ("musetalk_15", 480)
    assert selection.for_mode("visemes", None, None, "") == ("visemes", registry.info_of("visemes").default_resolution)


# ---------------------------------------------------------------------------------------- voice.json -> Config
def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_voice_json_reaches_the_service_config(tmp_path):
    s = sb.load(write(tmp_path / "voice.json", {"avatar": {"engines": {"android": "visemes", "pc": "sadtalker"}, "resolution": {"android": 144}, "fps_hint": 30}}))
    config = Config()
    applied = sb.apply(config, s)
    assert config.avatar_engines["android"] == "visemes" and config.avatar_engines["pc"] == "sadtalker" and config.avatar_engines["esp32"] == "visemes"
    assert config.avatar_resolution["android"] == 144 and config.avatar_fps_hint == 30
    assert {"avatar_engines", "avatar_resolution", "avatar_fps_hint"} <= set(applied)


def test_a_missing_or_damaged_file_gives_the_defaults_here_too(tmp_path):
    for data in (None, "{", {"avatar": 5}, {"avatar": {"engines": "x"}}, {"avatar": {"engines": {"android": "nope"}}}):
        path = tmp_path / "voice.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8") if data is not None else None
        got = sb.load(path).avatar.engines
        assert got.engines == registry.DEFAULT_ENGINES and got.resolution == registry.DEFAULT_RESOLUTION


def test_the_service_status_names_the_selection(tmp_path):
    from voice import service
    st = service.State(tmp_path / "voice.json", Config(), str(tmp_path / "m"))
    got = st.status()["avatar_engines"]
    assert got == selection.parse(None).as_dict()


# ---------------------------------------------------------------------------------------- the server's copy of the table
def test_the_server_copy_of_the_table_is_the_registrys():
    from serve import voice_settings as vs
    assert vs.AVATAR_TARGETS == TARGETS
    assert {k: (v[0], v[1], v[2]) for k, v in vs.AVATAR_ENGINES.items()} == {i.id: (i.runs_on, i.resolutions, i.default_resolution) for i in registry.INFOS}
    assert vs.AVATAR_DEFAULT_ENGINES == registry.DEFAULT_ENGINES and vs.AVATAR_DEFAULT_RESOLUTION == registry.DEFAULT_RESOLUTION
    assert vs.AVATAR_FPS == selection.FPS_HINT and vs.AVATAR_DEFAULT_FPS == selection.DEFAULT_FPS


def test_the_server_and_the_service_read_a_stored_value_the_same_way():
    from serve import voice_settings as vs
    stored = [None, {}, {"engines": {"pc": "er_nerf", "web": "er_nerf", "android": 5}, "resolution": {"pc": 384, "web": 7}, "fps_hint": 99},
              {"engines": {"android": "visemes"}, "resolution": {"android": 144}, "fps_hint": 12}, {"engines": {"pc": "sadtalker"}, "resolution": {"pc": 480}}]
    for s in stored:
        assert vs.avatar_selection(s) == selection.parse(s).as_dict(), s
