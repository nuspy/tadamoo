# PC avatar engines: Wav2Lip v2, SadTalker, ER-NeRF

Three engines of the registry (`voice/avatar_engines/registry.py`) draw the avatar on this PC from the code of Linly-Talker. Their code is part of the repository
(vendored); their weights are catalog entries (Settings > Voice, service `avatar`) that the person installs on request. Nothing downloads or loads by itself: a model
is loaded only when an engine is asked to prepare or render.

## The code is vendored

The Linly-Talker files each engine runs are in `voice/avatar_engines/vendor/<engine>/` (`wav2lipv2`, `sadtalker`, `ernerf`), taken byte for byte from the raw blobs of
Linly-Talker commit `dc831b3` (the paths are those of its tree: `TFG/`, `src/`, `NeRF/`), like the Ultralight and FeatherTalk code. Each folder has:

- `VENDORED.md`: origin, commit, the SHA-256 and size of every file, what is not vendored and why. No patches: the files are unedited, and some keep their CRLF line ends
  (`vendor/.gitattributes` turns text conversion off so a checkout hashes the same everywhere).
- `LICENSE`: Linly-Talker's MIT file, unchanged, and `LICENSE-NOTE.md`: what is known of the licence of the code it carries, stated plainly and marked "to confirm".
- The file list is the import closure of the upstream script the engine runs plus the data files that code opens, found by reading the code (26, 49 and 59 files, about 96,
  252 and 486 KB). Skipped: the live viewer and its audio input (`NeRF/nerf_triplane/gui.py`, `asr.py`), the enhancer path of Wav2Lip v2 (`face_enhancer.py`, `videoio.py`, used only
  with `enhance=True`), the first Wav2Lip model, the S3FD detector, the training and evaluation scripts of arcface_torch, and the rest of Linly-Talker.
- `ultralytics` (AGPL-3.0, the face detector of Wav2Lip v2) is not vendored: `TFG/Wav2Lipv2.py` imports it and the engine environment provides it.

The licence is not a blocker in this experimental phase: the notes live in the code (the module docstrings, `LICENSE-NOTE.md`), and the page shows nothing about them beyond the
protective notes that were already there (Wav2Lip v2 is non-commercial; its detector is AGPL-3.0; the download flow asks for the non-commercial acknowledgement).
Anything given to others or used commercially needs the licences settled first.

## How they run

- Run folder: before a check, a render or a training, `pack.stage_code` copies the vendored files (not the two notes) into `<models folder>/avatar-engines/<engine>/code/`, only
  the files that differ. The folder also holds the weights the code reads by relative path (`gfpgan/weights`, `checkpoints/`, the ER-NeRF 3DMM and face-parsing files), which the
  catalog installs there; a copy never touches them. Listing engines or reading their status copies nothing.
- Environment: a Python of its own named by `TADAMOO_AVATAR_ENGINES_PYTHON` (PyTorch with CUDA and the packages in the catalog notes; ffmpeg on the PATH). Nothing installs it.
  Each engine starts one runner of `voice/avatar_engines/runners/` in it as a subprocess: offline, the run folder as working directory, the environment's folder first on the PATH,
  stdout read as JSON events (`progress`, `done`, `error`) and tqdm bars as progress.
- One at a time: a process lock shared by every engine; a second request while one runs is refused (`EngineBusy`). Stop kills the whole process tree.
- Frames: the runner writes the frames of the video the upstream script made to a file (header `AVI4`, width, height, then I420, as `avatar_client` receives them) and the engine
  yields `avatar_client.Frame` objects at the model's own size, scaled down but never up to the height asked. `pack.feed_frames(frames, sink, fps, sleep)` hands them to a sink at the
  frame rate: `AvatarFrames.publish` for the preview and the web, `Enhancer.feed` for the enhance stage, which scales up.
- `status_of` reads files and the environment only and says in plain words, in English and Italian, what is missing: the Python, ffmpeg, the vendored code, each weight entry, the
  photo, and for ER-NeRF the checked CUDA modules and the trained checkpoint.
- The gateway does not serve these engines yet: the descriptors keep `integrated=False`, so `selection.for_mode` still answers with the integrated engine of a mode. Choosing one in
  Settings stores the choice and shows its availability.

## Loading, and the Set up panel

- Only the selected engine is loaded. `ActiveEngine` (`voice/avatar_engines/active.py`) makes one engine the one in use and unloads the previous one first
  (`AvatarEngine.unload`: its process tree stopped, its avatar forgotten); a pack engine keeps no process or model between calls, so a render starts a runner and
  ends it. Listing engines or reading their status starts nothing.
- Each engine card has a Set up panel (`serve/web/voicesetup.js`), asked nothing until opened. It lists, item by item, the vendored code, the engine Python, ffmpeg, each catalog file and
  (ER-NeRF) PyTorch3D and the four CUDA modules; Install runs the server's download flow (plan, licence acknowledgement, confirm, SHA-256) for a file that has a source and
  says where to put the others by hand; Check loads the engine once in its environment and records it (`check.json`, `modules.json` for ER-NeRF).
