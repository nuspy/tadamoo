"""FeatherTalk (github.com/anliyuan/FeatherTalk, commit 7a8bb043): a 144x144 visual model trained on the face of one person, driven by FeatherHuBERT, a
small audio encoder that is the same for everyone. The command lines are those of its README and of `process.py`, `train.py`, `inference.py` and
`FeatherTalk-CPP/tools/export_models.py` at that commit; the MNN step is `FeatherTalk-CPP/convert_mnn_models.sh` run with MNNConvert when the person set one.
See docs/voice/engines-personalised.md."""
from __future__ import annotations

import os
from pathlib import Path

from .personalised import BATCH, EPOCHS_DEFAULT, EPOCHS_RANGE, MNN_ENV, Check, PersonalisedEngine

FPS = 25                                  # the video the preprocessing requires
FACE = 144                                # IMAGE_SIZE of model.py
WEIGHTS = "feather_hubert.pth"
TOKENS, FEATURE_DIM = 40, 1024            # the fixed interface of the exported models (FeatherTalk-CPP/README.md)


class FeatherTalkEngine(PersonalisedEngine):
    ID = "feathertalk"

    def options(self, asr: str | None = None, epochs: int | None = None) -> dict:
        if asr not in (None, "feather_hubert"):
            raise ValueError("FeatherTalk has one audio encoder, FeatherHuBERT")
        epochs = EPOCHS_DEFAULT if epochs is None else epochs
        if isinstance(epochs, bool) or not isinstance(epochs, int) or not EPOCHS_RANGE[0] <= epochs <= EPOCHS_RANGE[1]:
            raise ValueError(f"epochs is a whole number from {EPOCHS_RANGE[0]} to {EPOCHS_RANGE[1]}")
        return {"asr": "feather_hubert", "fps": FPS, "epochs": epochs, "batch": BATCH}

    def weights_file(self, base: str) -> Path:
        return self.weights_dir(base) / WEIGHTS

    def audio_checks(self, base: str, opts: dict) -> list[Check]:
        have = bool(base) and self.weights_file(base).is_file()
        return [Check("audio", have, "the FeatherHuBERT weights are installed (trained on Chinese speech only: Italian is unverified)" if have else
                      "the FeatherHuBERT weights are not installed: install `feathertalk-weights` from Settings > Voice",
                      "i pesi FeatherHuBERT sono installati (addestrati solo su voce cinese: l'italiano non è verificato)" if have else
                      "i pesi FeatherHuBERT non sono installati: installa `feathertalk-weights` da Settings > Voice",
                      install="" if have else "feathertalk-weights", where=str(self.weights_dir(base)) if base else "")]

    def preprocess_cmd(self, python, code, base, video, opts):
        return [python, "data_utils/process.py", str(video), "--feather_hubert_checkpoint", str(self.weights_file(base))], code

    def train_cmd(self, python, code, base, dataset, ckpt_dir, opts, resume):
        argv = [python, "train.py", "--dataset_dir", str(dataset), "--save_dir", str(ckpt_dir)]
        argv += ["--resume", str(resume)] if resume else ["--model", "feathertalk"]
        return argv + ["--epochs", str(opts["epochs"]), "--batchsize", str(opts["batch"])], code

    def features_cmd(self, python, code, base, wav, work, opts):
        feat = work / "feat.npy"
        return [python, "data_utils/feather_hubert/feather_hubert.py", "--wav", str(wav), "--checkpoint", str(self.weights_file(base)), "--out", str(feat)], code, feat

    def infer_cmd(self, python, code, dataset, feat, ckpt, out, opts):
        return [python, "inference.py", "--dataset", str(dataset), "--audio_feat", str(feat), "--checkpoint", str(ckpt), "--save_path", str(out)], code

    @staticmethod
    def mnn_converter() -> str:
        path = os.environ.get(MNN_ENV, "").strip()
        return path if path and Path(path).is_file() else ""

    def export_cmds(self, python, code, base, ckpt, out_dir, opts):
        cmds = [([python, "FeatherTalk-CPP/tools/export_models.py", "--feather-checkpoint", str(self.weights_file(base)), "--visual-checkpoint", str(ckpt),
                  "--model", "auto", "--output-dir", str(out_dir)], code)]
        mnn = self.mnn_converter()
        for onnx, target in (("feather_hubert.onnx", "feather_hubert.mnn"), ("feathertalk_144.onnx", "feathertalk_144_fp16.mnn")):
            if mnn:
                cmds.append(([mnn, "-f", "ONNX", "--modelFile", str(out_dir / onnx), "--MNNModel", str(out_dir / target), "--fp16", "--bizCode", "FeatherTalk"], out_dir))
        return cmds

    def export_files(self, base, out_dir, opts):
        names = ["feather_hubert.onnx", "feathertalk_144.onnx"] + (["feather_hubert.mnn", "feathertalk_144_fp16.mnn"] if self.mnn_converter() else [])
        return [out_dir / n for n in names]

    def device_input(self, opts):
        mnn = bool(self.mnn_converter())
        return {"audio_encoder": "feather_hubert", "fps": FPS, "size": FACE, "sample_rate": 16000, "audio_step_ms": 20, "audio_tokens": TOKENS,
                "feature_dim": FEATURE_DIM, "model_file": "feathertalk_144_fp16.mnn" if mnn else "feathertalk_144.onnx",
                "encoder_file": "feather_hubert.mnn" if mnn else "feather_hubert.onnx",
                "shapes": {"input": [1, 6, FACE, FACE], "audio": [1, TOKENS, FEATURE_DIM], "waveform": [1, "samples"]}}
