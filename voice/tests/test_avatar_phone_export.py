"""The package a trained Ultralight or FeatherTalk gives the phone (voice/avatar_engines/phone_export.py): the face cut as the upstream scripts cut it, Kaldi's filter
bank as an ONNX graph against torchaudio, the self-test measured with onnxruntime on tiny stand-in graphs with the real input names and shapes, and what is refused.
No training, no real model, no GPU."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")
cv2 = pytest.importorskip("cv2")
from onnx import TensorProto as T, helper as h, numpy_helper as nh      # noqa: E402

from callbridge import avatar_assets                                   # noqa: E402
from voice.avatar_engines import phone_export as pe                    # noqa: E402

VENDOR = Path(__file__).resolve().parents[1] / "avatar_engines" / "vendor"


def _save(nodes, inputs, outputs, consts, path):
    graph = h.make_graph(nodes, "fake", inputs, outputs, [nh.from_array(v, name=k) for k, v in consts.items()])
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 13)])
    model.ir_version = 8
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def fake_unet(path, size, audio_shape):
    """input [1,6,S,S], audio -> output [1,3,S,S]: the first three planes, scaled, plus the mean of the audio."""
    _save([h.make_node("Slice", ["input", "s0", "s3", "ax"], ["rgb"]), h.make_node("Mul", ["rgb", "half"], ["m"]),
           h.make_node("ReduceMean", ["audio"], ["a"], keepdims=0), h.make_node("Add", ["m", "a"], ["output"])],
          [h.make_tensor_value_info("input", T.FLOAT, [1, 6, size, size]), h.make_tensor_value_info("audio", T.FLOAT, audio_shape)],
          [h.make_tensor_value_info("output", T.FLOAT, [1, 3, size, size])],
          {"s0": np.array([0], np.int64), "s3": np.array([3], np.int64), "ax": np.array([1], np.int64), "half": np.array(0.5, np.float32)}, path)


def fake_feather(path):
    """waveform [1, samples] -> hidden [1, 63, 1024]: the mean of the waveform on a ramp."""
    _save([h.make_node("ReduceMean", ["waveform"], ["m"], keepdims=1), h.make_node("Reshape", ["m", "one"], ["m3"]), h.make_node("Add", ["m3", "ramp"], ["hidden"])],
          [h.make_tensor_value_info("waveform", T.FLOAT, [1, "samples"])], [h.make_tensor_value_info("hidden", T.FLOAT, [1, 63, 1024])],
          {"one": np.array([1, 1, 1], np.int64), "ramp": np.linspace(0, 1, 63 * 1024, dtype=np.float32).reshape(1, 63, 1024)}, path)


def fake_wenet(path):
    """chunk [1,1,67,80], offset, att_cache, cnn_cache -> y [1,16,512] (and a cache output, as wenet's)."""
    _save([h.make_node("ReduceMean", ["chunk"], ["m"], keepdims=0), h.make_node("Cast", ["offset"], ["o"], to=T.FLOAT), h.make_node("ReduceMean", ["att_cache"], ["ac"], keepdims=0),
           h.make_node("Add", ["m", "o"], ["mo"]), h.make_node("Add", ["mo", "ac"], ["mos"]), h.make_node("Reshape", ["mos", "one"], ["m3"]), h.make_node("Add", ["m3", "ramp"], ["y"]),
           h.make_node("ReduceMean", ["cnn_cache"], ["cc"], keepdims=0)],
          [h.make_tensor_value_info("chunk", T.FLOAT, [1, 1, 67, 80]), h.make_tensor_value_info("offset", T.INT64, [1]), h.make_tensor_value_info("att_cache", T.FLOAT, [3, 8, 16, 128]),
           h.make_tensor_value_info("cnn_cache", T.FLOAT, [3, 1, 512, 14])],
          [h.make_tensor_value_info("y", T.FLOAT, [1, 16, 512]), h.make_tensor_value_info("cc", T.FLOAT, [])],
          {"one": np.array([1, 1, 1], np.int64), "ramp": np.linspace(0, 1, 16 * 512, dtype=np.float32).reshape(1, 16, 512)}, path)


def landmarks(path, left=60, top=70, right=220):
    lines = [f"{i} {i}" for i in range(68)]
    lines[1], lines[31], lines[52] = f"{left} 100", f"{right} 120", f"130 {top}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def frame_image(path, w=320, h_=300):
    rng = np.random.default_rng(3)
    img = rng.integers(20, 235, (h_, w, 3), dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), img)
    return cv2.imread(str(path))


