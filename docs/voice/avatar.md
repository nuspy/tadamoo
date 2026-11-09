# The avatar

An avatar is a person who speaks with lip sync. It is drawn in one of three ways, and the first two need no GPU and no function loaded:

| Mode | Where | Made from | Used by |
|---|---|---|---|
| `wav2lip` | on the device, an ONNX model | the photo and the voice | the Android app (first choice) |
| `visemes` | on the device, mouth sprites over the photo | the photo and a timeline from the PC | the Android app, the ESP32 (`esp32.md`) |
| `video` | on this PC, MuseTalk, sent as WebRTC video | a prepared video | an emergency only, and it uses VRAM |

The device declares what it can draw and degrades `wav2lip`, `visemes`, `video`, voice only (`gateway.md`, Render modes). The third voice
function (`avatar`, named "Avatar video from the PC (emergency, uses VRAM)", off by default) is the MuseTalk worker alone; it loads and unloads
with the others (`loading.md`), and the rest of this page names it where it matters. The gateway serves the first two whenever the service runs
and the personality's avatar folder has their files.

## Parts

| Part | File |
|---|---|
| Worker (MuseTalk, its own Python environment) | `voice/avatar/avatar_worker.py`, code in `voice/avatar/musetalk` |
| Client of the worker, the frame subscription | `voice/callbridge/avatar_client.py` |
| The avatars folder | `voice/callbridge/avatar_store.py` |
| The weights it needs | `voice/callbridge/avatar_weights.py` |
| Preview (MJPEG) and virtual camera | `voice/callbridge/avatar_output.py` |
| Acceleration (smaller and slower drawing, restored on this PC) | `voice/callbridge/enhance/`, `acceleration.md` |
| The service object, the personality link | `voice/avatar_service.py`, `voice/persona_link.py` |
| The creation wizard | `voice/avatar_wizard.py` |
| The engines (descriptors, choice per target), `engines.md` | `voice/avatar_engines/` |
| Mouth sprites from a photo, the phone's files and manifest | `voice/callbridge/avatar_visemes.py`, `avatar_assets.py`, `avatar_prep.py` |
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
`wav2lip/face.png` and `box.json`, `visemes/<V>.png` and `frame.json`, `poses/`, and `prepared/` with one folder per prepared clip and
`index.json`. A name is letters, digits, `_` and `-` (not the name of a route such as `wizard`, `models` or `phone`); nothing outside the folder
is touched, and a link in it is not followed.
`GET /avatar/list` gives each avatar with its size and the total; `DELETE /avatar/<name>` frees it (409 while in use).

## The personality it follows

The service reads the personality of `brain.persona` from `GET /api/personas` on TaDaMoo (a route its token opens): the `avatar` (a folder
name) and the `voice` `{engine, voice}`. They are put on the config (`avatar_persona`, `avatar_voice_engine`, `avatar_voice_name`); the TTS
router's `voce_avatar_attivo` reads them, so the avatar speaks with the personality's voice. A personality without a voice uses the one the
wizard made for the avatar, then the TTS default. `GET /avatar/state` shows the link and, when it does not hold, why.

## Creation wizard

`POST /avatar/wizard/start`, then `POST /avatar/wizard/<draft>/<foto|voce|video|pose>` (multipart field `file`), then
`POST /avatar/wizard/<draft>/elabora {name, language}`; `GET` and `DELETE /avatar/wizard/<draft>`. Only the photo is needed, and the page asks
in this order: name, photo, voice sample (optional), short video (optional). The poses are still accepted and no mode uses them.

- A photo is decoded and kept as JPEG (sides 64 to 8000 px, 20 MB); at most six poses. A video must be an MP4 that opens, 128 to 4096 px,
  up to 120 s and 500 MB. A voice sample is WAV, MP3, FLAC or OGG up to 25 MB (a WAV 3 to 120 s). Client file names are never used; a draft
  id is 32 hex digits; at most 8 drafts, those older than a day are removed.
- `elabora` writes the avatar at once, from the photo, with no GPU: the poster, the straightened 96x96 face and the mouth box (a default from
  the Haar face detector that `POST /personas/<name>/wav2lip/bocca {p1, p2}` corrects with the two mouth corners, and the sprites are made
  again on them), the 15 mouth sprites, the clip and the poses. It answers with `render_modes`, what the files allow. It prepares nothing for
  MuseTalk: a video, when given, waits for `POST /avatar/prepare`, which the person starts. Without OpenCV a photo is refused with
  `needs opencv` (501) and an avatar already made has no mouth files. A video is not used for the mouth shapes yet: they come from the photo.
- The voice sample is copied to `<models folder>/tts/voices/<name>.<ext>` (the cloning folder, same name rules as `POST /voices/clone`). The
  avatar's voice is that sample on the first installed cloning engine that speaks the language (CosyVoice, XTTS, Qwen3-TTS, Chatterbox, Dia);
  with none installed no voice is set and `voice_note` says why (the personality keeps its Kokoro voice). The page then offers "Use for a
  personality", which sets the personality's `avatar` and, when there is one, its `voice`.
