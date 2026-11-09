"""Stand-ins for the personalised engines' tests: fake vendored scripts (small Python scripts that take the arguments of the real upstream
ones and write what they write), a fake Python environment (this interpreter), and an avatar with a clip. Nothing here needs a GPU, a model or the network."""
import sys
from pathlib import Path

PY = sys.executable

# One script for every `train.py`: the arguments of both upstream ones, tqdm-like progress with carriage returns, a checkpoint every 5 epochs and at the end.
TRAIN = r'''
import argparse, os, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument("--dataset_dir", required=True); p.add_argument("--save_dir", required=True); p.add_argument("--asr", default="hubert")
p.add_argument("--model", default=None); p.add_argument("--epochs", type=int, default=60); p.add_argument("--batchsize", type=int, default=8)
p.add_argument("--resume", default="")
a = p.parse_args()
assert os.path.isdir(os.path.join(a.dataset_dir, "full_body_img")) and os.path.isdir(os.path.join(a.dataset_dir, "landmarks"))
os.makedirs(a.save_dir, exist_ok=True)
start = int(open(a.resume).read().split("=")[1]) + 1 if a.resume else 0
if os.environ.get("FAKE_HANG"):                       # a child that would outlive its parent
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    open(os.path.join(os.path.dirname(a.save_dir), "child.pid"), "w").write(str(child.pid))
    sys.stdout.write("Epoch 1/%d:   0%%|          | 0/10\r" % a.epochs); sys.stdout.flush()
    time.sleep(600)
for e in range(start, a.epochs):
    sys.stderr.write("\rEpoch %d/%d:   0%%|          | 0/10 [00:00<?, ?frame/s]" % (e + 1, a.epochs)); sys.stderr.flush()
    sys.stderr.write("\rEpoch %d/%d:  50%%|#####     | 5/10 [00:00<00:00, 9.0frame/s, loss=0.1]" % (e + 1, a.epochs)); sys.stderr.flush()
    sys.stderr.write("\rEpoch %d/%d: 100%%|##########| 10/10 [00:00<00:00, 9.0frame/s, loss=0.1]\n" % (e + 1, a.epochs)); sys.stderr.flush()
    if (e + 1) % 5 == 0 or e == a.epochs - 1:
        for name in ("%d.pth" % e, "last.pth"):
            open(os.path.join(a.save_dir, name), "w").write("epoch=%d" % e)
'''

PROCESS = r'''
import argparse, os, sys
p = argparse.ArgumentParser()
p.add_argument("path"); p.add_argument("--asr", default="hubert"); p.add_argument("--feather_hubert_checkpoint", default="")
a = p.parse_args()
assert os.path.isfile(a.path)
if a.feather_hubert_checkpoint:
    assert os.path.isfile(a.feather_hubert_checkpoint)
base = os.path.dirname(os.path.abspath(a.path))
print("[INFO] ===== extract audio: x =====", flush=True)
print("extracting images...", flush=True)
os.makedirs(os.path.join(base, "full_body_img")); os.makedirs(os.path.join(base, "landmarks"))
n = int(os.environ.get("FAKE_FRAMES", "12"))
for i in range(n):
    open(os.path.join(base, "full_body_img", "%d.jpg" % i), "wb").write(b"jpg%d" % i)
print("[INFO] extracted %d frames" % n, flush=True)
print("detecting landmarks...", flush=True)
for i in range(n):
    open(os.path.join(base, "landmarks", "%d.lms" % i), "w").write("1 2\n")
print("extracting audio feature...", flush=True)
open(os.path.join(base, "aud_wenet.npy" if a.asr == "wenet" else "aud_hu.npy"), "wb").write(b"feat")
'''

FEATURES = r'''
import argparse, os, sys
p = argparse.ArgumentParser(); p.add_argument("--wav"); p.add_argument("--out", default=""); p.add_argument("--checkpoint", default="")
a, rest = p.parse_known_args()
if not a.wav and rest:
    a.wav = rest[0]
assert os.path.isfile(a.wav), a.wav
out = a.out or a.wav.replace(".wav", "_wenet.npy")
open(out, "wb").write(b"feat")
'''

