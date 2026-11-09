"""Ultralight-Digital-Human (github.com/anliyuan/Ultralight-Digital-Human, commit 5aa8b5a8): a small UNet trained on the face of one person, driven by
an audio encoder, wenet (20 fps, runs on a phone) or HuBERT Large (25 fps, better in the author's experiments). The command lines are those of its README and
of `process.py`, `train.py`, `inference.py` and `pth2onnx.py` at that commit. See docs/voice/engines-personalised.md."""
from __future__ import annotations

import shutil
from pathlib import Path

from .personalised import BATCH, EPOCHS_DEFAULT, EPOCHS_RANGE, PACKS_DIR, Check, PersonalisedEngine

# Licence: the repository has no LICENSE file; its README badge says Apache 2.0. To be confirmed with the author before distribution to others (see
# vendor/ultralight/LICENSE-NOTE.md). This is kept in the code notes only: no page shows it.
FPS = {"wenet": 20, "hubert": 25}         # the frame rate each audio encoder requires of the video (process.py)
ASR_DEFAULT = "wenet"
FACE = 160                                # FACE_INNER_SIZE of face_utils.py: what the UNet draws
AUDIO_SHAPE = {"wenet": [1, 128, 16, 32], "hubert": [1, 16, 32, 32]}      # the dummy inputs of pth2onnx.py
HUBERT_CACHE = "models--facebook--hubert-large-ls960-ft"                  # what hubert.py loads by name


class UltralightEngine(PersonalisedEngine):
    ID = "ultralight"

    def options(self, asr: str | None = None, epochs: int | None = None) -> dict:
        asr = ASR_DEFAULT if asr is None else asr
        if asr not in FPS:
            raise ValueError("the audio encoder is wenet or hubert")
        epochs = EPOCHS_DEFAULT if epochs is None else epochs
        if isinstance(epochs, bool) or not isinstance(epochs, int) or not EPOCHS_RANGE[0] <= epochs <= EPOCHS_RANGE[1]:
            raise ValueError(f"epochs is a whole number from {EPOCHS_RANGE[0]} to {EPOCHS_RANGE[1]}")
        return {"asr": asr, "fps": FPS[asr], "epochs": epochs, "batch": BATCH}

    def audio_checks(self, base: str, opts: dict) -> list[Check]:
        if opts.get("asr") == "hubert":
            snaps = Path(base) / PACKS_DIR / "hf-home" / "hub" / HUBERT_CACHE / "snapshots" if base else None
            have = bool(snaps) and snaps.is_dir() and any(snaps.iterdir())
            return [Check("audio", have, "the HuBERT Large audio encoder is in the Hugging Face cache of the engines" if have else
                          f"hubert.py loads facebook/hubert-large-ls960-ft by name and nothing downloads it: put it in the Hugging Face cache {snaps}",
                          "il codificatore audio HuBERT Large è nella cache Hugging Face dei motori" if have else
                          f"hubert.py carica facebook/hubert-large-ls960-ft per nome e nessuno lo scarica: mettilo nella cache Hugging Face {snaps}", where=str(snaps) if snaps else "")]
        if opts.get("asr") == "wenet":
            enc = self.encoder_file(base) if base else None
            have = bool(enc) and enc.is_file()
            return [Check("audio", have, "the wenet audio encoder (encoder.onnx) is in place" if have else
                          f"the wenet audio encoder is not part of the vendored code and nothing downloads it: its README names a Google Drive file; put encoder.onnx in {enc}",
                          "il codificatore audio wenet (encoder.onnx) c'è" if have else
                          f"il codificatore audio wenet non fa parte del codice incluso e nessuno lo scarica: il suo README indica un file su Google Drive; metti encoder.onnx in {enc}",
                          install="" if have else "ultralight-wenet-encoder", where=str(enc.parent) if enc else "")]
        return []

    def modules(self, opts):
        asr = (opts or {}).get("asr")
        return self.MODULES + (("yaml",) if asr == "wenet" else ("transformers",) if asr == "hubert" else ())

    def encoder_file(self, base: str) -> Path:
        return Path(base) / PACKS_DIR / self.ID / "weights" / "encoder.onnx"

    def staged_weights(self, base, opts):
        out = super().staged_weights(base, opts)
        return out + ([("data_utils/encoder.onnx", self.encoder_file(base))] if opts and opts.get("asr") == "wenet" else [])

    def preprocess_cmd(self, python, code, base, video, opts):
        return [python, "data_utils/process.py", str(video), "--asr", opts["asr"]], code

    def train_cmd(self, python, code, base, dataset, ckpt_dir, opts, resume):
        argv = [python, "train.py", "--dataset_dir", str(dataset), "--save_dir", str(ckpt_dir), "--asr", opts["asr"],
                "--epochs", str(opts["epochs"]), "--batchsize", str(opts["batch"])]
        return argv + (["--resume", str(resume)] if resume else []), code

    def features_cmd(self, python, code, base, wav, work, opts):
        if opts["asr"] == "hubert":
            feat = work / "feat.npy"
            return [python, "data_utils/hubert.py", "--wav", str(wav), "--out", str(feat)], code, feat
        return [python, "wenet_infer.py", str(wav)], code / "data_utils", wav.with_name(wav.stem + "_wenet.npy")

    def infer_cmd(self, python, code, dataset, feat, ckpt, out, opts):
        return [python, "inference.py", "--asr", opts["asr"], "--dataset", str(dataset), "--audio_feat", str(feat), "--save_path", str(out),
                "--checkpoint", str(ckpt)], code

    def export_cmds(self, python, code, base, ckpt, out_dir, opts):
        return [([python, "pth2onnx.py", "--checkpoint", str(ckpt), "--onnx_path", str(out_dir / "unet.onnx"), "--asr", opts["asr"]], code)]

    def export_files(self, base, out_dir, opts):
        files = [out_dir / "unet.onnx"]
        if opts.get("asr") == "wenet":
            shutil.copyfile(self.encoder_file(base), out_dir / "encoder.onnx")
            files.append(out_dir / "encoder.onnx")
        return files

    def device_input(self, opts):
        asr = opts.get("asr") or ASR_DEFAULT
        out = {"audio_encoder": asr, "fps": FPS[asr], "size": FACE, "sample_rate": 16000, "model_file": "unet.onnx",
               "shapes": {"input": [1, 6, FACE, FACE], "audio": AUDIO_SHAPE[asr]}}
        if asr == "wenet":
            out["encoder_file"] = "encoder.onnx"
        return out