@pytest.fixture
def avatar(tmp_path):
    return tmp_path / "ada"


def exported(avatar, engine, asr, fps, builder):
    """An avatar as `export_for_device` leaves it: trained.json, export/ with the models, the first frame and its landmarks, export.json."""
    folder = avatar / "engines" / engine
    export = folder / "export"
    folder.mkdir(parents=True)
    (folder / "trained.json").write_text(json.dumps({"engine": engine, "asr": asr, "fps": fps, "epochs": 3}), encoding="utf-8")
    builder(export)
    frame_image(export / "frames" / "0.jpg")
    landmarks(export / "lms" / "0.lms")
    files = {f"engines/{engine}/export/{p.relative_to(export).as_posix()}": pe.sha256_file(p) for p in sorted(export.rglob("*")) if p.is_file()}
    (export / "export.json").write_text(json.dumps({"engine": engine, "files": files, "input": {"model_file": "m.onnx"}, "epochs": 3}), encoding="utf-8")
    return export


def feather_models(export):
    fake_feather(export / "feather_hubert.onnx")
    fake_unet(export / "feathertalk_144.onnx", 144, [1, 40, 1024])


def wenet_models(export):
    fake_wenet(export / "encoder.onnx")
    fake_unet(export / "unet.onnx", 160, [1, 128, 16, 32])


# ---------------------------------------------------------------------------------------------- the face
def _vendor(engine, name):
    pytest.importorskip("torch")
    path = VENDOR / engine / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"vendor_{engine}_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("engine", ["ultralight", "feathertalk"])
def test_the_template_is_the_input_the_upstream_inference_builds(tmp_path, engine):
    fu = _vendor(engine, "face_utils")
    img = frame_image(tmp_path / "f.jpg")
    lms_file = tmp_path / "f.lms"
    landmarks(lms_file)
    lms = pe.read_landmarks(lms_file)
    assert (lms == fu.read_landmarks(str(lms_file))).all()
    data, geo = pe.template(img, lms, engine)
    bbox = fu.compute_face_bbox(lms)
    assert pe.face_bbox(lms) == bbox
    inner_kw = {"inner_size": pe.FACE[engine]["size"]} if engine == "feathertalk" else {}
    crop = fu.crop_face(img, bbox, crop_size=pe.FACE[engine]["crop"]) if engine == "feathertalk" else fu.crop_face(img, bbox)
    inner = fu.extract_inner(crop, **inner_kw)
    ref = fu.hwc_to_chw_tensor(inner.copy()).numpy()
    masked = fu.hwc_to_chw_tensor(fu.mask_mouth(inner.copy(), **inner_kw) if engine == "feathertalk" else fu.mask_mouth(inner.copy())).numpy()
    expect = np.concatenate([ref, masked], axis=0)
    assert data.shape == (6, pe.FACE[engine]["size"], pe.FACE[engine]["size"]) and data.dtype == np.uint8
    assert np.abs(data.astype(np.float32) / 255.0 - expect).max() < 1e-6
    # the drawn face lands where the upstream paste puts it: the inner square of the resized crop, scaled back to the box
    size, a, c = pe.FACE[engine]["size"], geo["inv"][0], geo["inv"][2]
    xmin, ymin, xmax, _ = bbox
    assert abs(c - (xmin + 4 * (xmax - xmin) / pe.FACE[engine]["crop"])) < 1e-9 and abs(a * (size + 8) - (xmax - xmin)) < 1e-9
    assert geo["inv"][5] == ymin + 4 * a and geo["frame_w"] == 320 and geo["frame_h"] == 300
    m = geo["mask"]
    black = (data[3:] == 0).all(axis=0)
    assert black[m["y"]:m["y"] + m["h"], m["x"]:m["x"] + m["w"]].all() and black.sum() == m["w"] * m["h"]


