# PC avatar engines: Wav2Lip v2, SadTalker, ER-NeRF

Three engines of the registry (`voice/avatar_engines/registry.py`) draw the avatar on this PC from the code of Linly-Talker. Nothing is bundled and nothing
downloads or loads by itself: the files are catalog entries (Settings > Voice, service `avatar`) that the person installs on request, and a model is loaded
only when an engine is asked to prepare or render.

## Why the code is not vendored

Linly-Talker's own files are MIT, but its `LICENSE` is the only licence file in the pinned tree, and its README asks to comply with the licences of every component
it refers to. The code these engines run is that of other projects: the Wav2Lip v2 model derives from Wav2Lip (research and non-commercial) through
`primepake/wav2lip_288x288`, the SadTalker `src/` from SadTalker, the `NeRF/` folder from ER-NeRF (with DeepSpeech feature code that points to the original's
restrictions), and none of them has its own licence file or header in the tree. A component whose licence is missing or non-commercial is not vendored, so
all three stay catalog installs on request (`verify`), and ultralytics and the original Wav2Lip code are never vendored. If a component's licence is later
confirmed, its pack moves to `voice/avatar_engines/vendor/<engine>/` with a `VENDORED.md` and its code entry is removed.

## How they run

- Pack: the upstream code installed under `<models folder>/avatar-engines/<engine>/code/` from URL sources pinned to Linly-Talker commit `dc831b3` (MIT),
  each file with its blob SHA-256. The lists are in `serve/voice_linly_packs.py`; they are the import closure of the upstream script plus the data files it opens,
  found by reading the code. Empty files are left out (the downloader needs a size), so a package without its empty `__init__.py` imports as a namespace package.
- Environment: a Python of its own named by `TADAMOO_AVATAR_ENGINES_PYTHON` (PyTorch with CUDA and the packages in the catalog notes; ffmpeg on the PATH).
  Nothing installs it. Each engine starts one runner of `voice/avatar_engines/runners/` in it as a subprocess: offline, the code pack as working directory, the
  environment's folder first on the PATH, stdout read as JSON events (`progress`, `done`, `error`) and tqdm bars as progress.
