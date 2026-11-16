> Reference: [README](../../README.md).

## Voice and avatars

TaDaMoo can listen, speak and show a face. The voice stack lives in `voice/` (a Python process of its own, the **voice service**, started
by the app and idle until you ask) and in the **Voice** tab and Settings > Voice. Nothing is loaded, opened or downloaded until you choose it;
every model comes from the folder you set for the voice models, checked against a pinned hash where the catalog has one. The reference is in
[docs/voice/](../voice/service.md).

- **Three voice functions**, each switched on or off by its own box: speech recognition (Whisper), speech synthesis and the avatar. One command
  loads or unloads them and a VRAM arbiter keeps them from taking the engine's memory ([loading](../voice/loading.md)).
- **Speech synthesis**: Kokoro, Piper, XTTS v2, Chatterbox, CosyVoice 3, Qwen3-TTS, Orpheus 3B and Dia 1.6B, one voice per personality, with
  cloned voices kept in the models folder ([TTS](../voice/tts.md), [models](../voice/models.md)).
- **Spoken language that follows you**: the avatar switches to the language you speak, or keeps the one you ask for ("speak French"), with a
  hysteresis so a stray word does not change it. The engine never changes by voice; a voice that lacks the language falls back to Piper
  ([language](../voice/language.md)).
- **The avatar**: drawn on the phone with Wav2Lip or mouth shapes (no GPU), on this PC with MuseTalk, Wav2Lip v2, SadTalker, ER-NeRF and the
  personalised Ultralight-Digital-Human and FeatherTalk, trained once from a video of the person ([avatar](../voice/avatar.md),
  [engines](../voice/engines.md), [personalised](../voice/engines-personalised.md)).
- **Personalities**: a name, a character, a voice, an avatar, a short and a long term memory (encrypted on disk). Knowledge archives belong to a project, not to a personality.
  **Import from callAPIcall** brings personas, videos, memory and the prepared avatar over ([import](../voice/import.md)).
- **Phone, ESP32 and the desktop**: the Android app's Avatar tab and an ESP32 terminal talk to a WebSocket gateway on the PC, hold to talk or
  continuous, with barge-in ([gateway](../voice/gateway.md), [ESP32](../voice/esp32.md)); dictation with a hotkey, the clipboard history
  and push-to-talk run on the desktop ([desktop](../voice/desktop.md)).
- **Meetings and phone calls**: the assistant can join Meet, Teams or Zoom as an announced guest and suggest answers, and place or answer
  calls through a GSM modem or the phone, behind one policy (approval, disclosure of the AI, premium numbers refused, an audit without
  transcripts) ([meetings](../voice/meetings.md), [telephony](../voice/telephony.md)).
- **Safe by default**: one guard checks the host and the origin of every request (`server.allowed-hosts`, `server.allowed-origins`), the
  Android key is kept in the Keystore, and [the security review](../voice/security-review.md) lists each finding and its status.

Made and tested with stand-ins (no real model, GPU, phone, modem or meeting): the real microphone, hotkeys and overlay, the on-phone runtimes,
the ESP32 firmware (never compiled), the virtual camera and the GPU engines are **not proven** on real hardware; each page says so.

## Requirements

- **A GPU one of the two engines can run.** The **Basalt-Plus** engine needs an **NVIDIA Blackwell GPU** of the 12.x
  family (compute capability 12.x: GeForce RTX 50 series, RTX PRO 6000 Blackwell, DGX Spark): its kernels are built only for
  that family, and on any other GPU the Loader, the wizard, the start script and the app say so before starting, and load
  nothing with it. Data-centre Blackwell (10.x, B200) is refused: its kernels are not built.
  Other hardware runs through the **Strata** engine, installed from a local folder: NVIDIA **RTX 40** (Ada), **RTX 30**
  (Ampere) and **RTX 20** (Turing), **Apple silicon**, **AMD**, **Intel**, and machines with **unified memory** (one pool
  shared by CPU and GPU). The Loader shows one Models tab for each engine the PC can run.
- **Windows 10 or 11 x64** (WDDM driver) or **Linux x86-64** for the engine, the server and the web UI. The scripts and
  the app are Windows only.
- Enough RAM for the experts that do not fit in VRAM (about 50 GiB for the 3-bit files, with resident experts).
- Python 3.10 or newer with the packages of `requirements.txt`, in a virtual environment `.venv` in the repository
  folder (`python -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`).
- For the app: the .NET 8 Desktop Runtime and Microsoft Edge WebView2 Runtime (without WebView2 the app opens the
  web UI in the default browser).
- Building: Windows in [docs/WINDOWS.md](../WINDOWS.md) (Visual Studio v143 tools, CUDA 13.1, CMake, Ninja), Linux
  in [Build](upstream-basalt.md#build) below. The app: `dotnet publish tools\windows\desktop -c Release -o tools\windows\desktop\publish`.

## Compatibility

- **Basalt-Plus engine** (`.basalt` files, Qwen3.8-Flash-Next and the Qwen3.6 path): NVIDIA Blackwell 12.x only, Windows x64 and
  Linux x86-64. Its kernels are built for compute capability 12.0, and the FP4 module for the whole 12.x family, so a build
  runs on a later minor version such as the DGX Spark's 12.1; no run on a Spark was made. CUDA 13.1 is the documented
  toolkit; 13.4 also builds it and passes the kernel parity programs, with no measurable speed difference on the model tried.
- **Strata engine** (second engine in the Loader, installed from a local folder; also reads the Qwen3.6 and DeepSeek V4.1
  models): NVIDIA RTX 40 (compute capability 8.9), RTX 30 (8.6) and RTX 20 (7.5), Apple (Metal), AMD (HIP), Intel (SYCL)
  and unified-memory machines. The Loader picks the tab from the hardware it detects: RTX 30/40, Mac, AMD, Intel, Unified
  memory. Run and checked here only on an NVIDIA Blackwell GPU with a Qwen3.6 GGUF that carries the MTP layer; the tabs for
  the other hardware are in place and tested against a stand-in server, but no run on those machines was made. A DGX Spark
  (compute capability 12.1) is recognised as unified memory and offered Strata as well as Basalt-Plus (a build for 12.x
  runs on a later minor version); no run on one was made. A Mac release is not built: Mac users compile from the repository.
- **Add-ons in the setup**: Openweight Canary, Billion Context (needs Node.js), REA and Node.js; none of them needs a GPU.
- Known limit: the Qwen3.6 path on the Basalt-Plus engine can crash with prompt chunks of 2048 tokens or more; `--chunk 512` works.