- The engine Python is the variable `TADAMOO_AVATAR_ENGINES_PYTHON`, else the path saved from the panel in `<models folder>/avatar-engines/python.json`.
- Routes of the voice service (`voice/avatar_engines/setup_routes.py`, on the proxy's list): `GET /avatar/engines/<engine>/setup`, `POST /avatar/engines/<engine>/check`,
  `POST /avatar/engines/python`. ER-NeRF is trained from the Avatar card's section through `/avatar/train/plan|start|cancel` and `status`
  (`voice/avatar_training.py`): the same plan, confirm, progress and cancel as the other personalised engines, with the steps `check`, `prepare`, `head`, `torso`.

## Wav2Lip v2 (`wav2lip_v288`)

- Runs Linly-Talker's `TFG/Wav2Lipv2.py` on the avatar's `poster.jpg` and the audio (`run`, then the video is decoded). Native size 288 px per the Linly-Talker
  README; the script itself builds 256 px face crops. PC and web (a PC-rendered stream); not Android.
- Code: `vendor/wav2lipv2/` (26 files, 95,938 bytes). Weights, catalog entries: `wav2lipv2-weights` (`checkpoints/wav2lipv2.pth` of the Hugging Face repository
  `Kedreamix/Linly-Talker`), `wav2lipv2-yolo-face` and `wav2lipv2-landmarks` (no source stated anywhere read: placed by hand, under `models/` of the engine folder, which the
  runner passes as `pretrained_model_dir`).
- Licence (to confirm, see `vendor/wav2lipv2/LICENSE-NOTE.md`): the model derives from Wav2Lip (personal, research and non-commercial use per its README) through
  `primepake/wav2lip_288x288` (MIT); the face detector is loaded by ultralytics (AGPL-3.0); the licence of `yolov8n-face.pt` is not stated.
- Not proven: that `checkpoints/wav2lipv2.pth` exists in the repository (the README's tree does not list it; the download plan fails clearly if not), that the
  closure is complete on a real environment (only the imports were followed by reading, and the runner's calls were compared with the vendored signatures), and any speed or quality.

## SadTalker (`sadtalker`)

- Runs `TFG/SadTalker.py` `test2` at 256 px with the cropped preprocessing, no enhancer, on the photo and the audio. It reloads all its models on every call: a
  sentence at a time, not live. PC only.
- Code: `vendor/sadtalker/` (49 files, 252,307 bytes). Weights, catalog entries: `sadtalker-weights` (`SadTalker_V0.0.2_256.safetensors`, `mapping_00229-model.pth.tar`) and `sadtalker-gfpgan`
  (`alignment_WFLW_4HG.pth`, `detection_Resnet50_Final.pth`, installed inside the run folder because the code reads `./gfpgan/weights`), from the same repository
  as the README's tree names them.
- The README's step `scripts/sadtalker_download_models.sh` is not in that commit and the wrapper does not call it; the weight list comes from the code
  (`src/utils/init_path.py`) and the README's tree. Licence (to confirm, see `vendor/sadtalker/LICENSE-NOTE.md`): SadTalker's repository is Apache 2.0; the weights are not checked.
- Not proven: the closure, that the weights load, the packages the code needs beyond those listed in the catalog notes, any speed or quality.

## ER-NeRF (`er_nerf`)

- A NeRF trained for each person from a video (hours), drawn by `TFG/NeRFTalk.py`. PC only. Needs PyTorch3D and the four CUDA modules `freqencoder`,
  `gridencoder`, `raymarching`, `shencoder` built on this PC: very fragile with the versions of Python, PyTorch, CUDA and the compiler. A training checks
  them first (`ernerf_run.py --check`) and records the result in `<models folder>/avatar-engines/ernerf/modules.json`.
- Code: `vendor/ernerf/` (the `NeRF/` folder with its CUDA sources and the wrapper, 59 files, 486,227 bytes); the person builds the four modules from the sources in the run
  folder. Weights, catalog entries: `ernerf-audio-encoder` (`checkpoints/audio_visual_encoder.pth`), and by hand
  `ernerf-bfm` (the 3DMM folder built from the Basel Face Model, which needs a registration) and `ernerf-face-parsing`.
- A training is four steps (`ErNerfEngine.training_plan`, `prepare`): the build check, the upstream `data_utils/process.py`, then the head and the torso with
  `runners/ernerf_train.py`. Files go to `<avatar>/engines/er_nerf/` (`data/`, `head/`, `torso/`, `trained.json`). The data path must have no space: the upstream
  preparation does not quote it.
- Licence (to confirm, see `vendor/ernerf/LICENSE-NOTE.md`): ER-NeRF is MIT; the DeepSpeech feature code inside is "based on VOCA code" and points to its restrictions.
- Not proven, and the weakest part: ER-NeRF's own `main.py` is not in Linly-Talker (and its `NeRF/` differs from the upstream `nerf_triplane`), so `ernerf_train.py` is a
  stand-in written from the wrapper's `Options` and the `Trainer` signature (optimizer, scheduler and EMA values are not checked against ER-NeRF); the default 200000 iterations are the wrapper's `Options.iters`;
  the data preparation's deepspeech features need TensorFlow and a model file the upstream fetches itself; that a checkpoint it writes renders with the wrapper.

## Tests

`voice/tests/test_avatar_engine_{pack,wav2lipv2,sadtalker,ernerf}.py` use fakes (a fake process, empty files, a frames file) and one real pair of tiny processes
for the process tree. `voice/tests/test_avatar_engine_vendor_linly.py` re-hashes every vendored file against its table, checks that nothing else (no weight, no media) is there,
that the scripts parse and their local imports resolve inside the folder, that the runners call what the vendored scripts define, and that the run folder is filled correctly.
`serve/tests/test_avatar_engine_packs.py` checks the catalog entries. None of them starts a server, loads a model, uses a GPU or touches the network.
