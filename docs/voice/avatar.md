# The avatar

The avatar is the third voice function (`stt`, `tts`, `avatar`). It loads and unloads with the others (`loading.md`) and shows a person who
speaks with lip sync. Three ways to render it exist: MuseTalk on this PC from a video (this page), and Wav2Lip and visemes on the phone from a
photo (`gateway.md`; the PC only makes their assets and the viseme timeline).

## Parts

| Part | File |
|---|---|
| Worker (MuseTalk, its own Python environment) | `voice/avatar/avatar_worker.py`, code in `voice/avatar/musetalk` |
| Client of the worker, the frame subscription | `voice/callbridge/avatar_client.py` |
| The avatars folder | `voice/callbridge/avatar_store.py` |
| The weights it needs | `voice/callbridge/avatar_weights.py` |
| Preview (MJPEG) and virtual camera | `voice/callbridge/avatar_output.py` |
| The service object, the personality link | `voice/avatar_service.py`, `voice/persona_link.py` |
| The creation wizard | `voice/avatar_wizard.py` |
| Routes | `voice/avatar_routes.py` |
| Server side | `serve/voice_avatar.py` (files, stream, camera setting), `serve/voice_proxy.py` (JSON routes) |
| Page | `serve/web/voiceavatar.js`, `voiceavatar.css` |

## Loading and the offline rule

- The weights are read only from `<models folder>/avatar/musetalk-v15/`. `avatar_weights.REQUIRED` lists the 11 files: MuseTalk v1.5 (2), the
  SD VAE (2), the audio encoder (3), DWPose (1), face parsing (2) and the S3FD face detector (1). Nothing is fetched: the worker starts with
  `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` and `HF_DATASETS_OFFLINE`, and replaces `torch.hub.load_state_dict_from_url` and
  `torch.utils.model_zoo.load_url` with a function that raises, so a library that asks for a URL fails with the URL in the message.
- A weight that is missing keeps the function unloaded: `GET /functions` has `problem` and `POST /functions/apply` reports the plain reason
  (`The avatar is missing 2 weight file(s) in <folder>: s3fd/s3fd-619a316812.pth, ...`) and starts no process. The same goes for a missing
  environment: its Python is `avatar_python` of the config or the variable `TADAMOO_AVATAR_PYTHON` (the installer sets it).
- The face-parsing weights (`79999_iter.pth`, `resnet18-5c106cde.pth`) and the S3FD file have no source in `serve/voice_catalog.py`, so a
  download does not fetch them; they are put in the folder by hand until a source is chosen.
- The client puts `voice/avatar/musetalk` on `PYTHONPATH`, sets `MUSETALK_MODELS`, `AVATAR_CODE_DIR` and `AVATAR_STATE_DIR`, and gives the
  worker the GPU the person chose (`CUDA_VISIBLE_DEVICES`). Three files of the vendored code read `MUSETALK_MODELS` instead of `./models`
  (`preprocessing.py`, `face_parsing/__init__.py`, `sfd_detector.py`, which no longer downloads).
- `avatar_enabled` and `avatar_preload` of the config are the avatar function being on in `voice.json`; there is no separate flag. The worker
  starts only when the function is loaded and stops when it is unloaded or the service ends.
- Loading starts the worker and loads the avatar of the personality in `brain.persona` when it has one with a clip; otherwise the worker waits
  and `POST /avatar/start {persona}` chooses one.

## The avatars folder

`<user data>/voice/avatars/<name>/`: `avatar.json` (display name, the voice the wizard made), `clips/*.mp4`, `poster.jpg`,
`wav2lip/face.png` and `box.json`, `poses/`, and `prepared/` with one folder per prepared clip and `index.json`. A name is letters, digits,
`_` and `-` (not the name of a route such as `wizard`); nothing outside the folder is touched, and a link in it is not followed.
`GET /avatar/list` gives each avatar with its size and the total; `DELETE /avatar/<name>` frees it (409 while in use).

## The personality it follows

The service reads the personality of `brain.persona` from `GET /api/personas` on TaDaMoo (a route its token opens): the `avatar` (a folder
name) and the `voice` `{engine, voice}`. They are put on the config (`avatar_persona`, `avatar_voice_engine`, `avatar_voice_name`); the TTS
router's `voce_avatar_attivo` reads them, so the avatar speaks with the personality's voice. A personality without a voice uses the one the
wizard made for the avatar, then the TTS default. `GET /avatar/state` shows the link and, when it does not hold, why.

