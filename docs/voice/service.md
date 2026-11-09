# The voice service

A Python process of its own (`voice/service.py`) that serves the control routes of `voice/callbridge/control_server.py` on loopback.
The desktop app starts it (see `desktop-app.md`); the server reaches it through two files. It starts in standby: no model is loaded and
no audio, GPU or phone hardware is opened, except that an enabled STT whose model is installed loads at once (`loading.md`). Importing the service pulls in no audio library, no torch and no onnxruntime.

## Start

```
python -m voice.service --settings <voice.json> --host 127.0.0.1 --port P --token-file F --info-file I [--default-models-dir D] [--no-gateway]
```

- `--host` must be loopback (`127.0.0.1`, `::1`, `localhost`); anything else is refused with exit code 2. `--port 0` picks a free port.
- The token is the text of `F`, trimmed. A missing or empty `F` is refused with exit code 2 and nothing is printed on stdout.
- When the server listens, one line goes to stdout: `{"ready": true, "port": P, "pid": N}`. The same moment `I` is written (atomically):
  `{"host", "port", "pid", "started", "gateway_port"}`. A clean stop removes `I` again, unless a newer service wrote its own.
- Logs go to stderr. Stops cleanly on SIGINT, SIGTERM, Ctrl+Break and, on Windows, the closing of the console, logoff and shutdown.
- `--default-models-dir` is the models folder to use when `voice.json` names none (see below).

## Files

| File | In | Written by |
|---|---|---|
| `voice.json` | user data folder | the server (`serve/voice_settings.py`); the service only reads it |
| `voice-service.token` | user data folder | the server at first use (`serve/voice_service.py`) or the app; 64 hex characters, owner-only where the system allows |
| `voice-service.json` | user data folder | the service |

The token is never returned by a route, never logged and never sent anywhere but to `127.0.0.1`.

## Routes of the service

Every route needs `Authorization: Bearer <token>`, except `GET /health` (`{"status": "ok"}`). The other routes are those of callbridge
(the call, session, avatar, analysis and meeting routes of the imported code); telephony is off until Settings > Voice turns it on (`telephony.md`), and nothing opens a modem or a phone link before that. `GET /functions` and
`POST /functions/apply` load, unload and measure the stt, tts and avatar functions (`loading.md`).
`GET /brain/state` and `POST /brain/say` belong to the brains of the voice and `POST /session/start` takes a `brain` (`chat.md`).
`GET /gateway` is the state of the phone gateway, which starts with the service on loopback (`gateway.md`).

`GET /status` replaces the callbridge status. It loads nothing:

```json
{"service": "voice", "state": "standby", "pid": 1234, "started": "2026-11-08T10:00:00+00:00", "models_dir": "D:\\Models\\voice", "offline": true,
 "services": {"stt": {"model": "whisper-small", "name": "Whisper small", "engine": "whisper", "device": "auto", "memory": "vram",
                      "installed": false, "reason": "Whisper small is not installed: install it from Settings > Voice."}}}
```

One entry for each of `stt`, `tts`, `avatar`, `diarization`, `vad`, `embedding`; `reason` is present only when `installed` is false.
`embedding` has no model in the catalog yet and says so.
`gateway` is the state of the phone gateway (`host`, `port`, `clients`, `voice`, `avatar`, ...), or `null` with `--no-gateway`.

## Settings

`voice/settings_bridge.py` reads `voice.json` (format in `models.md`) without importing `serve` or `tadamoo`. A missing, unreadable or
damaged file, or a bad value in it, gives the defaults and never an error. The service reads the file again whenever its size or
modification time changes, so a choice made in Settings > Voice shows in `/status` at once.

| Setting | Becomes |
|---|---|
| `stt` selected `whisper-<size>` | `stt_model` = `<size>` |
| `stt` device and memory | `stt_device` = `cuda` or `cpu`; `stt_gpu_memory` = `dedicated` (vram) or `shared` (ram) |
| `tts` selected engine | `tts_engine`, and the `<engine>_device` / `<engine>_gpu_memory` pair of that engine (`tts_*` for Kokoro); `qwen3tts_size` |
| `diarization` device | `diarization_device` |
| `models_dir` | `kokoro_model_dir`, `piper_model_dir`, `diarization_model_dir`, `stt_vad_model` |
| `brain` (mode, personality, folder, server address, hotkeys) | `brain_mode`, `brain_persona`, `brain_workspace`, `tadamoo_base_url` (`chat.md`); the level is the server's (`../permission-levels.md`) |
| `telephony` (transport, approval, disclosure, auto-answer, name) | `call_transport`, `outgoing_approval`, `ai_disclosure_on_start`, `incoming_auto_answer`, `call_on_behalf_of`, `call_audit_file` (`telephony.md`) |

`ram`, or the device `cpu`, means the CPU. `cuda:N` is not a `Config` field: the service gives the helper processes of that service
`CUDA_VISIBLE_DEVICES=N`, so a helper sees the chosen GPU as `cuda:0`.

`models_dir` is `voice.json`'s own value when it is an absolute path, else `--default-models-dir`, else `voice-models` next to `voice.json`.
The server's default (a `voice/` folder in its first models folder) is not known to the service: `GET /api/voice/service` reports
`models_dir_matches` so a difference shows.

## The offline rule

Models are read only from `<models folder>/<service>/<model id>/` and never downloaded. A model counts as installed when
`.basalt-model.json` there lists files that all exist with the recorded sizes, or, for a model put there by hand, when its probe files
exist. `voice/callbridge/models_store.py` holds this rule and a table of ids and probe files that a test in `serve/tests` keeps equal to
`serve/voice_catalog.py`.

- A loader that does not find its model raises `ModelNotInstalled`: `<Model name> is not installed: install it from Settings > Voice.`
  This covers the Whisper checkpoint (`<size>.pt`), Kokoro (the `.onnx` and `voices-v1.0.bin`, checked before a GPU helper is started),
  a Piper voice and the diarization models. The Whisper fallback loads the local file, never a name. The other TTS engines are in `tts.md`.
- Silero VAD is the one exception to raising: without it speech detection falls back to the energy detector, a warning gives the same
  message, and `/status` shows the `vad` service as `degraded` with the reason.
- The service and every helper process it starts get `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1` and
  `BASALT_VOICE_MODELS_DIR` (`models_store.helper_env`); the helpers of the heavy TTS engines get them last, so nothing set before can
  turn them off.
- Default folders of other programs (Handy, callAPIcall, the Whisper cache) are no longer searched.

## The server side

`serve/voice_service.py` has `info()`, `token_path()`, `ensure_token()` and `status()`. `GET /api/voice/service` (loopback only)
returns `{running, port, pid, status}`; `status` is the service's `/status` when it answers, else `{"reason": ...}` saying why it is not
reachable. It also creates the token file when it is missing. `call(method, route, body)` sends one request with the token to the running
service (`ServiceDown` when it is not there); `serve/voice_loader.py` uses it for the functions (`loading.md`).

## Tests

`voice/tests/test_service.py` (the app in-process, the network blocked), `test_service_process.py` (the real entry point on a free
loopback port), `test_settings_bridge.py` and `serve/tests/test_voice_service.py` (a stand-in service on loopback).
