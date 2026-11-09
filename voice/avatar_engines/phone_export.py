"""The package a trained personalised avatar (Ultralight, FeatherTalk) gives the phone, made from what `export_for_device` left in
`<avatar>/engines/<id>/export/`: the ONNX models, the first frame of the person's video, and what is added here:

    phone.json              what the phone checks and runs: sha256 and size of each file, the models' inputs and outputs, the frame rate, where the face
                            sits in the frame, the audio front end, and the self-test (inputs shipped with the package and the outputs the PC measured)
    phone/template.bin      the model input of the template frame, made as the upstream `inference.py` makes it: 6 x S x S bytes (the face, then the same
                            face with its mouth rectangle black), blue first
    phone/fbank.onnx        Ultralight with wenet: Kaldi's 80-band filter bank as a fixed ONNX graph (the phone has no Kaldi), checked here against torchaudio
    phone/selftest/*        the synthetic audio and model inputs of the self-test

The phone draws over the template frame, not over a moving video: the model input is made once, the template's own geometry is `template.inv`. What can
run on a phone: FeatherTalk (FeatherHuBERT is an ONNX model of 14 M parameters that takes the waveform) and Ultralight with wenet (the filter bank above, then
the wenet encoder the person installed). Ultralight with HuBERT Large (about 1.2 GB) cannot, and is refused. Nothing here loads a model other than to
measure the outputs the phone must reproduce (onnxruntime on the CPU); see docs/voice/engines-personalised.md."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PHONE_FILE = "phone.json"
PHONE_DIR = "phone"
SELFTEST_DIR = "phone/selftest"
VERSION = 1
RATE = 16000
ENGINES = ("ultralight", "feathertalk")
TOLERANCE = {"rtol": 0.02, "atol": 0.002}               # what the phone allows between its outputs and the PC's: |a - b| <= atol + rtol * max|b|
SAMPLES_AT = 16                                         # how many output values the self-test compares one by one

# The fixed interface of each model, from pth2onnx.py (Ultralight) and FeatherTalk-CPP/tools/export_models.py at the pinned commits.
FACE = {"ultralight": {"size": 160, "crop": 168, "audio": [1, 128, 16, 32], "model": "unet.onnx"},
        "feathertalk": {"size": 144, "crop": 152, "audio": [1, 40, 1024], "model": "feathertalk_144.onnx"}}
BORDER = 4                                              # (crop - size) / 2 in both engines
MASK_RECT = (5, 5, 150, 145)                            # face_utils.MASK_RECT, for a face of 160 px; scaled by size / 160
FEATHER = {"encoder": "feather_hubert.onnx", "tokens": 40, "dim": 1024, "token_samples": 320, "kernel": 400, "frames": 20, "block_tokens": 32, "context_tokens": 64}
WENET = {"frames": 67, "frame_len": 400, "shift": 160, "mels": 80, "nfft": 512, "front": 5120, "back": 5600, "step": 5, "window": 8, "frame_floats": 8192,
         "encoder": "encoder.onnx", "inputs": ["chunk", "offset", "att_cache", "cnn_cache"]}
WENET_STATE = {"offset": ("i64", [1], 100), "att_cache": ("f32", [3, 8, 16, 128], 0), "cnn_cache": ("f32", [3, 1, 512, 14], 0)}   # wenet_infer.py: no state is carried


class PhoneExportError(RuntimeError):
    """The package cannot be made; the message says why, in a form a person can read."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- the face, as the upstream scripts cut it
def read_landmarks(path: Path):
    import numpy as np
    pts = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            pts.append([int(float(v)) for v in line.split()[:2]])
    return np.array(pts, dtype=np.int32)


def face_bbox(lms) -> tuple[int, int, int, int]:
    """face_utils.compute_face_bbox: left and right at landmarks 1 and 31, top at 52, a square."""
    if len(lms) < 53:
        raise PhoneExportError("the landmark file of the template frame has fewer than 53 points")
    xmin, ymin, xmax = int(lms[1][0]), int(lms[52][1]), int(lms[31][0])
    return xmin, ymin, xmax, ymin + (xmax - xmin)


def mask_rect(size: int) -> tuple[int, int, int, int]:
    scale = size / 160.0
    return tuple(round(v * scale) for v in MASK_RECT)       # type: ignore[return-value]


