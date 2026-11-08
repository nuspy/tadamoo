# Voice: text to speech

The engines are Kokoro, Piper, Chatterbox, XTTS v2, CosyVoice 3, Qwen3-TTS (0.6B and 1.7B), Orpheus 3B and Dia 1.6B. Their code is in
`voice/callbridge/engines/` (`catalog.py`, `tts_router.py`, `venv_tts.py` and the `*_tts_helper.py` scripts). Nothing here downloads: a
model is installed on request from Settings > Voice (see `models.md`) and only read afterwards.

## Where the weights are read

Every engine reads its weights from `<models folder>/tts/<model id>/`, the folder that `serve/voice_api.py` fills (marker
`.basalt-model.json`, rule in `service.md`). The engine-to-model table is `catalog.py` (`EngineSpec.model`):

| Engine | Folder under `<models folder>/tts/` | Loaded with |
|---|---|---|
| Kokoro | `kokoro/` (`.onnx` and `voices-v1.0.bin`) | the local files |
| Piper | `piper/` (one `<voice>.onnx` per voice) | `PiperVoice.load(<file>)` |
| Chatterbox | `chatterbox/` | `ChatterboxMultilingualTTS.from_local(<folder>, device)` |
| XTTS v2 | `xtts-v2/` | `load_checkpoint(checkpoint_dir=<folder>)` |
| CosyVoice 3 | `cosyvoice-3/` | `AutoModel(model_dir=<folder>)` |
| Qwen3-TTS | `qwen3tts-0.6b/` or `qwen3tts-1.7b/`, with `base/` and `custom-voice/` | `FasterQwen3TTS.from_pretrained(<folder>)` |
| Orpheus 3B | `orpheus-3b/`, the SNAC codec in `orpheus-3b/snac_24khz/` | `from_pretrained(<folder>, local_files_only=True)`; `SNAC.from_pretrained(<folder>)` |
| Dia 1.6B | `dia-1.6b/` | `from_pretrained(<folder>, local_files_only=True)` |

The size of Qwen3-TTS comes from the model chosen in Settings (`qwen3tts_size`). The Qwen3-TTS GGUF backend and its `--prefetch`
command are gone: they fetched weights the catalog does not install. The cache folder settings (`HF_HUB_CACHE`, `TTS_MODELS_DIR`) are
gone too; the helpers get `BASALT_VOICE_MODELS_DIR`, the three `*_OFFLINE` variables and `TTS_VENVS_DIR` (the folder of the Python
environments, where CosyVoice's code also lives). `engines/helper_models.py` is what the helpers use to find a model, a voice sample
or the Whisper checkpoint; it imports only the standard library.

## When a model is missing

- The engine raises `ModelNotInstalled` before it starts a helper process, and each helper checks again before it imports torch,
  transformers or any other heavy library. The text is always `<Model name> is not installed: install it from Settings > Voice.`
- The router does not fall back to another engine: asking for an engine whose model is missing, a voice that belongs to one, or no
  voice at all while that engine is chosen, raises the same error.
- `GET /status` of the service has, for `tts`, `installed: false` and the `reason`. For `vad` it also has `degraded: true` and
  `degraded_reason` (the same text plus "Speech detection uses the energy detector instead."): without Silero the energy detector runs.
- The TTS routes of `handy_compat` answer HTTP 409 with `{status: "error", code: "model_not_installed", model, message}` (`/tts/test`,
  `/models/load`); the TTS WebSocket sends `{type: "error", code: "model_not_installed", message}`. Other errors keep their old answers.
- `/tts/models` rows have `model` and `reason`, and `is_downloaded` is true only when the model is installed and the engine's Python
  environment and helper exist.

## Cloned voices

A sample is audio the person gave (`.wav`, `.mp3`, `.flac`, `.ogg`) and lives in `<models folder>/tts/voices/<name>.<ext>`. The routes
are unchanged: `GET /voices/cloned`, `POST /voices/clone` (`name`, `format`, `audio_b64`) and `DELETE /voices/cloned/<name>`. A name
must be one word-like token (letters, digits, `_`, space, `.`, `-`; no path, no leading dot) and the format one of the four above, or the
answer is 400; with no models folder the answer is 409. A transcript `<name>.txt` next to a sample is used by CosyVoice and Qwen3-TTS;
without it they transcribe the sample with the local Whisper small checkpoint (`stt/whisper-small`). Qwen3-TTS clones from the timbre
alone when that checkpoint is missing; CosyVoice reports the error.

## Selection and placement

`voice.json` reaches the service through `voice/settings_bridge.py` (see `service.md`). The chosen TTS model sets `tts_engine` and the
engine's `device` and `memory` fields: `ram` or `cpu` runs on the CPU, otherwise on the GPU. For `cuda:N` the helpers of the `tts`
service get `CUDA_VISIBLE_DEVICES=N` and see that GPU as `cuda:0`. When the file changes and a TTS router exists, `State.refresh`
calls `engines.aggiorna_tts`: the router makes the chosen engine the active one (no model is loaded), restarts the helpers whose
placement changed, closes all of them when the GPU changed, and follows a new models folder. With no voice and no engine in a request,
the active engine speaks.

## What cannot be run here

Nothing here loads a model, uses a GPU or plays audio. The tests (`voice/tests/test_tts_*.py`) replace torch, transformers, TTS, snac,
faster_qwen3_tts, whisper and chatterbox with fakes and a helper process with a fake `Popen`. Not proven on real weights: the helpers
in their own environments; `ChatterboxMultilingualTTS.from_local` and `SNAC.from_pretrained(<folder>)` taking a local folder;
`FasterQwen3TTS.from_pretrained` and CosyVoice's `AutoModel` reading nothing from the network once given a local folder (they get no
`local_files_only`, only the offline variables); Piper on `cuda:N` (it runs in the service process, which cannot hide other GPUs).
Chatterbox has no source or probe file in `serve/voice_catalog.py`, so it counts as installed only with a marker file. Its Python
environment is still looked for where the old setup left it (`chatterbox_venv_python`).