INFERENCE = r'''
import argparse, os, sys
import cv2, numpy as np
p = argparse.ArgumentParser()
p.add_argument("--asr", default="hubert"); p.add_argument("--dataset", required=True); p.add_argument("--audio_feat", required=True)
p.add_argument("--save_path", required=True); p.add_argument("--checkpoint", required=True); p.add_argument("--audio_wav", default="")
a = p.parse_args()
assert os.path.isfile(a.audio_feat) and os.path.isfile(a.checkpoint) and os.path.isdir(os.path.join(a.dataset, "full_body_img"))
if os.environ.get("FAKE_HANG_RENDER"):                # a render that does not end, with a child
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    open(os.environ["FAKE_PID_FILE"], "w").write(str(child.pid))
    time.sleep(600)
fps = 20 if a.asr == "wenet" else 25
w = cv2.VideoWriter(a.save_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 48))
for i in range(int(os.environ.get("FAKE_OUT_FRAMES", "10"))):
    w.write(np.full((48, 64, 3), i * 10, np.uint8))
w.release()
'''

ONNX = r'''
import argparse, os
p = argparse.ArgumentParser()
p.add_argument("--checkpoint"); p.add_argument("--onnx_path"); p.add_argument("--asr", default="wenet")
p.add_argument("--feather-checkpoint"); p.add_argument("--visual-checkpoint"); p.add_argument("--model", default="auto"); p.add_argument("--output-dir")
a = p.parse_args()
if a.onnx_path:
    assert os.path.isfile(a.checkpoint)
    open(a.onnx_path, "wb").write(b"onnx")
else:
    assert os.path.isfile(a.feather_checkpoint) and os.path.isfile(a.visual_checkpoint)
    open(os.path.join(a.output_dir, "feather_hubert.onnx"), "wb").write(b"onnx1")
    open(os.path.join(a.output_dir, "feathertalk_144.onnx"), "wb").write(b"onnx2")
'''

PACKS = {
    "ultralight": {"train.py": TRAIN, "inference.py": INFERENCE, "pth2onnx.py": ONNX, "data_utils/process.py": PROCESS, "data_utils/get_landmark.py": "# fake\n",
                   "data_utils/hubert.py": FEATURES, "data_utils/wenet_infer.py": FEATURES},
    "feathertalk": {"train.py": TRAIN, "inference.py": INFERENCE, "model.py": "# fake\n", "data_utils/process.py": PROCESS, "LICENSE": "license text\n",
                    "data_utils/feather_hubert/feather_hubert.py": FEATURES, "FeatherTalk-CPP/tools/export_models.py": ONNX},
}


VENDORS: dict[str, Path] = {}


def put_pack(models: Path, engine: str, *, weights=True, wenet=True, vgg=True, hubert=False, landmarks=True) -> Path:
    """The fake vendored scripts of `engine` (beside the models folder), and the weights they load in the models folder, as far as `weights`, `wenet`, `vgg`,
    `hubert` and `landmarks` say. `fake_commands` points the engine at the fake scripts."""
    code = models.parent / "fake-vendor" / engine
    for rel, text in PACKS[engine].items():
        (code / rel).parent.mkdir(parents=True, exist_ok=True)
        (code / rel).write_text(text, encoding="utf-8")
    VENDORS[engine] = code
    base = models / "avatar-engines"
    if engine == "feathertalk" and weights:
        (base / engine / "weights").mkdir(parents=True, exist_ok=True)
        (base / engine / "weights" / "feather_hubert.pth").write_bytes(b"w")
    if engine == "ultralight" and wenet:
        (base / engine / "weights").mkdir(parents=True, exist_ok=True)
        (base / engine / "weights" / "encoder.onnx").write_bytes(b"enc")
    if landmarks:
        (base / "landmarks").mkdir(parents=True, exist_ok=True)
        for name in ("scrfd_2.5g_kps.onnx", "checkpoint_epoch_335.pth.tar"):
            (base / "landmarks" / name).write_bytes(b"lm")
    if hubert:
        (base / "hf-home" / "hub" / "models--facebook--hubert-large-ls960-ft" / "snapshots" / "abc").mkdir(parents=True)
    if vgg:
        f = base / "torch-home" / "hub" / "checkpoints" / "vgg19-dcbb9e9d.pth"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"vgg")
    return code


def fake_commands(engine, monkeypatch=None):
    """Makes the engine run only things this machine has: the conversions are file copies, and ffmpeg is found."""
    copy = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"
    engine.convert_cmd = lambda clip, out, fps: [PY, "-c", copy, str(clip), str(out)]
    engine.audio_cmd = lambda raw, wav: [PY, "-c", copy, str(raw), str(wav)]
    engine.which = lambda name: "/fake/" + name
    engine.vendor = VENDORS[engine.ID]
    return engine