def template(frame_bgr, lms, engine: str) -> tuple:
    """(the model input as uint8 [6, S, S], the geometry) of one frame: the face with its mouth rectangle black next to the whole face, and the 2 x 3 matrix
    from the drawn S x S face to the frame's pixels (x' = a x + b y + c, y' = d x + e y + f) with the rectangle the model was asked to redraw."""
    import cv2
    import numpy as np
    spec = FACE[engine]
    size, crop = spec["size"], spec["crop"]
    xmin, ymin, xmax, ymax = face_bbox(lms)
    height, width = frame_bgr.shape[:2]
    if not (0 <= xmin < xmax <= width and 0 <= ymin < ymax <= height):
        raise PhoneExportError("the face box of the template frame is outside the frame")
    region = frame_bgr[ymin:ymax, xmin:xmax]
    face = cv2.resize(region, (crop, crop), interpolation=cv2.INTER_AREA)[BORDER:BORDER + size, BORDER:BORDER + size].copy()
    rect = mask_rect(size)
    masked = cv2.rectangle(face.copy(), rect, (0, 0, 0), -1)
    hot = cv2.rectangle(np.zeros((size, size), np.uint8), rect, 255, -1)
    ys, xs = np.nonzero(hot)
    chw = np.concatenate([face.transpose(2, 0, 1), masked.transpose(2, 0, 1)], axis=0)
    step = (xmax - xmin) / crop
    geometry = {"frame_w": int(width), "frame_h": int(height), "inv": [step, 0.0, xmin + BORDER * step, 0.0, step, ymin + BORDER * step],
                "mask": {"x": int(xs.min()), "y": int(ys.min()), "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1)}}
    return np.ascontiguousarray(chw, dtype=np.uint8), geometry


# ---------------------------------------------------------------------------------------------- the audio front end of wenet, as an ONNX graph
def _mel_banks(mels: int, nfft: int, rate: int, low: float):
    """torchaudio.compliance.kaldi.get_mel_banks without warping, as [nfft / 2 + 1, mels] (the last row, the Nyquist bin, is zero)."""
    import numpy as np
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)         # noqa: E731
    high = rate / 2.0
    delta = (mel(high) - mel(low)) / (mels + 1)
    b = np.arange(mels)[:, None]
    left, center, right = mel(low) + b * delta, mel(low) + (b + 1) * delta, mel(low) + (b + 2) * delta
    m = mel(rate / nfft * np.arange(nfft // 2))[None, :]
    banks = np.maximum(0.0, np.minimum((m - left) / (center - left), (right - m) / (right - center)))
    return np.concatenate([banks, np.zeros((mels, 1))], axis=1).T


def build_fbank_onnx(path: Path, frames: int = WENET["frames"], frame_len: int = WENET["frame_len"], shift: int = WENET["shift"], mels: int = WENET["mels"],
                     nfft: int = WENET["nfft"], rate: int = RATE, low: float = 20.0) -> int:
    """Writes Kaldi's fbank (what wenet's `Feature_Pipeline` computes: 80 bands, 25 ms every 10 ms, no dither, DC removed, pre-emphasis 0.97, Povey window,
    power spectrum, log, low edge 20 Hz) for exactly `frames` frames as an ONNX graph in double precision: input `samples` [1, n] (the waveform at 16-bit
    scale), output `fbank` [1, 1, frames, mels] (the shape of wenet's `chunk`). Returns n."""
    import numpy as np
    import onnx
    from onnx import TensorProto as T, helper as h, numpy_helper as nh
    n = (frames - 1) * shift + frame_len
    k, t = np.arange(nfft // 2 + 1)[None, :], np.arange(frame_len)[:, None]
    ang = 2.0 * np.pi * t * k / nfft
    eps = float(np.finfo(np.float32).eps)
    consts = {"idx": (np.arange(frames)[:, None] * shift + np.arange(frame_len)[None, :]).astype(np.int64), "flat": np.array([-1], np.int64),
              "window": (np.hanning(frame_len) ** 0.85)[None, :], "cos": np.cos(ang), "sin": np.sin(ang), "banks": _mel_banks(mels, nfft, rate, low),
              "pre": np.array(0.97), "eps": np.array(eps), "out": np.array([1, 1, frames, mels], np.int64),
              "s0": np.array([0], np.int64), "s1": np.array([1], np.int64), "sm": np.array([frame_len - 1], np.int64), "ax": np.array([1], np.int64)}
    nodes = [h.make_node("Cast", ["samples"], ["xd"], to=T.DOUBLE), h.make_node("Reshape", ["xd", "flat"], ["x1"]), h.make_node("Gather", ["x1", "idx"], ["fr"], axis=0),
             h.make_node("ReduceMean", ["fr"], ["mean"], axes=[1], keepdims=1), h.make_node("Sub", ["fr", "mean"], ["dc"]),
             h.make_node("Slice", ["dc", "s0", "s1", "ax"], ["first"]), h.make_node("Slice", ["dc", "s0", "sm", "ax"], ["head"]),
             h.make_node("Concat", ["first", "head"], ["prev"], axis=1), h.make_node("Mul", ["prev", "pre"], ["pp"]), h.make_node("Sub", ["dc", "pp"], ["em"]),
             h.make_node("Mul", ["em", "window"], ["win"]), h.make_node("MatMul", ["win", "cos"], ["re"]), h.make_node("MatMul", ["win", "sin"], ["im"]),
             h.make_node("Mul", ["re", "re"], ["re2"]), h.make_node("Mul", ["im", "im"], ["im2"]), h.make_node("Add", ["re2", "im2"], ["power"]),
             h.make_node("MatMul", ["power", "banks"], ["mel"]), h.make_node("Max", ["mel", "eps"], ["floor"]), h.make_node("Log", ["floor"], ["lg"]),
             h.make_node("Reshape", ["lg", "out"], ["lgr"]), h.make_node("Cast", ["lgr"], ["fbank"], to=T.FLOAT)]
    graph = h.make_graph(nodes, "kaldi_fbank", [h.make_tensor_value_info("samples", T.FLOAT, [1, n])], [h.make_tensor_value_info("fbank", T.FLOAT, [1, 1, frames, mels])],
                         [nh.from_array(v, name=name) for name, v in consts.items()])
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    return n


# ---------------------------------------------------------------------------------------------- measuring what the phone must reproduce
def summarize(arr) -> dict:
    import numpy as np
    flat = np.asarray(arr, dtype=np.float64).reshape(-1)
    at = np.unique(np.linspace(0, flat.size - 1, SAMPLES_AT).astype(np.int64))
    return {"shape": [int(d) for d in np.asarray(arr).shape], "mean": float(flat.mean()), "std": float(flat.std()), "absmax": float(np.abs(flat).max()),
            "at": [int(i) for i in at], "values": [float(flat[i]) for i in at]}


def compare(ref: dict, arr, rtol: float = TOLERANCE["rtol"], atol: float = TOLERANCE["atol"]) -> list[str]:
    """What differs between `arr` and the summary `ref` (empty: the same). The phone does exactly this."""
    import numpy as np
    problems = []
    got = np.asarray(arr, dtype=np.float64)
    if [int(d) for d in got.shape] != list(ref["shape"]):
        return [f"shape {list(got.shape)} instead of {ref['shape']}"]
    if not np.isfinite(got).all():
        return ["not finite"]
    flat, limit = got.reshape(-1), atol + rtol * ref["absmax"]
    for i, v in zip(ref["at"], ref["values"]):
        if abs(flat[i] - v) > limit:
            problems.append(f"value {i}: {flat[i]:.6g} instead of {v:.6g}")
    for key, v in (("mean", float(flat.mean())), ("std", float(flat.std()))):
        if abs(v - ref[key]) > limit:
            problems.append(f"{key}: {v:.6g} instead of {ref[key]:.6g}")
    return problems


def ort_run(model: Path, feeds: dict) -> list:
    """The outputs of a model on the CPU, with onnxruntime (the PC's reference for the phone's self-test)."""
    try:
        import onnxruntime as ort
    except ImportError:
        raise PhoneExportError("onnxruntime is not installed in the Python that makes the package: the phone's self-test needs the PC's outputs") from None
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return session.run(None, feeds)


def synthetic_audio(samples: int, rate: int = RATE):
    """A speech-like signal (three tones under a syllable-rate envelope, a little noise), the same on every machine because it is shipped as a file."""
    import numpy as np
    t = np.arange(samples) / rate
    env = 0.5 * (1.0 + np.sin(2.0 * np.pi * 2.5 * t))
    x = env * (0.3 * np.sin(2.0 * np.pi * 180 * t) + 0.15 * np.sin(2.0 * np.pi * 720 * t) + 0.08 * np.sin(2.0 * np.pi * 2400 * t))
    return (x + 0.02 * np.random.default_rng(7).standard_normal(samples)).astype(np.float32)


def _onnx_io(path: Path) -> tuple[dict, dict]:
    """({input name: shape}, {output name: shape}) of an ONNX file, -1 for a dimension that is not fixed."""
    import onnx
    model = onnx.load(str(path), load_external_data=False)
    inits = {i.name for i in model.graph.initializer}

    def shape(v):
        return [d.dim_value if d.HasField("dim_value") and d.dim_value > 0 else -1 for d in v.type.tensor_type.shape.dim]
    return {v.name: shape(v) for v in model.graph.input if v.name not in inits}, {v.name: shape(v) for v in model.graph.output}


def _need(path: Path, inputs: dict, output: tuple[str, int] | None = None, what: str = "") -> dict:
    """The model's inputs must be those named (with these fixed dimensions); returns {"inputs", "outputs"} for phone.json."""
    if not path.is_file():
        raise PhoneExportError(f"{path.name} is not in the export folder: export again")
    try:
        have_in, have_out = _onnx_io(path)
    except Exception as e:                                          # noqa: BLE001 (onnx raises its own kinds for a file that is not a model)
        raise PhoneExportError(f"{path.name} is not an ONNX model ({type(e).__name__})") from None
    if set(have_in) != set(inputs):
        raise PhoneExportError(f"{path.name} takes {sorted(have_in)}, {what or 'the phone'} expects {sorted(inputs)}")
    for name, shape in inputs.items():
        got = have_in[name]
        if shape is not None and (len(got) != len(shape) or any(a != -1 and b != -1 and a != b for a, b in zip(got, shape))):
            raise PhoneExportError(f"{path.name}: input {name} has shape {got}, expected {shape}")
    if output is not None and (output[0] not in have_out or len(have_out[output[0]]) != output[1]):
        raise PhoneExportError(f"{path.name}: output {output[0]} is missing or has the wrong rank")
    return {"inputs": have_in, "outputs": have_out}


def _write_f32(path: Path, arr) -> None:
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(np.ascontiguousarray(arr, dtype="<f4").tobytes())


def _feeds(stage: dict, root: Path) -> dict:
    """The arrays a stage feeds its model with, from the files and fills its `inputs` name (what the phone does too)."""
    import numpy as np
    feeds = {}
    for spec in stage["inputs"]:
        if "file" in spec:
            raw = (root / spec["file"]).read_bytes()
            arr = np.frombuffer(raw, dtype=np.uint8 if spec["dtype"] == "u8" else "<f4").astype(np.float32)
            arr = arr / spec["scale"] if "scale" in spec else arr
        else:
            arr = np.full(spec["shape"], spec["fill"])
        feeds[spec["name"]] = arr.astype(np.int64 if spec["dtype"] == "i64" else np.float32).reshape(spec["shape"])
    return feeds


def run_stage(stage: dict, root: Path, run: Callable = ort_run) -> list:
    return run(root / stage["model"], _feeds(stage, root))


def check_stages(phone: dict, root: Path, run: Callable = ort_run) -> list[str]:
    """Runs every self-test stage of `phone.json` again and compares with what it records (the same as the phone's test, on the PC)."""
    problems = []
    for stage in phone["selftest"]["stages"]:
        outs = run_stage(stage, root, run)
        for spec in stage["outputs"]:
            problems += [f"{stage['name']}: {p}" for p in compare(spec, outs[spec["index"]], **phone["selftest"]["tolerance"])]
    return problems


# ---------------------------------------------------------------------------------------------- the package
def _measure(name: str, model: str, inputs: list, root: Path, run: Callable) -> tuple[dict, list]:
    """A self-test stage and the outputs of its first output, measured now."""
    stage = {"name": name, "model": model, "inputs": inputs, "outputs": []}
    outs = run_stage(stage, root, run)
    stage["outputs"] = [{"index": 0, **summarize(outs[0])}]
    return stage, outs


def _visual_inputs(spec: dict) -> list:
    return [{"name": "input", "dtype": "u8", "shape": [1, 6, spec["size"], spec["size"]], "file": f"{PHONE_DIR}/template.bin", "scale": 255},
            {"name": "audio", "dtype": "f32", "shape": spec["audio"], "file": f"{SELFTEST_DIR}/audio_feat.f32"}]


def _feather(export: Path, run: Callable) -> dict:
    import numpy as np
    spec = FACE["feathertalk"]
    audio_io = _need(export / FEATHER["encoder"], {"waveform": [1, -1]}, ("hidden", 3), "FeatherHuBERT")
    visual_io = _need(export / spec["model"], {"input": [1, 6, spec["size"], spec["size"]], "audio": spec["audio"]}, ("output", 4))
    n = FEATHER["token_samples"] * 64 + FEATHER["kernel"] - FEATHER["token_samples"]
    x = synthetic_audio(n)
    _write_f32(export / SELFTEST_DIR / "audio.f32", (x - x.mean()) / np.sqrt(x.var() + 1e-7))
    _write_f32(export / SELFTEST_DIR / "audio_feat.f32", np.random.default_rng(11).standard_normal(spec["audio"]) * 0.5)
    stages = [_measure("audio", FEATHER["encoder"], [{"name": "waveform", "dtype": "f32", "shape": [1, n], "file": f"{SELFTEST_DIR}/audio.f32"}], export, run)[0],
              _measure("visual", spec["model"], _visual_inputs(spec), export, run)[0]]
    front = {"kind": "feather_hubert", "file": FEATHER["encoder"], "input": "waveform", "output": "hidden", "token_samples": FEATHER["token_samples"],
             "kernel": FEATHER["kernel"], "dim": FEATHER["dim"], "tokens_per_frame": 2, "window_frames": FEATHER["frames"], "normalize": "mean_var",
             "block_tokens": FEATHER["block_tokens"], "context_tokens": FEATHER["context_tokens"]}
    return {"model": spec["model"], "front": front, "ios": {FEATHER["encoder"]: audio_io, spec["model"]: visual_io}, "stages": stages, "extra": [FEATHER["encoder"]]}


def _wenet(export: Path, run: Callable) -> dict:
    import numpy as np
    spec = FACE["ultralight"]
    unet_io = _need(export / spec["model"], {"input": [1, 6, spec["size"], spec["size"]], "audio": spec["audio"]}, ("output", 4))
    enc_io = _need(export / WENET["encoder"], {k: None for k in WENET["inputs"]}, None, "wenet")
    n = build_fbank_onnx(export / PHONE_DIR / "fbank.onnx")
    _write_f32(export / SELFTEST_DIR / "audio.f32", np.trunc(synthetic_audio(n) * 32767.0))
    _write_f32(export / SELFTEST_DIR / "audio_feat.f32", np.random.default_rng(11).standard_normal(spec["audio"]) * 0.5)
    fb, outs = _measure("fbank", f"{PHONE_DIR}/fbank.onnx", [{"name": "samples", "dtype": "f32", "shape": [1, n], "file": f"{SELFTEST_DIR}/audio.f32"}], export, run)
    _write_f32(export / SELFTEST_DIR / "fbank.f32", outs[0])
    chunk = [1, 1, WENET["frames"], WENET["mels"]]
    state = [{"name": k, "dtype": d, "shape": s, "fill": v} for k, (d, s, v) in WENET_STATE.items()]
    enc, outs = _measure("encoder", WENET["encoder"], [{"name": "chunk", "dtype": "f32", "shape": chunk, "file": f"{SELFTEST_DIR}/fbank.f32"}] + state, export, run)
    if np.asarray(outs[0]).size != WENET["frame_floats"]:
        raise PhoneExportError(f"the wenet encoder gives {np.asarray(outs[0]).size} values for a chunk, the model of Ultralight takes {WENET['frame_floats']}")
    uv = _measure("visual", spec["model"], _visual_inputs(spec), export, run)[0]
    front = {"kind": "wenet", "fbank_file": f"{PHONE_DIR}/fbank.onnx", "fbank_samples": n, "encoder_file": WENET["encoder"], "chunk_frames": WENET["frames"], "mels": WENET["mels"],
             "pad_front": WENET["front"], "pad_back": WENET["back"], "step_samples": WENET["step"] * WENET["shift"], "window_frames": WENET["window"],
             "frame_floats": WENET["frame_floats"], "pcm_scale": 32767, "shift": WENET["shift"], "frame_len": WENET["frame_len"],
             "state": {k: {"dtype": d, "shape": s, "fill": v} for k, (d, s, v) in WENET_STATE.items()}}
    ios = {spec["model"]: unet_io, WENET["encoder"]: enc_io, f"{PHONE_DIR}/fbank.onnx": {"inputs": {"samples": [1, n]}, "outputs": {"fbank": chunk}}}
    return {"model": spec["model"], "front": front, "ios": ios, "stages": [fb, enc, uv], "extra": [WENET["encoder"], f"{PHONE_DIR}/fbank.onnx"]}


def package(avatar_dir: Path, engine: str, run: Callable = ort_run, now: Callable[[], str] | None = None) -> dict:
    """Makes `phone.json` and its files in `<avatar>/engines/<engine>/export/` and lists them in `export.json` (so that the avatar's manifest carries them).
    Raises PhoneExportError when this avatar's engine cannot run on a phone or something is missing. `run(model, feeds)` measures the outputs."""
    import cv2
    if engine not in ENGINES:
        raise PhoneExportError(f"{engine} has no phone runtime")
    folder = Path(avatar_dir) / "engines" / engine
    export = folder / "export"
    try:
        mark = json.loads((folder / "trained.json").read_text(encoding="utf-8"))
        listing = json.loads((export / "export.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PhoneExportError("this avatar has not been trained and exported for this engine") from None
    if not isinstance(mark, dict) or not isinstance(listing, dict) or not isinstance(listing.get("files"), dict) or not isinstance(listing.get("input"), dict):
        raise PhoneExportError("export.json is not readable: export again")
    if engine == "ultralight" and mark.get("asr") != "wenet":
        raise PhoneExportError("Ultralight with HuBERT Large (about 1.2 GB) cannot run on a phone: train it with wenet")
    frame_file, lms_file = export / "frames" / "0.jpg", export / "lms" / "0.lms"
    frame = cv2.imread(str(frame_file)) if frame_file.is_file() else None
    if frame is None or not lms_file.is_file():
        raise PhoneExportError("the first frame of the video and its landmarks are not in the export folder: export again")
    data, geometry = template(frame, read_landmarks(lms_file), engine)
    (export / PHONE_DIR).mkdir(exist_ok=True)
    (export / PHONE_DIR / "template.bin").write_bytes(data.tobytes())
    part = (_feather if engine == "feathertalk" else _wenet)(export, run)
    spec = FACE[engine]
    rels = sorted({part["model"], "frames/0.jpg", f"{PHONE_DIR}/template.bin", *part["extra"]} | {p.relative_to(export).as_posix() for p in (export / SELFTEST_DIR).glob("*")})
    phone = {"version": VERSION, "engine": engine, "created": (now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")))(), "fps": int(mark.get("fps") or 25),
             "sample_rate": RATE, "size": spec["size"], "audio_encoder": mark.get("asr"), "frontend": part["front"], "model": {"file": part["model"], "audio": spec["audio"]},
             "template": {"input": f"{PHONE_DIR}/template.bin", "frame": "frames/0.jpg", **geometry}, "models": part["ios"],
             "selftest": {"tolerance": dict(TOLERANCE), "stages": part["stages"]}, "files": {r: {"sha256": sha256_file(export / r), "bytes": (export / r).stat().st_size} for r in rels}}
    problems = check_stages(phone, export, run)
    if problems:
        raise PhoneExportError("the models do not repeat their own outputs: " + "; ".join(problems[:3]))
    (export / PHONE_FILE).write_text(json.dumps(phone, indent=1), encoding="utf-8")
    prefix = f"engines/{engine}/export/"
    listing["files"].update({prefix + r: sha256_file(export / r) for r in rels + [PHONE_FILE]})
    listing["input"]["phone"] = {"version": VERSION, "file": PHONE_FILE}
    (export / "export.json").write_text(json.dumps(listing, indent=1), encoding="utf-8")
    return {"ok": True, "files": len(rels) + 1, "bytes": sum(v["bytes"] for v in phone["files"].values()), "fps": phone["fps"], "size": phone["size"]}