def test_a_face_box_outside_the_frame_or_too_few_landmarks_is_refused(tmp_path):
    img = np.zeros((100, 100, 3), np.uint8)
    lms = np.arange(136, dtype=np.int32).reshape(68, 2)
    lms[1], lms[31], lms[52] = (10, 0), (90, 0), (0, 50)
    with pytest.raises(pe.PhoneExportError, match="outside the frame"):
        pe.template(img, lms, "feathertalk")
    with pytest.raises(pe.PhoneExportError, match="fewer than 53"):
        pe.face_bbox(lms[:10])


# ---------------------------------------------------------------------------------------------- Kaldi's filter bank
def test_the_fbank_graph_gives_what_torchaudio_computes_for_wenet(tmp_path):
    torch = pytest.importorskip("torch")
    kaldi = pytest.importorskip("torchaudio.compliance.kaldi")
    n = pe.build_fbank_onnx(tmp_path / "fbank.onnx")
    assert n == 10960
    wave = np.trunc(pe.synthetic_audio(n + 4000)[2000:2000 + n] * 32767.0)
    got = ort.InferenceSession(str(tmp_path / "fbank.onnx"), providers=["CPUExecutionProvider"]).run(None, {"samples": wave.astype(np.float32)[None]})[0]
    want = kaldi.fbank(torch.from_numpy(wave.astype(np.float32))[None], num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0, energy_floor=0.0, sample_frequency=16000).numpy()
    assert got.shape == (1, 1, 67, 80) and want.shape == (67, 80)
    assert np.abs(got[0, 0] - want).max() < 1e-3
    silent = ort.InferenceSession(str(tmp_path / "fbank.onnx"), providers=["CPUExecutionProvider"]).run(None, {"samples": np.zeros((1, n), np.float32)})[0]
    assert np.allclose(silent, np.log(np.finfo(np.float32).eps), atol=1e-4)                  # Kaldi's floor for a frame with no energy


# ---------------------------------------------------------------------------------------------- the self-test numbers
def test_compare_says_what_differs_and_allows_the_tolerance():
    ref = pe.summarize(np.linspace(-1, 1, 200).reshape(2, 100))
    assert pe.compare(ref, np.linspace(-1, 1, 200).reshape(2, 100)) == []
    assert pe.compare(ref, np.linspace(-1, 1, 200).reshape(2, 100) + 0.01) == []              # inside 0.002 + 0.02 x 1
    assert pe.compare(ref, np.linspace(-1, 1, 200).reshape(2, 100) + 0.1)
    assert "shape" in pe.compare(ref, np.zeros((3, 3)))[0]
    assert pe.compare(ref, np.full((2, 100), np.nan)) == ["not finite"]


# ---------------------------------------------------------------------------------------------- the package
def test_feathertalk_package_lists_files_stages_and_geometry(avatar):
    export = exported(avatar, "feathertalk", "feather_hubert", 25, feather_models)
    out = pe.package(avatar, "feathertalk", now=lambda: "2026-11-09T00:00:00+00:00")
    assert out["ok"] and out["fps"] == 25 and out["size"] == 144
    phone = json.loads((export / "phone.json").read_text())
    assert phone["version"] == 1 and phone["engine"] == "feathertalk" and phone["frontend"]["kind"] == "feather_hubert" and phone["template"]["frame"] == "frames/0.jpg"
    assert phone["models"]["feathertalk_144.onnx"]["inputs"] == {"input": [1, 6, 144, 144], "audio": [1, 40, 1024]}
    assert phone["models"]["feather_hubert.onnx"]["inputs"] == {"waveform": [1, -1]}
    assert set(phone["files"]) == {"feather_hubert.onnx", "feathertalk_144.onnx", "frames/0.jpg", "phone/template.bin", "phone/selftest/audio.f32", "phone/selftest/audio_feat.f32"}
    for rel, meta in phone["files"].items():
        assert meta["sha256"] == pe.sha256_file(export / rel) and meta["bytes"] == (export / rel).stat().st_size
    assert (export / "phone" / "template.bin").stat().st_size == 6 * 144 * 144
    assert [s["name"] for s in phone["selftest"]["stages"]] == ["audio", "visual"] and pe.check_stages(phone, export) == []
    listing = json.loads((export / "export.json").read_text())
    assert listing["input"]["phone"] == {"version": 1, "file": "phone.json"} and listing["input"]["model_file"] == "m.onnx"
    assert listing["files"]["engines/feathertalk/export/phone.json"] == pe.sha256_file(export / "phone.json")
    entry = avatar_assets.engine_entries(avatar)["feathertalk"]                                    # the avatar's manifest carries the package
    assert entry["files"]["engines/feathertalk/export/phone/template.bin"] == pe.sha256_file(export / "phone" / "template.bin") and entry["input"]["phone"]["file"] == "phone.json"


