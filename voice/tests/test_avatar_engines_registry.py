"""The table of avatar engines: every descriptor is complete and valid, the facts that were given are the ones set, the interface raises
NotImplementedForNow until an engine has code, and `status_of` reads what exists for an avatar (temp folders only, no model, no GPU)."""
import json
from dataclasses import replace

import pytest

from callbridge import avatar_assets, avatar_weights, models_store
from callbridge.viseme_gen import VISEMI
from voice.avatar_engines import registry, status
from voice.avatar_engines.base import MODES, TARGETS, AvatarEngine, License, NotImplementedForNow, Training
from voice.avatar_engines.pack import PackEngine
from voice.avatar_engines.personalised import PersonalisedEngine

IDS = ["visemes", "wav2lip_gan96", "wav2lip_v288", "ultralight", "feathertalk", "musetalk_15", "sadtalker", "er_nerf"]


def info(i):
    return registry.info_of(i)


def test_the_table_has_the_eight_engines_each_once_and_every_descriptor_validates():
    assert [i.id for i in registry.INFOS] == IDS == list(registry.ENGINES)
    for i in registry.INFOS:
        i.validate()
        assert i.as_dict()["id"] == i.id and json.dumps(i.as_dict())          # a descriptor goes out as JSON


def test_the_values_that_were_given_are_the_ones_set():
    v, w, w2 = info("visemes"), info("wav2lip_gan96"), info("wav2lip_v288")
    assert (v.kind, v.runs_on, v.license.id) == ("generic", ("pc", "web", "android", "esp32"), "own") and min(v.resolutions) == 96 and max(v.resolutions) == 480
    assert w.native_resolution == 96 and w.license.noncommercial and w.license.verify and set(w.runs_on) == {"pc", "android", "web"}
    assert w.weight_mb == 37 and "36,664,106" in w.performance["en"]
    assert w2.weight_mb is None and w2.license.noncommercial and not w2.license.verify and w2.native_resolution == 288
    for name in ("ultralight", "feathertalk"):
        i = info(name)
        assert i.kind == "personalized" and i.license.id == "Apache-2.0" and i.training == Training(True, (3, 5), True)
        assert not i.license.verify                                              # the note on Ultralight's missing LICENSE file is in the code notes, not in the page
    assert info("ultralight").native_resolution is None
    ft = info("feathertalk")
    assert ft.native_resolution == 144 and "3.36M" in ft.performance["en"] and "Chinese-only" in " ".join(n["en"] for n in ft.needs)
    m = info("musetalk_15")
    assert m.license.id == "MIT" and m.native_resolution == 256 and "30 fps" in m.performance["en"] and "per the project" in m.performance["en"]
    assert info("sadtalker").runs_on == ("pc",) and not info("sadtalker").license.verify
    er = info("er_nerf")
    assert er.runs_on == ("pc",) and er.kind == "personalized" and "fragile" in " ".join(n["en"] for n in er.needs).lower()


def test_the_size_of_the_wav2lip_file_is_the_one_in_the_catalog_and_every_other_weight_is_not_claimed():
    assert round(36_664_106 / 1_000_000) == info("wav2lip_gan96").weight_mb
    assert [i.id for i in registry.INFOS if i.weight_mb is None] == ["wav2lip_v288", "ultralight", "feathertalk", "musetalk_15", "sadtalker", "er_nerf"]


def test_no_performance_note_states_a_speed_without_a_source():
    for i in registry.INFOS:
        text = i.performance["en"]
        assert ("fps" not in text) or i.id == "musetalk_15", i.id
        assert "not measured" in text.lower() or "per the project" in text or i.id == "er_nerf", i.id


def test_the_defaults_run_on_their_target_and_draw_at_a_height_the_engine_offers():
    for target in TARGETS:
        i = info(registry.DEFAULT_ENGINES[target])
        assert target in i.runs_on and registry.DEFAULT_RESOLUTION[target] in i.resolutions
    assert registry.DEFAULT_ENGINES == {"pc": "musetalk_15", "web": "visemes", "android": "wav2lip_gan96", "esp32": "visemes"}
    for mode, eid in registry.MODE_ENGINE.items():
        assert info(eid).mode == mode and info(eid).integrated


def test_modes_are_the_gateways_and_video_engines_are_the_ones_that_draw_on_the_pc():
    assert {i.mode for i in registry.INFOS} <= set(MODES) | {None}
    assert {i.id for i in registry.INFOS if i.mode == "video"} == {"musetalk_15", "wav2lip_v288", "sadtalker", "er_nerf"}
    assert {i.id for i in registry.INFOS if i.mode == "wav2lip"} == {"wav2lip_gan96"}
    assert {i.id for i in registry.INFOS if i.integrated} == {"visemes", "wav2lip_gan96", "musetalk_15"}


