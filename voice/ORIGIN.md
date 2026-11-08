# voice/: where the code comes from

The voice service of TaDaMoo starts as a snapshot of callAPIcall (`C:\Projects\callAPIcall`, branch `StreamingTTS`, commit `63ed73eb`), imported with `git archive`, so only committed
files came across. The plan is `piano_voce.md` at the root of the repository.

## What was imported

| Here | From callAPIcall |
|---|---|
| `callbridge/` | `call-bridge/callbridge/` (102 files: engines, telephony, meeting bot, copilot, dictation, clipboard, avatar client, gateway, API) |
| `tests/` | `call-bridge/tests/` (9 files, 90 tests; run with `python -m pytest voice/tests`) |
| `main.py`, `requirements.txt`, `config.json.example` | `call-bridge/` |
| `avatar/avatar_worker.py`, `avatar/SETUP-VENV.md` | `avatar/` |
| `avatar/musetalk/` | `avatar/MuseTalk/` (a vendored copy of MuseTalk at upstream commit `0a89dec4`, with callAPIcall's patches; MIT, its `LICENSE` and `VENDORED.md` are kept) |

## What was left out, and why

- `config.json`: it holds the values of the old Telegram wrapper and the live setup of one PC. A new configuration is built by the service; the example file has placeholders only.
- Models and weights (`silero_vad_v4.onnx`, `wav2lip_int8.onnx`, every download) and `fetch_models.py`, the script that downloaded them: nothing is bundled and nothing is downloaded unless the person asks from Settings > Voice (plan, section 3).
- The Tauri Control Center (`gui/`), the Capacitor phone app (`app/`), Handy-API, the Hermes skills and the WSL scripts: replaced by TaDaMoo's own pages and app.
- Scripts in `.bat`, PowerShell and `.vbs`: the repository adds none; the C# application and the Python service take their place.
- `android-controller/` (the dialer APK), `esp32-terminal/`, `drivers/`: imported in their own phases (telephony, ESP32), not here.
- Runtime files (`sospeso.on`, `.phone_cache.json`, `dump-voce.on`) and build output.

## Rules for what is here

- No secret enters the repository: tokens and keys go to the credential store.
- The package keeps the name `callbridge` for now, so the imported tests run unchanged; it is renamed and split into the plan's layout as each phase moves its part.
- Code is moved, then changed: each phase keeps the imported tests passing, or replaces them with tests of the new behaviour.
