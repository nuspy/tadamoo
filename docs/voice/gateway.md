# The voice gateway (the phone's Avatar tab)

A WebSocket server inside the voice service (`voice/gateway.py`, one connection is `voice/callbridge/voice_gateway.py`). The Flutter app's
Avatar tab and the ESP32 terminal (`esp32.md`) talk to it for hold-to-talk or continuous voice, with the PC's own speech recognition and
synthesis and the PC's brains. The avatar is drawn on the device (Wav2Lip, or visemes sent as a timeline); the PC video is an emergency only.

## One address, one key

```
phone --(Tailscale, device key)--> TaDaMoo server  /api/voice/gateway/ws  --(loopback, service token)--> gateway
```

- The gateway listens on `127.0.0.1` on a port the service chooses (`GET /gateway`, `gateway` in `/status`, `gateway_port` in the info file).
  It refuses any other bind address. `--no-gateway` leaves it off.
- A phone never connects to it. It connects to the server it already uses, with the key it already has, to `GET /api/voice/gateway/ws`.
  The handler checks the key like any other request (a device key on the listener for phones, with phones turned on; or this PC), then
  `serve/voice_gateway_relay.py` opens the handshake to the gateway with the service token and copies bytes both ways. The server does not
  read the frames. At most 4 phones at once; the key is checked again every 5 seconds, so a key taken away ends a conversation in progress.
- `GET /api/voice/gateway` (same checks) tells the app what it needs before it connects: `available`, `reason`, `path`, `voice`, `stt`, `tts`,
  `avatar` (the PC video function is loaded), `render_modes` (all the gateway knows: `wav2lip`, `visemes`, `video`), `brain`, `persona` and
  `personas` (`[{slug, name}]`, names only: a phone may choose a personality, not read it).
- The gateway accepts a connection only with the service token: in the handshake (`Authorization: Bearer`, which the relay adds), or in the
  first message `{"type":"auth","token":...}` within 10 seconds. A wrong path is 404, a wrong bearer 401, a wrong first message `auth_failed` and a close.

## Protocol

Text frames are JSON, binary frames are 16-bit little-endian mono PCM. Phone to PC at `sample_rate` (16000), PC to phone at the `audio_start` rate.

| Phone to PC | Meaning |
|---|---|
| `auth {token?, render_modes, device}` | first message; answered by `auth_ok` then `ready` (Render modes below) |
| `start {mode: hold\|continuous, lang, sample_rate, duplex?, transcribe_only?}` then PCM, then `end` (hold) | one turn |
| `start {duplex: true}` then PCM until `stop` | full duplex: the PC answers each utterance and the person can talk over it |
| `text {text, lang}` | a typed or corrected sentence: the same turn without the STT |
| `interrupt`, or a new `start`/`text` while a reply plays | the reply stops |
| `brain {brain: direct\|harness}`, `persona {persona: slug}` | who answers; each is confirmed by the same message |
| `render_mode {mode}`, `render_failed {mode, why}` | choose among the modes offered; report one that does not work (Render modes) |
| `video_start {reason: "emergency", sdp, sdp_type, persona?, brain?}`, `video_stop` | WebRTC video of the avatar, an emergency only |

| PC to phone | Meaning |
|---|---|
| `ready {voice, missing, avatar, brain, persona, render_mode, render_modes, avatar_name}` | the connection is open |
| `state {voice, missing, avatar}` | sent when the loaded functions change (checked once a second while idle) |
| `transcript {text}`, `reply {chunk}`, `reply_reset {text}` | what was heard; the answer as it is written |
| `audio_start {sample_rate, segment}`, PCM, `audio_end` | the voice of the answer (or the media track, see below) |
| `visemes {segment, text, lang, duration_ms, pcm_bytes, visemes}` | visemes mode: the timeline of a segment, before its PCM |
| `render_mode {mode, render_modes, avatar_name, previous?, why?}` | the mode in use changed (Render modes) |
| `done`, `turn_done`, `interrupt` | the turn is over; the reply was stopped. Each `start`/`text` gets exactly one `done` |
| `video_answer {sdp}`, `video_state {state: preparing\|live, detail}`, `video_stopped` | WebRTC negotiation |
| `video_unavailable {reason: emergency_only\|no_avatar\|no_webrtc}` | no video: the phone stays in voice |
| `error {code, message}` | `auth_failed`, `not_loaded` (with `missing`), `voice_failed`, `brain_failed`, `bad_request`, `video_failed`, `avatar_failed`, `no_persona`, `render_mode_unavailable` |

The gateway starts in standby with the service. It loads nothing: a turn without the STT or TTS function loaded is answered with
`not_loaded`, and the phone is told by `state` when they are loaded. The modes `wav2lip` and `visemes` need no function loaded.

## Brains and permissions

Each connection forks the process brain (`callbridge/brains.py`): its own mode, direct or harness, over the same two brains. The text of a
turn goes to the brain with the source `remote`: an approval question is never answered by this voice, whatever the tool, and
phone calls, payments and orders found in outside text stay on the screen as in every other voice session. The phone cannot set the
permission level (Settings > Voice on the PC) and cannot read a personality's text or memories. A personality chosen on the phone
applies to the connection and is put back to the PC's default when it ends.

