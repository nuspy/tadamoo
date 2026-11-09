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
| `embedding` | Multilingual E5 small (proposed for the internal RAG, which does not read it yet) |

Each entry was read from the voice service's own code. What that code does not say is not guessed: the entry has `verify` (the
fields still to check: `license`, `size`, `sha256`, `source`, `license-card`, `face-parse`, `dac-local`) and the page shows it. XTTS v2 has the
Coqui Public Model License and is marked `noncommercial`.

## Grounded entries

A grounded entry has real sources: a Hugging Face repository pinned to a commit with the SHA-256 of every file (`pins`; the LFS id, or
the hash of a small file read at that commit), or a URL with its SHA-256 and size. `serve/downloads.py` refuses a pinned file whose
LFS id differs from the pin and uses the pin as the hash of a file Hugging Face does not hash. `verify` keeps `sha256` where the hash
is not the author's own: GitHub releases and the authors' servers publish no digest, so the value is the one that independent
Hugging Face copies give for a file of the same size (it is checked at download, so a wrong value installs nothing).

| Entry | Source | Licence and what to know |
|---|---|---|
| Whisper (5 sizes) | the openai-whisper 20250625 addresses; each folder is the file's SHA-256, sizes from the server | MIT, code and weights |
| Kokoro | kokoro-onnx release `model-files-v1.0`: `kokoro-v1.0.fp16.onnx`, `voices-v1.0.bin` | Apache-2.0 (hexgrad's weights), MIT (the conversion's code) |
| Piper | `rhasspy/piper-voices`: Riccardo x_low, Serena medium (Italian), LJ Speech medium (en_US), Cori medium (en_GB), each `.onnx` + `.onnx.json` | each voice follows its dataset: M-AILABS (BSD-style), CC BY 4.0, public domain |
| Silero VAD v4 | `files/silero_vad.onnx` at the commit of the `v4.0` tag of `snakers4/silero-vad`, saved as `silero_vad_v4.onnx` | MIT |
| Diarization | `csukuangfj/sherpa-onnx-pyannote-segmentation-3-0` (`model.onnx`) and the CAM++ file of the sherpa-onnx release | MIT (pyannote) and Apache-2.0 (CAM++) |
| MuseTalk | 11 files: MuseTalk v1.5, sd-vae-ft-mse, whisper-tiny, DWPose, `vivym/face-parsing-bisenet`, ResNet-18 (pytorch.org), S3FD (its author's server) | `noncommercial`: see below |
| Chatterbox | `ResembleAI/chatterbox`: the six files `ChatterboxMultilingualTTS.from_local` of chatterbox-tts 0.1.7 reads (v3 and English-only files left out) | MIT |
| XTTS v2 | `coqui/XTTS-v2`: model, config, vocab, DVAE, mel stats, speakers, `LICENSE.txt` (samples left out) | CPML, `noncommercial` |
| CosyVoice 3 | `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`: twelve files (YAML, `llm.pt`, `flow.pt`, `hift.pt`, two tokenizers, `CosyVoice-BlankEN/`) | Apache-2.0 |
| Qwen3-TTS 0.6B, 1.7B | `Qwen/Qwen3-TTS-12Hz-<size>-Base` (`base/`) and `-CustomVoice` (`custom-voice/`), every file but the README | Apache-2.0 |
| Orpheus 3B | `unsloth/orpheus-3b-0.1-ft` and the codec `hubertsiuzdak/snac_24khz` (`snac_24khz/`) | Apache-2.0 on the card, the Llama 3.2 base terms unchecked (`verify: license`); SNAC MIT |
| Dia 1.6B | `nari-labs/Dia-1.6B-0626` (transformers files) and the codec `descript/dac_44khz` (`dac_44khz/`) | Apache-2.0; DAC MIT on GitHub, none stated on its Hugging Face copy |
| Multilingual E5 small | `intfloat/multilingual-e5-small`, the ONNX files | MIT |

- **Piper runtime.** The voice service imports `piper` (`PiperVoice`, `SynthesisConfig`): the pip package `piper-tts` 1.3 or newer
  (older ones have no `SynthesisConfig`), which carries the espeak-ng data, needs no binary and is GPL-3.0-or-later. It is not part of
  the download. Paola (it_IT) and Alba (en_GB) are left out: they were finetuned from the Lessac voice, whose data is for research only.
  A voice is named after its file, and other voices go in the same folder by hand.
- **Gated.** `pyannote/segmentation-3.0` and `pyannote/speaker-diarization-3.1` ask for a Hugging Face login and accepted terms; the
  catalog uses the converted copy, which does not. Nothing in the catalog is gated. A gated repository is planned with a `token`
  problem: the person pastes a Hugging Face token (or accepts the terms on the model's page) before the Install button works.
- **MuseTalk.** Its code and trained weights are free for any purpose, but the face-parsing network (BiSeNet `79999_iter.pth`) was
  trained on CelebAMask-HQ, which is for non-commercial research, so the whole entry is `noncommercial`. The original Google Drive link
  of that file is not fetchable; the copy used has the same hash as dozens of others. S3FD states no licence in what was read.
- **Embedding.** Multilingual E5 small is 12 layers, 384 numbers, 512 tokens, trained for retrieval (texts start with `query: ` or
  `passage: `, mean pooling, normalised). It was preferred to `paraphrase-multilingual-MiniLM-L12-v2` (128 tokens, trained for
  paraphrase) and `BAAI/bge-m3` (2.3 GB). The RAG consumer is a later step: `DEFAULTS["embedding"]` stays empty and nothing loads it.
- **TTS pins.** Each TTS entry was read from the Hugging Face tree of its repository at one commit: the LFS id and size of a big file,
  and for a small file (read at that commit, its git blob id checked) the SHA-256 and size. A source records `sizes` and the entry's
  `size` is their sum (what the Loader plans the VRAM from until a figure is measured; Dia's weights are stored in 32 bits and loaded in
  16). The file lists follow what each engine loads, read from its code, not from a run. These entries have no `probe`: they are
  recognised by the marker of a finished download.
- **Gated.** The official `canopylabs/orpheus-3b-0.1-ft` and the multilingual Orpheus research releases (Italian, French, German,
  Korean, Chinese, Hindi) ask for a login and list no file hashes, so Orpheus uses unsloth's open copy; the research releases are not
  in the catalog.
- **Dia and the codec.** The processor of Dia names its codec `descript/dac_44khz` by Hub name; `dia_tts_helper.py` loads it with
  `local_files_only` and does not point it to `dac_44khz/` yet, so `dac-local` stays on until the engine does.
- **Not found.** Wav2Lip INT8 (36,664,106 bytes) has no public copy with that size and hash, so it keeps `verify` and no source.

## Sources and what may be downloaded

- A **Hugging Face** source (`repo`, `revision`, optional `files`, `subdir`, `strip`, `pins`) is either grounded (a commit and a pin per
  file, see above) or still at revision `main`. A plan resolves `main` to a commit (`/api/models/<repo>/revision/main`), lists the tree
  at that commit and takes each file's size and SHA-256 from it. Without `files` the whole repository is taken, hidden files left out.
  Paths inside the repository are kept, except for a `strip` prefix.
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
- `meetings`: the meeting bot and the copilot (`speak`, `proactive`, `consent_mode`, `consent_window_s`, `on_behalf_of`, and the rest), with the careful defaults of `meetings.md`.

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
| `GET`, `POST /api/voice/meetings` | the rules of the meeting bot and the copilot; the POST takes any of their fields (`meetings.md`) |
| `GET /api/voice/download/plan?id=` | the plan of one model |
| `POST /api/voice/download` | `{id, token, confirm, ack, noncommercial}`; 202 with the status |
| `GET /api/voice/download/status` | phase, bytes, speed, the log's tail |
| `POST /api/voice/download/cancel` | stops the running download; its `.part` files stay |

The tests are `serve/tests/test_voice_catalog.py`, `test_voice_settings.py`, `test_voice_api.py` (a local server stands in for Hugging Face
and a file host) and `test_voice_page.py`.
