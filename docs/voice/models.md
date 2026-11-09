# Voice models

Settings > Voice chooses the model each voice service uses, says where it is loaded and installs it on request. The page talks to the
`/api/voice/*` routes, which are answered only on the PC the server runs on. The code is `serve/voice_catalog.py` (the catalog),
`serve/voice_settings.py` (what is chosen), `serve/voice_api.py` (the routes' logic) and the download engine of `serve/downloads.py`.

## Services and the catalog

Services: `stt`, `tts`, `avatar`, `diarization`, `vad`, `embedding`. Every catalog entry has an id, a service, an engine, a name, a
family, an approximate size (display only), a license (id, name, English and Italian text), `noncommercial`, languages, notes, the
text of its environment requirement, its sources and its `probe` files.

| Service | Entries |
|---|---|
| `stt` | Whisper tiny, base, small, medium, large-v3 |
| `tts` | Kokoro, Piper, Chatterbox, XTTS v2, CosyVoice 3, Qwen3-TTS 0.6B and 1.7B, Orpheus 3B, Dia 1.6B |
| `avatar` | MuseTalk v1.5 weights |
| `diarization` | sherpa-onnx (pyannote segmentation and CAM++) |
| `vad` | Silero VAD v4 |
| `embedding` | none: the voice code names no embedding model |

Each entry was read from the voice service's own code. What that code does not say is not guessed: the entry has `verify` (the
fields still to check: `license`, `size`, `sha256`, `source`, `license-card`, `face-parse`) and the page shows it. XTTS v2 has the
Coqui Public Model License and is marked `noncommercial`.

## Sources and what may be downloaded

- A **Hugging Face** source (`repo`, optional `files`, optional `subdir`) stays at revision `main` in the catalog. A plan resolves
  the commit then (`/api/models/<repo>/revision/main`), lists the tree at that commit and takes each file's size and SHA-256 from
  it. Without `files` the whole repository is taken, hidden files left out. Paths inside the repository are kept.
- A **URL** source has a fixed SHA-256 and a name. Its size comes from the server (a HEAD request) when the catalog has none. The
  Hugging Face token is never sent to a URL source.
- An entry is refused (a `verify` problem in its plan, a 422 on download) when it has no source, when a URL source has no SHA-256,
  or when a source is an archive: unpacking is not built.

## The download

The rules are those of `serve/downloads.py`: a plan first (files, sizes, free disk, access), `<name>.part` files resumed with a range
request and renamed only when the SHA-256 matches, one download at a time, the token held only in memory. A start needs the plan's
`token`, `confirm: true` and `ack: true` (the license read); a `noncommercial` model needs `noncommercial: true` as well. The files go to
`<models folder>/<service>/<model id>/`. A finished download writes `.basalt-model.json` there (the names and sizes it wrote); a model is
**installed** when the marker's files all exist with those sizes, or, for a model put there by hand, when its `probe` files exist.

## The settings file

`voice.json` in the user data folder, replaced whole on every write:

```json
{"models_dir": "D:\\Models\\voice", "services": {"tts": {"selected": "kokoro", "device": "auto", "memory": "vram", "enabled": true}}}
```

- `models_dir`: an absolute path, existing or creatable. Default: `voice/` in the first models folder of Basalt, else `voice-models/` in
  the user data folder. Changing it moves nothing.
- `selected`: a model id of that service; the default is Whisper small for `stt`, Kokoro for `tts`, MuseTalk for `avatar`.
- `device`: `auto`, `cpu` or `cuda:N`; `memory`: `vram` or `ram`. The CPU with `vram` is refused. `diarization`, `vad` and `embedding`
  default to the CPU and RAM, the others to `auto` and `vram`.

- `enabled`: a boolean, whether the function is loaded with the others (`loading.md`); off when absent, except for an installed `stt`.
- `telephony`: `transport`, `outgoing_approval`, `ai_disclosure_on_start`, `incoming_auto_answer` and `on_behalf_of`, with the careful defaults of
  `telephony.md` (telephony off, calls of the agent asked, the AI said first, no call answered by itself).

An unknown or invalid value is refused with a 400 whose body has `code`, `message` and `message_it`.

## Routes

| Route | Does |
|---|---|
| `GET /api/voice/models` | the folder, the services with their models (`installed`, `verify`, `downloadable`, license, languages, notes) and the download status |
| `POST /api/voice/select` | `{service, model}` |
| `POST /api/voice/placement` | `{service, device, memory}` |
| `POST /api/voice/enabled` | `{service, enabled}` |
| `POST /api/voice/models-dir` | `{path}` |
| `GET`, `POST /api/voice/telephony` | the rules of phone calls; the POST takes any of their fields (`telephony.md`) |
| `GET /api/voice/download/plan?id=` | the plan of one model |
| `POST /api/voice/download` | `{id, token, confirm, ack, noncommercial}`; 202 with the status |
| `GET /api/voice/download/status` | phase, bytes, speed, the log's tail |
| `POST /api/voice/download/cancel` | stops the running download; its `.part` files stay |

The tests are `serve/tests/test_voice_catalog.py`, `test_voice_settings.py`, `test_voice_api.py` (a local server stands in for Hugging Face
and a file host) and `test_voice_page.py`.
