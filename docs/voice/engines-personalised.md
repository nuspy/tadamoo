# Personalised engines: Ultralight-Digital-Human and FeatherTalk

A personalised engine draws one person's face and mouth from the audio. It learns that person from a video of them, on this PC's GPU, once per avatar. The other engines
(`engines.md`) need no training. The avatar stays usable with them while a training runs.

## Where the code and the weights are

| What | Where | How it arrives |
|---|---|---|
| The upstream scripts | `voice/avatar_engines/vendor/<engine>/` | in the repository, byte for byte except the patches `VENDORED.md` records (one, below); it lists origin, commit and the SHA-256 of every file |
| Face detector and landmark weights | `<models folder>/avatar-engines/landmarks/` | catalog entry `engine-landmarks`, Install button |
| FeatherHuBERT weights | `<models folder>/avatar-engines/feathertalk/weights/` | entry `feathertalk-weights`, Install button |
| Ultralight's wenet encoder | `<models folder>/avatar-engines/ultralight/weights/encoder.onnx` | by hand: the README names a Google Drive file, no address or SHA-256 |
| torchvision's VGG19 (`vgg19-dcbb9e9d.pth`) | `<models folder>/avatar-engines/torch-home/hub/checkpoints/` | by hand: both `train.py` load it for the perceptual loss and nothing downloads it |
| HuBERT Large (Ultralight with HuBERT) | `<models folder>/avatar-engines/hf-home/hub/` | by hand: `hubert.py` loads `facebook/hubert-large-ls960-ft` by name |

Pinned commits: Ultralight `5aa8b5a8aae4b003b7de5750abeac5343a7b38e0`, FeatherTalk `7a8bb0439eb5da5e10601168df329a7052b7edf3`. Nothing is bundled or downloaded by itself. The scripts
open their weights by relative path, so before running one the engine copies the vendored folder to `<models folder>/avatar-engines/<engine>/run/` and the weights into place there
(only what changed is copied). The scripts run from that folder, offline (`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`), with `TORCH_HOME` and `HF_HOME` inside the models folder, as
subprocesses of a Python environment of your own with PyTorch and CUDA. Nothing here installs it. That Python is, in order: the variable
`TADAMOO_AVATAR_ENGINES_PYTHON` when set, else the one chosen in the Set up panel (`<models folder>/avatar-engines/python.json`, shared with the other engines that run upstream code).

## The engines

| | Ultralight-Digital-Human | FeatherTalk |
|---|---|---|
| Audio encoder | wenet (video at 20 fps, light) or HuBERT Large (25 fps, better per the author) | FeatherHuBERT, 3.36M parameters, 16 kHz in, 1024 features every 20 ms |
| Face the model draws | 160 px | 144 px |
| Export for a phone | `pth2onnx.py` to ONNX (`unet.onnx`, plus `encoder.onnx` with wenet) | `export_models.py` to ONNX; MNN with MNNConvert when `TADAMOO_MNNCONVERT` names it (the flags of `convert_mnn_models.sh`) |
| Licence | Apache 2.0 by the README badge; no LICENSE file (`vendor/ultralight/LICENSE-NOTE.md`) | Apache 2.0, LICENSE file in `vendor/feathertalk/` |

FeatherHuBERT's published weights were trained on Chinese speech only and are incomplete, per their authors: the quality in Italian is unverified.

## The files of one avatar

