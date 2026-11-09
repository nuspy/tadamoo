# The ESP32 terminal

A third terminal beside the PC and the phone: a board with a screen, a microphone and a speaker that shows the avatar and carries the voice. It
runs only the client. Speech recognition, the brain, speech synthesis and the mouth-shape timeline stay on the PC. Nothing here has been built
or run: the source in `esp32/terminal/` is imported as it was, and what follows says what exists in it and what does not.

## What the device does

- Shows the avatar as **visemes**: the photo with one of 15 mouth sprites drawn over it at the times the PC sends. No GPU anywhere, and no
  video: it is the avatar mode of the ESP32 (`docs/voice/avatar.md`; the PC video, MuseTalk, is an emergency mode a board cannot decode well).
- Sends the microphone as PCM16 and plays the PCM16 of the answer.
- Draws the screen; a tap is a press-to-talk.

The board of the source is a Guition JC4880P443C (ESP32-P4 with an ESP32-C6 for Wi-Fi, 4.3 inch 480x800 ST7701S display, GT911 touch, ES8311
audio codec, 16 MB flash, 32 MB PSRAM), built with ESP-IDF 5.5 and ESP-Brookesia over LVGL.

## The protocol it speaks

The one every client speaks, `gateway.md`: JSON text frames and binary PCM16 little-endian mono frames on a WebSocket.
`voice/tests/fake_device.py` is a device that does exactly this and the gateway tests run it, so the list below is executable.

1. Connect to `/api/voice/gateway/ws` of the TaDaMoo server with the device key (`Authorization: Bearer <key>` in the handshake). The gateway
   itself listens on loopback only; the board never reaches it directly.
2. First message, within 10 seconds:
   `{"type":"auth","render_modes":["visemes"],"device":"esp32"}`. A device that declares no mode gets the voice only.
3. Receive `auth_ok`, then `ready`; with an avatar `ready.render_mode` is `visemes` and `ready.avatar_name` names the folder to download:
   `GET /api/voice/avatar/<avatar_name>/manifest.json`, then the files it lists (or `assets.zip`), checking each SHA-256. The files are
   `poster.jpg`, `visemes/<V>.png` and `visemes/frame.json`.
4. A turn: `{"type":"start","mode":"hold","lang":"it","sample_rate":16000}`, binary PCM16 frames of **even length**, `{"type":"end"}`;
   or `{"type":"text","text":"..."}` for a typed turn. Binary frames outside a turn are dropped.
5. The answer: `transcript`, `reply` chunks, then `audio_start {sample_rate, segment}` (the rate is not constant: 24000 and 22050 both occur)
   and for every audio segment a `visemes` message **before** its PCM:
   `{"type":"visemes","segment":n,"text":"...","duration_ms":1840,"pcm_bytes":88320,"visemes":[{"t":0,"v":"sil","dur":120},...]}`, then
   `audio_end`, `done`. Between `transcript` and `audio_start` up to 20 seconds can pass.
6. The timeline of segment n starts when the speaker starts segment n: the sum of the `pcm_bytes` of the earlier segments of the reply, divided
   by 2 and by the rate. The schedule is kept on the audio clock, not on the arrival time. `t` and `dur` are milliseconds.
7. `{"type":"interrupt"}` (a tap while it speaks) stops the answer: flush the speaker, drop the timelines not yet reached, show `sil`. The PC
   answers `interrupt`, then `done`. A new `start` does the same and begins the next turn.
8. If the sprites cannot be shown: `{"type":"render_failed","mode":"visemes","why":"..."}`. The answer is `render_mode` with `mode: "none"`
   (voice only), or the next mode the device declared.

The viseme names are `sil PP FF TH DD kk CH SS nn RR aa E I O U`, from `voice/callbridge/viseme_gen.py`. `visemes/frame.json` has `size`
(the sprite size in pixels, 384 wide at most), and `inv`, the 2x3 matrix that maps a sprite pixel to a pixel of `poster.jpg`: the device draws
the poster, then the current sprite through that transform. A sprite is RGBA and changes only the mouth, so drawing without alpha shows the
same picture.

## How it reaches the PC

The device is never on the PC's network. The source decided on a tailnet node on the board itself (MicroLink, an MIT library that speaks the
Tailscale control protocol) with WireGuard to a subnet router as the fallback, and on the device key of TaDaMoo's server for authentication,
the same key a phone has. Neither was tried: the board had not arrived when the notes were written.

## State

| Part | State |
|---|---|
| Display (ST7701 over MIPI-DSI, backlight, GT911 touch) | bring-up written in `components/guition_display`; the notes list four faults found and fixed on the board |
| Launcher and demo apps (ESP-Brookesia) | present; a 480x480 stylesheet is used, one for 480x800 is not made |
| WebSocket client | `components/gateway_client`: auth, turn start and end, PCM send, events. Sends `auth` **without** `render_modes`, so against this gateway it gets the voice only |
| Avatar app | `components/apps/avatar`: a poster, a status light and a press-to-talk that sends a fixed sentence as a typed turn |
| Visemes | not written: no `visemes` handler, no sprite download, no PNG decoding, no schedule on the audio clock |
| Microphone, speaker, echo cancellation | not written; the audio path counts the PCM it receives and plays nothing |
| Network on the tailnet | not written |

Nothing in this repository compiles this firmware, and the CI does not. A first step is to add `render_modes` and `device` to the `auth` of
`gateway_client.c` and to parse `visemes` and `segment`; the protocol above is what the gateway sends.

## What was imported

From the snapshot `63ed73eb` of callAPIcall (`esp32-terminal/`): `main/`, `CMakeLists.txt`, `partitions.csv`, `sdkconfig.defaults`,
`sdkconfig.defaults.p4x_c5`, `dependencies.lock`, `README.md` (Italian, as it was) and the sources of `components/` (`gateway_client`,
`guition_display`, `bsp_extra` with its licence, the demo apps, and the sources of the two detector components). 102 files, about 570 KB.

Left out: `build/` (2,648 files), `managed_components/` (the downloaded ESP-IDF components, 8,722 files), the generated `sdkconfig` and
`sdkconfig.old`, the image arrays and icons of the apps (`assets/`, `images/`), the music and video player assets and the MP3 files of
`spiffs/`, the `.espdl` models of the detectors, `build.ps1` (no scripts for PowerShell here), `README_CN.md`, `esp32p4.md` (the plan, written
for a video design that this decision replaced) and `tools/esp32-sdp-probe.py` (a fake WebRTC client for that design).
`dependencies.lock` fixes the versions `idf.py` fetches into `managed_components/`. The tree is therefore a source subset: it needs the
`managed_components` fetch and the image arrays regenerated before it builds, and that has not been tried.