- One at a time: a process lock shared by every engine; a second request while one runs is refused (`EngineBusy`). Stop kills the whole process tree.
- Frames: the runner writes the frames of the video the upstream script made to a file (header `AVI4`, width, height, then I420, as `avatar_client` receives them)
  and the engine yields `avatar_client.Frame` objects at the model's own size, scaled down but never up to the height asked. `pack.feed_frames(frames, sink, fps,
  sleep)` hands them to a sink at the frame rate: `AvatarFrames.publish` for the preview and the web, `Enhancer.feed` for the enhance stage, which scales up.
- `status_of` reads files and the environment only and says in plain words, in English and Italian, what is missing: the Python, ffmpeg, each catalog entry,
  the photo, and for ER-NeRF the checked CUDA modules and the trained checkpoint.
- The gateway does not serve these engines yet: the descriptors keep `integrated=False`, so `selection.for_mode` still answers with the integrated engine of a mode.
  Choosing one in Settings stores the choice and shows its availability.

## Loading, and the Set up panel

- Only the selected engine is loaded. `ActiveEngine` (`voice/avatar_engines/active.py`) makes one engine the one in use and unloads the previous one first
  (`AvatarEngine.unload`: its process tree stopped, its avatar forgotten); a pack engine keeps no process or model between calls, so a render starts a runner and
  ends it. Listing engines or reading their status starts nothing.
- Each engine card has a Set up panel (`serve/web/voicesetup.js`), asked nothing until opened. It lists, item by item, the engine Python, ffmpeg, each catalog file and
  (ER-NeRF) PyTorch3D and the four CUDA modules; Install runs the server's download flow (plan, licence acknowledgement, confirm, SHA-256) for a file that has a source and
  says where to put the others by hand; Check loads the engine once in its environment and records it (`check.json`, `modules.json` for ER-NeRF).
- The engine Python is the variable `TADAMOO_AVATAR_ENGINES_PYTHON`, else the path saved from the panel in `<models folder>/avatar-engines/python.json`.
- Routes of the voice service (`voice/avatar_engines/setup_routes.py`, on the proxy's list): `GET /avatar/engines/<engine>/setup`, `POST /avatar/engines/<engine>/check`,
  `POST /avatar/engines/python`. ER-NeRF is trained from the Avatar card's section through `/avatar/train/plan|start|cancel` and `status`
  (`voice/avatar_training.py`): the same plan, confirm, progress and cancel as the other personalised engines, with the steps `check`, `prepare`, `head`, `torso`.
- Licence notes are kept here and in the code, not on the page: the page shows only the restrictions that protect the person (Wav2Lip v2 is non-commercial; its face
  detector is run by ultralytics, AGPL-3.0).

## Wav2Lip v2 (`wav2lip_v288`)

- Runs Linly-Talker's `TFG/Wav2Lipv2.py` on the avatar's `poster.jpg` and the audio (`run`, then the video is decoded). Native size 288 px per the Linly-Talker
  README; the script itself builds 256 px face crops. PC and web (a PC-rendered stream); not Android.
- Catalog: `wav2lipv2-code` (26 files, 95,938 bytes), `wav2lipv2-weights` (`checkpoints/wav2lipv2.pth` of the Hugging Face repository `Kedreamix/Linly-Talker`),
  `wav2lipv2-yolo-face` and `wav2lipv2-landmarks` (no source stated anywhere read: placed by hand).
- Licence: the code is Linly-Talker's MIT, but the model derives from Wav2Lip (research and non-commercial) through `primepake/wav2lip_288x288`, and its weights'
  provenance is not stated, so it is treated as non-commercial. The face detector is loaded by ultralytics, which is AGPL-3.0; the licence of
  `yolov8n-face.pt` is not stated. All of it is marked verify.
- Not proven: that `checkpoints/wav2lipv2.pth` exists in the repository (the README's tree does not list it; the download plan fails clearly if not), that the
  closure is complete on a real environment, and any speed or quality.

## SadTalker (`sadtalker`)

- Runs `TFG/SadTalker.py` `test2` at 256 px with the cropped preprocessing, no enhancer, on the photo and the audio. It reloads all its models on every call: a
  sentence at a time, not live. PC only.
- Catalog: `sadtalker-code` (49 files), `sadtalker-weights` (`SadTalker_V0.0.2_256.safetensors`, `mapping_00229-model.pth.tar`) and `sadtalker-gfpgan`
  (`alignment_WFLW_4HG.pth`, `detection_Resnet50_Final.pth`, installed inside the code folder because the code reads `./gfpgan/weights`), from the same repository
  as the README's tree names them.
- The README's step `scripts/sadtalker_download_models.sh` is not in that commit and the wrapper does not call it; the weight list comes from the code
  (`src/utils/init_path.py`) and the README's tree. Licences of the code and weights are not verified.
- Not proven: the closure, that the weights load, the packages the code needs beyond those listed in the catalog notes, any speed or quality.

## ER-NeRF (`er_nerf`)

- A NeRF trained for each person from a video (hours), drawn by `TFG/NeRFTalk.py`. PC only. Needs PyTorch3D and the four CUDA modules `freqencoder`,
  `gridencoder`, `raymarching`, `shencoder` built on this PC: very fragile with the versions of Python, PyTorch, CUDA and the compiler. A training checks
  them first (`ernerf_run.py --check`) and records the result in `<models folder>/avatar-engines/ernerf/modules.json`.
- Catalog: `ernerf-code` (the whole `NeRF/` folder and the wrapper, 60 files), `ernerf-audio-encoder` (`checkpoints/audio_visual_encoder.pth`), and by hand
  `ernerf-bfm` (the 3DMM folder built from the Basel Face Model, which needs a registration) and `ernerf-face-parsing`.
- A training is four steps (`ErNerfEngine.training_plan`, `prepare`): the build check, the upstream `data_utils/process.py`, then the head and the torso with
  `runners/ernerf_train.py`. Files go to `<avatar>/engines/er_nerf/` (`data/`, `head/`, `torso/`, `trained.json`). The data path must have no space: the upstream
  preparation does not quote it.
- Licence: the DeepSpeech feature code inside points to the original's licence restrictions; the rest is not verified.
- Not proven, and the weakest part: ER-NeRF's own `main.py` is not in Linly-Talker, so `ernerf_train.py` is a stand-in written from the wrapper's `Options` and the
  `Trainer` signature (optimizer, scheduler and EMA values are not checked against ER-NeRF); the default 200000 iterations are the wrapper's `Options.iters`;
  the data preparation's deepspeech features need TensorFlow and a model file the upstream fetches itself; that a checkpoint it writes renders with the wrapper.

## Tests

`voice/tests/test_avatar_engine_{pack,wav2lipv2,sadtalker,ernerf}.py` use fakes (a fake process, empty files, a frames file) and one real pair of tiny processes
for the process tree; `serve/tests/test_avatar_engine_packs.py` checks the catalog entries, the folders and the planner, and re-hashes every blob when the scratch
clone of Linly-Talker is on the machine. None of them starts a server, loads a model, uses a GPU or touches the network.