## Creation wizard

`POST /avatar/wizard/start`, then `POST /avatar/wizard/<draft>/<foto|pose|video|voce>` (multipart field `file`), then
`POST /avatar/wizard/<draft>/elabora {name, language}`; `GET` and `DELETE /avatar/wizard/<draft>`. Only the photo is needed.

- A photo is decoded and kept as JPEG (sides 64 to 8000 px, 20 MB); at most six poses. A video must be an MP4 that opens, 128 to 4096 px,
  up to 120 s and 500 MB. A voice sample is WAV, MP3, FLAC or OGG up to 25 MB (a WAV 3 to 120 s). Client file names are never used; a draft
  id is 32 hex digits; at most 8 drafts, those older than a day are removed.
- `elabora` writes the avatar: the poster, the straightened 96x96 face and the mouth box (a default from the Haar face detector that
  `POST /personas/<name>/wav2lip/bocca {p1, p2}` corrects with the two mouth corners), the clip and the poses.
- The voice sample is copied to `<models folder>/tts/voices/<name>.<ext>` (the cloning folder, same name rules as `POST /voices/clone`). The
  avatar's voice is that sample on the first installed cloning engine that speaks the language (CosyVoice, XTTS, Qwen3-TTS, Chatterbox, Dia);
  with none installed no voice is set and `voice_note` says why (the personality keeps its Kokoro voice). The page then offers "Use for a
  personality", which sets the personality's `avatar` and, when there is one, its `voice`.
- Preparing a clip for MuseTalk (frames, DWPose, latents, masks) is a job of the worker: `POST /avatar/prepare {avatar, clip}`,
  `GET /avatar/prepare` (state, `progress` with phase, percent and `eta_s`), `POST /avatar/prepare/cancel`. One at a time, cancellable
  even before it starts, needs the function loaded; the result is listed in `prepared/index.json`.

## Outputs

- Browser preview: `GET /avatar/preview.mjpg` is `multipart/x-mixed-replace`; the server relays it as `/api/voice/avatar/preview.mjpg`
  (this PC only, no token to the browser). Frames are encoded to JPEG only while someone watches, at most 15 a second, for up to 4 viewers.
  The Voice tab shows it in an `<img>` (read with `fetch` when an API key is set). The OpenCV window of the old worker is gone.
- Virtual camera: `voice.json` `avatar.virtual_camera` (default false; Settings > Voice > Avatar output) with pyvirtualcam, on Windows and
  Linux only (other systems say unsupported). It opens on the first frame once the avatar is loaded and closes when the setting goes off or the
  avatar stops. It installs no driver and no package: OBS Virtual Camera (Windows) or v4l2loopback (Linux) must be there, and the reason shows
  in `GET /avatar/output` when it cannot open.
- Frames for other code: `AvatarClient.frames.subscribe(callback)` returns the function that stops it; `callback(frame)` gets a `Frame` with
  `width`, `height`, `i420` (bytes), `bgr()` and `jpeg()`. It runs on the receiving thread, so it must return quickly; with no subscriber the
  worker is told to send nothing. The phone gateway's own socket (`POST /avatar/stream {on, port}`) still works beside it.

## Speaking, visemes, the phone

- `POST /avatar/speak {text, lang?}` synthesises with the TTS router and plays through the worker; 409 `not_loaded` otherwise.
  `POST /avatar/lipsync {wav, mute}` and `/avatar/interrupt` are the gateway's.
- `POST /avatar/visemes {text, duration_s, lang}` returns the viseme timeline of `viseme_gen.py` (espeak-ng when found, else Italian rules).
- `GET /avatar/<name>/assets.zip` is `poster.jpg`, `face.png` and `box.json` of an avatar in the folder, for the phone's Wav2Lip. The paths
  `GET /personas/<name>/poster|mouthbox|wav2lip/box|wav2lip/face` of callAPIcall are served on the same folder.

## Tests and what they leave out

`voice/tests/test_avatar_*.py` (a fake worker process speaks the JSON-lines protocol; fake frames, GPU readings and camera; the network is
blocked) and `serve/tests/test_voice_avatar_*.py`. Not proven here: the real rendering, the VRAM it takes, lip-sync quality, the virtual
camera, that the MuseTalk code makes no network call of its own beyond the two download functions that are blocked (DWPose's config names a
URL for a backbone it does not load when a checkpoint is given), and Linux.
