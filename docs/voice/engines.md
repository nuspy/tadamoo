# Avatar engines

An engine is one way to draw the avatar. The registry lists the ones TaDaMoo knows, says where each runs and what it costs, and the person
chooses one per kind of screen. The rendering code of each engine is added one at a time; the descriptor, the choice and the gateway rule are here.

## Parts

| Part | File |
|---|---|
| Descriptor `EngineInfo` and the interface `AvatarEngine` | `voice/avatar_engines/base.py` |
| The table of engines, the defaults | `voice/avatar_engines/registry.py` |
| Is an engine usable for this avatar | `voice/avatar_engines/status.py` |
| The choice per target, what a device gets | `voice/avatar_engines/selection.py` |
| The choice in `voice.json`, listing and selecting | `serve/voice_settings.py`, `serve/voice_avatar.py` |
| The service's list | `GET /avatar/engines` in `voice/avatar_routes.py`, `avatar_engines` in `GET /status` |
| The card | `serve/web/voiceengines.js`, `voiceengines.css` (Settings > Voice), one line in the Voice tab's Avatar card |

## The descriptor

`id`, `name`, `kind` (`generic` or `personalized`), `runs_on` (a subset of `pc`, `web`, `android`, `esp32`), `weight_mb` (None: not measured),
`performance` (`{en, it}` text), `native_resolution` (None: not stated), `resolutions` (the output heights that can be chosen) and
`default_resolution`, `license` (`id`, `noncommercial`, `verify`, `note`), `training` (`needed`, `video_minutes` as (min, max) or None, `gpu`),
`needs` (reasons, each `{en, it}`), `mode` (the gateway render mode it is served as, None: not offered on a device yet), `requires` (what
`status_of` looks for) and `integrated` (its code and weights are part of this build). `validate()` refuses an incomplete or contradictory one.

A number is written only when it is a fact: a size measured on a file, a figure the project states (the text says "per the project"), or a height
this build offers. The rest is None or "Not measured here".

`status_of(avatar_dir, models_base=None)` gives `{available, reason, reason_it}` and reads files only. An engine that is not integrated is never
available. The requirements:

| `requires` | Looks for |
|---|---|
| `viseme_sprites` | `poster.jpg` and the 15 sprites with `frame.json` |
| `wav2lip_assets` | `poster.jpg`, `wav2lip/face.png`, `box.json`, and the model file in `<models folder>/avatar/wav2lip-int8/` |
| `musetalk_prepared` | the 11 MuseTalk weights and a prepared clip |
| `trained` | `<avatar>/engines/<id>/trained.json`, written when a training finished |
| `none` | nothing |

## The table

Heights are output heights in pixels; a model draws at its own size and the acceleration stage (`acceleration.md`) scales the picture to the
chosen height, so lower is lighter.

| Id | Kind | Runs on | Size | Native | Licence | Integrated |
|---|---|---|---|---|---|---|
| `visemes` | generic | pc, web, android, esp32 | no model | - | own code | yes |
| `wav2lip_gan96` | generic | pc, web, android | 37 MB (36,664,106 bytes) | 96 | non-commercial, provenance of the ONNX to verify | yes |
| `wav2lip_v288` | generic | pc, web, android | not measured | 288 | to verify (newly trained weights) | no |
| `ultralight` | personalized, 3 to 5 min of video, GPU | pc, web, android | not measured | - | Apache-2.0 (encoder: its own) | no |
| `feathertalk` | personalized, 3 to 5 min of video, GPU | pc, web, android | not measured | 144 | Apache-2.0 | no |
| `musetalk_15` | generic | pc, web | not measured | 256 | MIT (third-party models keep theirs) | yes |
| `sadtalker` | generic | pc | not measured | - | to verify | no |
| `er_nerf` | personalized, hours of training, GPU | pc | not measured | - | to verify | no |

FeatherTalk's audio encoder, FeatherHuBERT (3.36M parameters), has Chinese-only incomplete weights: Italian quality is unverified. ER-NeRF needs
PyTorch3D and CUDA modules compiled on this PC and breaks with the versions of Python, PyTorch, CUDA and the compiler. MuseTalk is "over 30 fps on
a GPU" per its project, not measured here.

## The choice

`voice.json` `avatar`: `engines` `{pc, web, android, esp32}` (ids), `resolution` `{...}` (heights) and `fps_hint` (5 to 60, default 25).
Defaults: pc `musetalk_15` at 480, web `visemes` at 240, android `wav2lip_gan96` at 96, esp32 `visemes` at 96. An engine that does not run on its
target, or a height it does not offer, is refused with the field's code and a message in both languages; the file is not touched. When an engine
is chosen without a height, the old one is kept if the new engine offers it, else the target's default, else the engine's own.

| Route | Does |
|---|---|
| `GET /api/voice/avatar/engines` | `{avatar, engines: [descriptor + status], selection, resolutions}`; the service describes, the server adds the choice |
| `POST /api/voice/avatar/engines/select {target, engine, resolution?}` | one target; answers `{selection, resolutions}` |
| `GET` and `POST /api/voice/avatar` | `engines`, `resolution`, `fps_hint` with the other avatar settings, checked as a whole |
| `GET /avatar/engines` on the service (proxy list) | the same list, with the choice the service runs with |

The server keeps a copy of the table's ids, targets and heights (`serve/voice_settings.py`) because it cannot import the voice package;
`voice/tests/test_avatar_engines_selection.py` keeps the two equal.

## At the gateway

A device is chosen by its `device` kind (`android`, `esp32`, `web`, `pc`). Among the modes it declares and the PC serves, the mode of its
engine goes first (`visemes` or `wav2lip`); the emergency video stays last. `ready` and `render_mode` carry `engine` and `resolution`: the engine
that draws the mode served, and its height (empty and 0 for voice only). An engine that is not integrated serves the integrated engine of its mode
instead, and that one is named. A device may declare `engine` and `resolution` in `auth`; they count when the engine runs on its kind and offers
the height. With nothing configured the device's own order stands. The fields are additive (`gateway.md`, Render modes).

## Adding an engine

1. Add its `EngineInfo` to `INFOS` in `registry.py` with only facts in it, and its row to the table of `serve/voice_settings.py`.
2. Subclass `AvatarEngine`: `prepare`, `render`, `export_for_device` raise `NotImplementedForNow` until written.
3. Set `integrated` and, for a device mode, add it to `MODE_ENGINE` only when it is the engine of that mode.
4. Add its requirement to `status.py` if none fits, and its tests to `voice/tests/test_avatar_engines_*.py`.

## Not proven here

The speed and size of every engine but the Wav2Lip file, the licences marked to verify, the look of any engine on a real face, the cards on a real
phone, and any engine running: only the descriptors, the choice, the gateway rule and the page were tested (fake sockets and files, no GPU).