`<avatar>/engines/<engine>/`: `dataset/` (the video re-encoded at the engine's frame rate, its frames, landmarks and audio features; `ready.json` says what for), `checkpoints/`
(every 5 epochs and `last.pth`), `export/` (what a phone needs, with `export.json`), `trained.json` (written when the training ends), `log_tail.txt`. With `export.json` present the
manifest of the avatar gets an `engines` entry (`gateway.md`, Personalised engines).

## The steps and the commands

1. `ffmpeg -y -i <clip> -r <fps> -c:v libx264 -crf 14 -pix_fmt yuv420p -c:a aac dataset/train.mp4` (the preprocessing refuses any other frame rate).
2. `python data_utils/process.py <video> --asr <wenet|hubert>` (Ultralight) or `--feather_hubert_checkpoint <weights>` (FeatherTalk): frames, landmarks, features.
3. `python train.py --dataset_dir ... --save_dir ... [--asr ...] [--model feathertalk] --epochs <n> --batchsize 16`; a later run adds `--resume checkpoints/last.pth` and continues after the last saved epoch.
4. Export for a phone (above). A failed export is recorded in `trained.json` and does not undo the training; `POST /avatar/train/export` makes it again.

Progress is read from the `Epoch n/N` of the output. The job reports the phase, the percent (conversion 2, preprocessing 10, training 83, export 5), the epoch and the time left.

## The plan and the estimate

`POST /avatar/train/plan {avatar, engine, epochs?, asr?, fresh?}` returns, and starts nothing:

- the video: the avatar's longest clip. Under 2 minutes or over 10 is refused with its length; 3 to 5 minutes is best. The wizard accepts videos only up to 120 s, so a longer clip has to be put in the avatar's `clips` folder;
- the duration, labelled an estimate: frames (minutes x 60 x fps) x epochs x seconds per frame per epoch. Neither project states a speed. Until a training has been timed on this PC the range is
  an assumption made here (0.003 to 0.02 s per frame per epoch); after one it is the measured figure (`avatar-engine-timing.json`). Preprocessing is not included. Epochs default to 200, the value of the upstream examples;
- the GPU memory: `null`, "not measured", because the upstream documents state no figure; the free memory now is shown;
- what will be created, and the checks: idle, the weights, the Python, ffmpeg, the VGG19 file, the GPU readable.

`POST /avatar/train/start {token, confirm: true}` starts it only if the token belongs to a plan that is unchanged and has no failed check. One training runs at a time.
`POST /avatar/train/cancel` ends it and every process it started; stopping the service does the same. The dataset and checkpoints stay, and a new plan continues from them (`fresh` discards them).

## Only the engine in use is loaded

An engine starts nothing when it is described, asked its status or selected. `render` and `export_for_device` start their scripts when called and leave nothing behind (each render loads
its model again: it is for a sentence at a time, not live). `ActiveEngine.use(id)` (`active.py`) makes an engine the one in use and calls `unload()` on the one that was: for these engines that ends their
running scripts and every child process. `release()` does it for the one in use. A training is a separate job the person confirmed: choosing another engine does not end it.

## The Set up panel

Each engine card in the Avatar tab has one. `POST /avatar/train/setup {engine, asr?, avatar?, probe?}` gives every prerequisite as a list (OK or to fix, with the catalog entry whose
Install button fixes it, or the folder to put a file in), where the Python came from, and with `probe` what that Python has (it imports the packages and asks PyTorch for CUDA: it starts that
Python, so only on request). `POST /avatar/train/python {path}` chooses the Python (an existing file; nothing is run). The Install button opens the catalog's own plan: files, size, folder,
licence text, the acknowledgement, then the download with its SHA-256 check.

## What is not proven

No GPU, model, real video or network was used: the tests run fake scripts that take the arguments of the real ones. Not proven: that the upstream scripts run end to end here,
how long a training takes, the memory it needs, the quality of either model (Italian included), an ONNX or MNN file on a phone. Known upstream defect, left unedited: both `train.py` need the VGG19 file.

## Windows

The scripts are made to run on Windows with one patch and one setting. Patch (recorded in `vendor/ultralight/VENDORED.md`, Patches): `data_utils/get_landmark.py` had `from os import wait3`, never
used and missing on Windows, so the import was removed; `voice/tests/test_avatar_engine_vendor.py` checks that putting the line back gives the upstream blob. Setting: the scripts run with `PYTHONUTF8=1`, because
`data_utils/wenet_infer.py` reads its UTF-8 word list with `open()` and no encoding, which on Windows would use the locale's. Scanned and found fine: no `fork`, `wait*`, `fcntl`, signals,
`os.system`, `shell=True` or hard-coded `/` paths; `ffmpeg` is started as a list; `DataLoader` workers use spawn on Windows (slower start, the scripts have the `__main__` guard).
`FeatherTalk-CPP/convert_mnn_models.sh` is a shell script: it is not run here (MNN conversion uses `TADAMOO_MNNCONVERT`). Not proven on a real Windows run.