- Preparing a clip for MuseTalk (the emergency video: frames, DWPose, latents, masks) is a job of the worker: `POST /avatar/prepare {avatar, clip}`,
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

## Speaking and the viseme timeline

- `POST /avatar/speak {text, lang?}` synthesises with the TTS router and plays through the worker; 409 `not_loaded` otherwise.
  `POST /avatar/lipsync {wav, mute}` and `/avatar/interrupt` are the gateway's.
- `POST /avatar/visemes {text, duration_s, lang}` returns the timeline of `viseme_gen.py` (espeak-ng when found, else Italian rules):
  `{type: "visemes", text, lang, duration_ms, visemes: [{t, v, dur}]}` with `t` and `dur` in milliseconds, covering `duration_ms`.

## The mouth sprites

`avatar_visemes.py`, from the photo alone (OpenCV and NumPy). The mouth corners (the default from the face detector, or the two the person
marked) give the mouth centre, its width `mw` and the roll of the head. The poster is cut in an upright frame, 2.2 mouth widths wide and 1.7 high
and at most 384 px wide, centred on the mouth. Each of the 15 visemes of `viseme_gen.VISEMI` is that frame with only the mouth changed:

- the lower lip drops by `open` mouth widths at the middle and not at the corners (a parabola), the upper lip rises by `lift`;
- the gap is redrawn as the inside of the mouth: dark, with upper teeth and a tongue in proportion to the opening;
- the mouth is widened or narrowed about its centre by `wide`; a closed mouth (`PP`) gets a darker line.

| Viseme | open | wide | | Viseme | open | wide |
|---|---|---|---|---|---|---|
| sil | 0 | 1 | | SS | 0.07 | 1.08 |
| PP | 0 | 0.96 | | nn | 0.10 | 1 |
| FF | 0.07 | 1 | | RR | 0.14 | 0.85 |
| TH | 0.10 | 1 | | aa | 0.42 | 1.04 |
| DD | 0.14 | 1 | | E | 0.26 | 1.08 |
| kk | 0.18 | 0.98 | | I | 0.12 | 1.14 |
| CH | 0.14 | 0.78 | | O, U | 0.30, 0.18 | 0.70, 0.56 |

`visemes/<V>.png` is RGBA: the alpha is an ellipse of 1.6 half-widths around the mouth with a soft edge, and the pixels outside it are the
photo's own. `visemes/frame.json` has `size`, `inv` (the 2x3 matrix from a sprite pixel to a poster pixel, the convention of `box.json`),
`mouth` (centre and width in sprite pixels), `angle_deg` and `visemes`. `GET /avatar/<name>/sprites.png` draws all of them over the photo,
labelled, which is what the page shows. Checked on a drawn face only: how they look on real photos is not proven.

## What a device downloads

- `GET /avatar/<name>/manifest.json`: `{version: 1, avatar, created, render_modes, files: {path: sha256}}`. The files are `poster.jpg`,
  `wav2lip/face.png`, `wav2lip/box.json`, `visemes/<V>.png`, `visemes/frame.json`. `render_modes` lists `wav2lip` (face and box present),
  `visemes` (the 15 sprites and the frame) and `video` (a clip is prepared). An avatar made before the sprites gets them on first request,
  keeping the mouth that was marked.
- `GET /avatar/<name>/assets.zip`: those files and `manifest.json`.
- `GET /avatar/phone[?persona=slug]`: the avatar of a personality (the one of Settings > Voice by default) and its `render_modes`; the gateway
  asks it.
- `GET /avatar/models/wav2lip.onnx`: the Wav2Lip model, with `Range`, `ETag` (the SHA-256) and `Cache-Control: private, no-cache`; 404
  `model_not_installed` with the reason when it is not in `<models folder>/avatar/wav2lip-int8/wav2lip_int8.onnx`. The catalog entry
  `wav2lip-int8` (service `avatar`, not selectable as the function's model) records size 36,664,106 and the SHA-256
  `2e297b31...6896f97`. It is non-commercial (the Wav2Lip licence is for research) and its source is undeclared, so it is marked to verify and
  nothing downloads it: it is put there by hand or by the importer, and it is not in this repository.
- The server relays the zip, the manifest and the model as `/api/voice/avatar/<name>/assets.zip|manifest.json` and
  `/api/voice/avatar/models/wav2lip.onnx`, to this PC and to a phone with its key (ranges and `If-None-Match` pass through, the service token
  does not leave the server). The sprite sheet and the poster are for this PC's page only.

## Tests and what they leave out

`voice/tests/test_avatar_*.py` (a fake worker process speaks the JSON-lines protocol; fake frames, GPU readings and camera; the network is
blocked), `voice/tests/test_avatar_visemes.py` (sprites, manifest) and `serve/tests/test_voice_avatar_*.py`. Not proven here: the real
rendering, the VRAM it takes, lip-sync quality, the look of the sprites on real photos, the model on a phone, the virtual camera, that the MuseTalk code makes no network call of its own beyond the two download functions that are blocked (DWPose's config names a
URL for a backbone it does not load when a checkpoint is given), and Linux.