## Render modes

This section is what an app or a device implements. A mode is a way to draw the avatar: `wav2lip` (the device runs the model on the photo and
the voice), `visemes` (the device draws mouth sprites over the photo at the times the PC sends) and `video` (the PC makes the video: an
emergency only, it uses the GPU). The degradation order is `wav2lip`, `visemes`, `video`, voice only.

**Declare.** The first message carries what the device can draw, best first, and its kind (`[a-z0-9_-]`, up to 32; informational):

    {"type":"auth","token":"...","render_modes":["wav2lip","visemes","video"],"device":"android"}

The token may be left out when the handshake carried the key. Unknown modes are ignored; `video` is always tried last, wherever it is listed.
A device that declares nothing gets the voice only. The answer, after `auth_ok`:

    {"type":"ready","voice":true,"missing":[],"avatar":false,"brain":"direct","persona":"","render_mode":"visemes",
     "render_modes":["visemes"],"avatar_name":"ada"}

`render_modes` is what this PC serves to this device now, in the device's order: `wav2lip` and `visemes` when the personality's avatar has their
files (`avatar.md`), `video` when the avatar function is loaded. `render_mode` is the first of them, or `"none"` (voice only). `avatar_name` is
the folder to download (empty when none). `avatar` keeps its old meaning: the PC video function is loaded.

**Files.** With the device key, on the server the app already uses: `GET /api/voice/avatar/<avatar_name>/manifest.json`
(`{version, avatar, created, render_modes, files: {path: sha256}}`), then each file or `.../assets.zip` (the files and `manifest.json`), and
for `wav2lip` the model `GET /api/voice/avatar/models/wav2lip.onnx` (`Range`, `ETag` = SHA-256, 404 `{error, code: "model_not_installed"}`).
Keep a file while its SHA-256 in the manifest is unchanged.

**wav2lip.** Audio only: `audio_start {sample_rate, segment}`, PCM, `audio_end`. No timeline, no video; the device draws the lips from the audio.

**visemes.** The same messages, and before the PCM of every segment a timeline (a segment is what the speech engine returned for a sentence):

    {"type":"audio_start","sample_rate":24000,"segment":1}
    {"type":"visemes","segment":1,"text":"Ciao a tutti","lang":"it","duration_ms":1840,"pcm_bytes":88320,
     "visemes":[{"t":0,"v":"sil","dur":120},{"t":120,"v":"CH","dur":210}]}
    <binary PCM16 frames, 88320 bytes in all>
    {"type":"visemes","segment":2,...}   <binary PCM16 frames>
    {"type":"audio_end"}

`v` is one of `sil PP FF TH DD kk CH SS nn RR aa E I O U`; `t` and `dur` are milliseconds from the start of that segment. The segment starts
where the audio of the earlier segments of the reply ends (the sum of their `pcm_bytes`, divided by 2 and by `sample_rate`): schedule on the
audio clock. The `visemes` message of a segment is always followed by its PCM, and one is never sent for a segment that an interrupt cut. If
the generator fails the timeline is a single `sil`. With the new duplex engine (`duplex_motore_nuovo`, off by default) no timeline is sent.

**Switch and fail.** `{"type":"render_mode","mode":"visemes"}` chooses among the modes offered; the answer is the same message with
`render_modes` and `avatar_name`, or `error` `render_mode_unavailable`. When a mode does not work on the device:

    {"type":"render_failed","mode":"wav2lip","why":"onnx_load_failed"}
    {"type":"render_mode","mode":"visemes","previous":"wav2lip","why":"onnx_load_failed","render_modes":["visemes"],"avatar_name":"ada"}

The failed mode is left out for the rest of the connection; the answer names the next one of the device's list, or `"none"`. The gateway also
sends `render_mode` by itself when choosing a personality (`persona`) changes the avatar and with it the mode.

**Interrupt.** `{"type":"interrupt"}` or a new `start` while the reply plays: stop the speaker, drop the timelines not yet reached, show `sil`.
The PC stops the synthesis, and no timeline of a cut segment goes out; it answers `interrupt` and `done`.

**Video, an emergency.** `render_mode` is `video` only when nothing before it can be drawn and the avatar function is loaded. The device sends

    {"type":"video_start","reason":"emergency","sdp":"...","sdp_type":"offer","persona":"...","brain":"direct"}

Without `reason: "emergency"` the answer is `{"type":"video_unavailable","reason":"emergency_only"}`; with the avatar function not loaded,
`no_avatar`; without WebRTC on the PC, `no_webrtc`. Otherwise `video_answer {sdp}` comes at once, then `video_state {state: preparing|live,
detail}` while the PC prepares the persona, and `video_stopped` after `video_stop`. The app offers receive-only video (H.264 first) and audio;
when the offer has audio the voice goes on the media track and no PCM is sent (`avatar_webrtc.py`, `aiortc`). While it runs no timeline is sent.

## Limits

The gateway carries voice, text, timelines and the emergency video. A file from the phone goes through the server's own routes, and so do the
avatar's files. The same gateway serves the ESP32 (`esp32.md`, `voice/tests/fake_device.py`). Not proven: a real device of any kind.