def test_a_changed_model_no_longer_repeats_the_numbers_the_package_recorded(avatar):
    export = exported(avatar, "feathertalk", "feather_hubert", 25, feather_models)
    pe.package(avatar, "feathertalk")
    phone = json.loads((export / "phone.json").read_text())
    fake_unet(export / "feathertalk_144.onnx", 144, [1, 40, 1024])                                  # same graph: still equal
    assert pe.check_stages(phone, export) == []
    _save([h.make_node("Constant", [], ["output"], value=nh.from_array(np.full((1, 3, 144, 144), 0.9, np.float32)))],
          [h.make_tensor_value_info("input", T.FLOAT, [1, 6, 144, 144]), h.make_tensor_value_info("audio", T.FLOAT, [1, 40, 1024])],
          [h.make_tensor_value_info("output", T.FLOAT, [1, 3, 144, 144])], {}, export / "feathertalk_144.onnx")
    assert any(p.startswith("visual:") for p in pe.check_stages(phone, export))


def test_ultralight_with_wenet_package_has_the_fbank_graph_and_the_encoder_state(avatar):
    export = exported(avatar, "ultralight", "wenet", 20, wenet_models)
    out = pe.package(avatar, "ultralight")
    assert out["fps"] == 20 and out["size"] == 160
    phone = json.loads((export / "phone.json").read_text())
    front = phone["frontend"]
    assert front["kind"] == "wenet" and front["fbank_samples"] == 10960 and front["step_samples"] == 800 and front["pad_front"] == 5120 and front["pad_back"] == 5600
    assert front["state"]["offset"] == {"dtype": "i64", "shape": [1], "fill": 100} and front["frame_floats"] == 8192
    assert {"unet.onnx", "encoder.onnx", "phone/fbank.onnx", "phone/template.bin", "frames/0.jpg", "phone/selftest/fbank.f32"} <= set(phone["files"])
    assert [s["name"] for s in phone["selftest"]["stages"]] == ["fbank", "encoder", "visual"] and pe.check_stages(phone, export) == []
    assert phone["selftest"]["stages"][0]["outputs"][0]["shape"] == [1, 1, 67, 80]


def test_what_cannot_run_on_a_phone_is_refused_with_the_reason(avatar, tmp_path):
    exported(avatar, "ultralight", "hubert", 25, wenet_models)
    with pytest.raises(pe.PhoneExportError, match="HuBERT Large"):
        pe.package(avatar, "ultralight")
    with pytest.raises(pe.PhoneExportError, match="no phone runtime"):
        pe.package(avatar, "er_nerf")
    with pytest.raises(pe.PhoneExportError, match="not been trained"):
        pe.package(tmp_path / "nobody", "feathertalk")


def test_a_model_with_other_inputs_or_a_missing_frame_is_refused(avatar):
    def wrong(export):
        fake_feather(export / "feather_hubert.onnx")
        fake_unet(export / "feathertalk_144.onnx", 144, [1, 20, 1024])                              # the audio window is not 40 tokens
    export = exported(avatar, "feathertalk", "feather_hubert", 25, wrong)
    with pytest.raises(pe.PhoneExportError, match="input audio has shape"):
        pe.package(avatar, "feathertalk")
    fake_unet(export / "feathertalk_144.onnx", 144, [1, 40, 1024])
    (export / "feathertalk_144.onnx").write_bytes(b"not a model")
    with pytest.raises(pe.PhoneExportError, match="not an ONNX model"):
        pe.package(avatar, "feathertalk")
    fake_unet(export / "feathertalk_144.onnx", 144, [1, 40, 1024])
    (export / "frames" / "0.jpg").unlink()
    with pytest.raises(pe.PhoneExportError, match="first frame"):
        pe.package(avatar, "feathertalk")