def test_a_broken_descriptor_is_refused():
    good = info("visemes")
    bad = [replace(good, runs_on=("moon",)), replace(good, kind="generic", training=Training(True, None, True)), replace(good, default_resolution=7),
           replace(good, performance={"en": "x"}), replace(good, resolutions=(240, 96)), replace(good, mode="hologram"), replace(good, requires="magic"),
           replace(good, weight_mb=-1), replace(good, kind="personalized"), replace(good, license=License("x", False, False, {"en": "a", "it": ""}))]
    for b in bad:
        with pytest.raises(ValueError):
            b.validate()


def test_the_interface_raises_not_implemented_for_now_and_the_engines_are_listed_meanwhile(tmp_path):
    for engine in registry.ENGINES.values():
        assert isinstance(engine, AvatarEngine)
        if isinstance(engine, (PersonalisedEngine, PackEngine)):             # these have code: voice/tests/test_avatar_engine_*.py
            continue
        with pytest.raises(NotImplementedForNow):
            engine.prepare(tmp_path, lambda phase, pct: None)
        with pytest.raises(NotImplementedForNow):
            engine.render(b"", 16000, 240, 25)
        with pytest.raises(NotImplementedForNow):
            engine.export_for_device(tmp_path, "android")
    with pytest.raises(TypeError):
        AvatarEngine()                                                       # `info` is abstract


# ---------------------------------------------------------------------------------------- status_of on temp folders
def make(tmp_path, *files):
    folder = tmp_path / "avatars" / "ada"
    for rel in files:
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(b"x")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def models(tmp_path, *, wav2lip=False, musetalk=False):
    base = tmp_path / "models"
    base.mkdir(parents=True, exist_ok=True)
    if wav2lip:
        f = base / "avatar" / "wav2lip-int8"
        f.mkdir(parents=True)
        (f / avatar_assets.MODEL_FILE).write_bytes(b"onnx")
    if musetalk:
        for rel in avatar_weights.REQUIRED:
            p = base / "avatar" / "musetalk-v15" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"w")
    return str(base)


AVAILABLE = {"available": True, "reason": "", "reason_it": ""}


def test_no_avatar_means_nothing_is_available_and_says_so(tmp_path):
    for i in registry.INFOS:
        for none in (None, tmp_path / "nothing"):
            got = i.status_of(none, models(tmp_path))
            assert got["available"] is False and "no avatar" in got["reason"] and got["reason_it"]


def test_visemes_need_the_poster_and_the_sprites(tmp_path):
    v = info("visemes")
    assert "no photo" in v.status_of(make(tmp_path))["reason"]
    folder = make(tmp_path, "poster.jpg")
    assert not v.status_of(folder)["available"] and "not made yet" in v.status_of(folder)["reason"]
    folder = make(tmp_path, "poster.jpg", *avatar_assets.VISEME_FILES)
    assert v.status_of(folder) == AVAILABLE
    assert len(avatar_assets.VISEME_FILES) == len(VISEMI) + 1


def test_wav2lip_needs_the_face_the_box_and_the_model_file_in_the_models_folder(tmp_path):
    w = info("wav2lip_gan96")
    folder = make(tmp_path, "poster.jpg", *avatar_assets.WAV2LIP_FILES)
    got = w.status_of(folder, models(tmp_path))
    assert not got["available"] and "model file is not in the models folder" in got["reason"]
    assert w.status_of(folder, models(tmp_path / "again", wav2lip=True)) == AVAILABLE
    both = w.status_of(make(tmp_path / "x", "poster.jpg"), models(tmp_path / "y"))
    assert "not made yet" in both["reason"] and "model file" in both["reason"] and ";" in both["reason"]


def test_musetalk_needs_its_weights_and_a_prepared_clip(tmp_path):
    m = info("musetalk_15")
    folder = make(tmp_path, "poster.jpg", "clips/walk.mp4")
    got = m.status_of(folder, models(tmp_path))
    assert not got["available"] and "11 MuseTalk weight" in got["reason"] and "no video" in got["reason"]
    base = models(tmp_path / "w", musetalk=True)
    assert "no video" in m.status_of(folder, base)["reason"] and "weight" not in m.status_of(folder, base)["reason"]
    (folder / "prepared").mkdir()
    (folder / "prepared" / "index.json").write_text(json.dumps({"walk.mp4": {"frames": 10}}), encoding="utf-8")
    assert m.status_of(folder, base) == AVAILABLE


def test_a_personalized_engine_is_not_available_until_it_is_trained_and_integrated(tmp_path):
    u = info("ultralight")
    folder = make(tmp_path, "poster.jpg", "engines/ultralight/trained.json")             # the mark holds garbage: not a finished training
    got = u.status_of(folder)
    assert not got["available"] and "not part of this build" in got["reason"] and "not been trained" in got["reason"]
    integrated = replace(u, integrated=True)                                  # what it will be when its code is in
    assert "not been trained" in integrated.status_of(make(tmp_path / "n", "poster.jpg"))["reason"]
    (folder / "engines" / "ultralight" / "trained.json").write_text("{}", encoding="utf-8")
    assert integrated.status_of(folder) == AVAILABLE


def test_the_files_the_status_looks_for_are_the_ones_the_service_serves():
    assert avatar_assets.MODEL_ID in models_store.MODELS and status.TRAINED_DIR == "engines"
