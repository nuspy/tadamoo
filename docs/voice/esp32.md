# The ESP32 terminal

A third terminal beside the PC and the phone: a board with a screen, a microphone and a speaker that shows the avatar and carries the voice. It
runs only the client. Speech recognition, the brain, speech synthesis and the mouth-shape timeline stay on the PC. Nothing here has been built
for the board or run on it: `esp32/terminal/` was imported from callAPIcall and then given the gateway protocol and a face. What follows says
what exists in it and what does not.

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
   `{"type":"auth","render_modes":["visemes"],"device":"esp32"}`. A device that declares no mode gets the voice only. The firmware declares
   `visemes` only when its renderer passed its own check at start, and an empty list otherwise; it never declares `wav2lip` or `video`.
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
| WebSocket client | `components/gateway_client`: `auth` with `render_modes` and `device`, turn start and end, PCM send, `interrupt`, `render_failed`. Text frames sent in pieces are put together (32 KB at most, a larger message is dropped whole); unknown message types are ignored |
| Avatar app | `components/apps/avatar`: the stage, a status light, and a button that sends a fixed sentence as a typed turn, or stops the answer while it speaks. Closing the app closes the connection |
| Visemes | `AvatarFace`: a drawn face whose mouth follows the `visemes` timeline (opening and width per viseme, 60 ms cross-fade), scheduled on the audio clock; `components/avatar_core` holds the parser and the clock, tested on a PC in `esp32/terminal/test_host/` |
| Photo and sprites | not written: the avatar folder is not downloaded, there is no PNG decoding; the face is the same for everyone |
| Audio clock | the position of the PCM being played. With no speaker it is a virtual clock that runs at the audio rate while received audio is waiting and stops when there is none; `AvatarFace::played()` takes the codec's position when playback exists |
| Microphone, speaker, echo cancellation | not written; the audio path counts the PCM it receives and plays nothing |
| Network on the tailnet | not written |

Nothing in this repository compiles this firmware, and the CI does not: the new code was checked against stand-in headers (syntax only) and its
pure parts (`viseme_timeline.c`, `ws_reasm.c`) run in `esp32/terminal/test_host/test_avatar_core.c` with clang (112 checks, also under the
address and undefined-behaviour sanitisers). Not proven: that it builds with ESP-IDF 5.5, the fields of `esp_websocket_event_data_t` it reads
(`payload_len`, `payload_offset`, `fin`), the look of the face on the 480x800 panel (stage about 456 by 335 pixels), and any run on a board.

Behaviour worth knowing. The timeline of a segment is placed where the PCM received so far ends, so a timeline that was lost or dropped does not
shift the next one. A mode the PC picks that the board did not declare, or a `visemes` with no working renderer, is answered once with
`render_failed` and the screen says "Solo voce". `render_modes` (declare again on an open connection) is not used: the board reconnects when the
app is reopened. At most 8 timelines wait ahead of the playback and 512 items are kept per segment; more are dropped and logged.

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