def test_a_wenet_encoder_with_another_output_size_is_refused(avatar):
    def builder(export):
        wenet_models(export)
        _save([h.make_node("ReduceMean", ["chunk"], ["y"], keepdims=0), h.make_node("Cast", ["offset"], ["o"], to=T.FLOAT)],
              [h.make_tensor_value_info("chunk", T.FLOAT, [1, 1, 67, 80]), h.make_tensor_value_info("offset", T.INT64, [1]), h.make_tensor_value_info("att_cache", T.FLOAT, [3, 8, 16, 128]),
               h.make_tensor_value_info("cnn_cache", T.FLOAT, [3, 1, 512, 14])], [h.make_tensor_value_info("y", T.FLOAT, [])], {}, export / "encoder.onnx")
    exported(avatar, "ultralight", "wenet", 20, builder)
    with pytest.raises(pe.PhoneExportError, match="8192"):
        pe.package(avatar, "ultralight")


def test_without_onnxruntime_the_package_says_so(avatar, monkeypatch):
    exported(avatar, "feathertalk", "feather_hubert", 25, feather_models)
    import builtins
    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "onnxruntime":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(pe.PhoneExportError, match="onnxruntime is not installed"):
        pe.package(avatar, "feathertalk")


# ---------------------------------------------------------------------------------------------- the training manager and the route that serves the package
from voice.avatar_training import TrainingJobs                          # noqa: E402
from voice.tests.test_avatar_service import AUTH, _isolated, rig        # noqa: E402,F401 (fixtures)


def test_the_training_manager_makes_the_package_and_reports_a_failure_as_a_note(avatar):
    class Engine:
        ID = "feathertalk"
    assert TrainingJobs._phone_package(Engine, avatar) == {"ok": False, "error": "this avatar has not been trained and exported for this engine"}
    exported(avatar, "feathertalk", "feather_hubert", 25, feather_models)
    assert TrainingJobs._phone_package(Engine, avatar)["ok"] is True
    (avatar / "engines" / "feathertalk" / "export" / "feather_hubert.onnx").write_bytes(b"x")
    assert "not an ONNX model" in TrainingJobs._phone_package(Engine, avatar)["error"]


def test_the_package_files_are_served_with_ranges_and_only_the_ones_the_manifest_lists(rig):
    avatar = rig.store.path("ada")
    export = exported(avatar, "feathertalk", "feather_hubert", 25, feather_models)
    pe.package(avatar, "feathertalk")
    url = "/avatar/ada/engines/feathertalk/files/"
    resp = rig.get(url + "phone.json")
    digest = pe.sha256_file(export / "phone.json")
    assert resp.status_code == 200 and resp.data == (export / "phone.json").read_bytes() and resp.headers["X-Content-SHA256"] == digest and resp.headers["ETag"] == f'"{digest}"'
    part = rig.client.get(url + "phone/template.bin", headers={**AUTH, "Range": "bytes=0-9"})
    assert part.status_code == 206 and part.data == (export / "phone" / "template.bin").read_bytes()[:10]
    for refused in ("export.json", "frames/1.jpg", "../../trained.json", "nothing"):
        assert rig.get(url + refused).status_code == 404, refused
    assert rig.get("/avatar/ada/engines/ultralight/files/phone.json").status_code == 404 and rig.get("/avatar/nobody/engines/feathertalk/files/phone.json").status_code == 404
    assert rig.client.get(url + "phone.json").status_code == 401
    man = rig.get("/avatar/ada/manifest.json").get_json()
    assert "feathertalk" in man["render_modes"] and "engines/feathertalk/export/phone.json" in man["engines"]["feathertalk"]["files"]


def test_the_server_relays_the_package_files_to_a_phone_and_not_a_path_that_climbs():
    from serve import voice_avatar
    base = "/api/voice/avatar/ada/engines/feathertalk/files/"
    assert all(voice_avatar.DEVICE_FILES.fullmatch(base + rel) for rel in ("phone.json", "phone/template.bin", "phone/selftest/audio.f32", "frames/0.jpg"))
    assert not any(voice_avatar.DEVICE_FILES.fullmatch(base + rel) for rel in ("../manifest.json", "a/../b", "x" * 200, "a b", ""))
    assert not voice_avatar.DEVICE_FILES.fullmatch("/api/voice/avatar/ada/engines/files/phone.json")
