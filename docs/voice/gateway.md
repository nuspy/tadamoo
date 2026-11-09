# The voice gateway (the phone's Avatar tab)

A WebSocket server inside the voice service (`voice/gateway.py`, one connection is `voice/callbridge/voice_gateway.py`). The Flutter app's
Avatar tab talks to it for hold-to-talk or continuous voice, with the PC's own speech recognition and synthesis and the PC's brains, and
for the avatar's video when the avatar function is loaded.

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
  `avatar`, `render_modes`, `brain`, `persona` and `personas` (`[{slug, name}]`, names only: a phone may choose a personality, not read it).
- The gateway accepts a connection only with the service token: in the handshake (`Authorization: Bearer`, which the relay adds), or in the
  first message `{"type":"auth","token":...}` within 10 seconds. A wrong path is 404, a wrong bearer 401, a wrong first message `auth_failed` and a close.

## Protocol

Text frames are JSON, binary frames are 16-bit little-endian mono PCM. Phone to PC at `sample_rate` (16000), PC to phone at the `audio_start` rate.

| Phone to PC | Meaning |
|---|---|
| `auth` | first message; answered by `auth_ok` then `ready` |
| `start {mode: hold\|continuous, lang, sample_rate, duplex?, transcribe_only?}` then PCM, then `end` (hold) | one turn |
| `start {duplex: true}` then PCM until `stop` | full duplex: the PC answers each utterance and the person can talk over it |
| `text {text, lang}` | a typed or corrected sentence: the same turn without the STT |
| `interrupt`, or a new `start`/`text` while a reply plays | the reply stops |
| `brain {brain: direct\|harness}`, `persona {persona: slug}` | who answers; each is confirmed by the same message |
| `render_mode {mode: musetalk\|wav2lip\|visemi}` | how the phone draws the avatar |
| `video_start {sdp, sdp_type, persona?, brain?}`, `video_stop` | WebRTC video of the avatar |

| PC to phone | Meaning |
|---|---|
| `ready {voice, missing, avatar, brain, persona, render_mode, render_modes}` | the connection is open |
| `state {voice, missing, avatar}` | sent when the loaded functions change (checked once a second while idle) |
| `transcript {text}`, `reply {chunk}`, `reply_reset {text}` | what was heard; the answer as it is written |
| `audio_start {sample_rate}`, PCM, `audio_end` | the voice of the answer (or the media track, see below) |
| `done`, `turn_done`, `interrupt` | the turn is over; the reply was stopped. Each `start`/`text` gets exactly one `done` |
| `video_answer {sdp}`, `video_state {state: preparing\|live, detail}`, `video_stopped` | WebRTC negotiation |
| `video_unavailable {reason: no_avatar\|no_webrtc}` | no video: the phone stays in voice |
| `error {code, message}` | `auth_failed`, `not_loaded` (with `missing`), `voice_failed`, `brain_failed`, `bad_request`, `video_failed`, `avatar_failed`, `no_persona` |

The gateway starts in standby with the service. It loads nothing: a turn without the STT or TTS function loaded is answered with
`not_loaded`, and the phone is told by `state` when they are loaded. When the avatar function is not loaded, `video_start` is answered
`video_unavailable/no_avatar`; the phone keeps talking by voice and asks for the video again when `state.avatar` turns true.

## Brains and permissions

Each connection forks the process brain (`callbridge/brains.py`): its own mode, direct or harness, over the same two brains. The text of a
turn goes to the brain with the source `remote`: an approval question is never answered by this voice, whatever the tool, and
phone calls, payments and orders found in outside text stay on the screen as in every other voice session. The phone cannot set the
permission level (Settings > Voice on the PC) and cannot read a personality's text or memories. A personality chosen on the phone
applies to the connection and is put back to the PC's default when it ends.

## Video

The app offers receive-only video (H.264 first) and audio. The gateway negotiates at once, prepares the avatar in the background
(`video_state preparing` with the step) and sends frames from the avatar worker (`avatar_webrtc.py`, `aiortc`). When the offer has audio,
the voice goes on the media track and no PCM frames are sent; the lips are driven by the same segments. `video_stop` gives the frames back
to the PC's camera. The modes `wav2lip` and `visemi` need nothing from the PC beyond the voice (`visemi` adds a mouth-shape timeline before each segment).

## Limits

The gateway carries voice, text and video. A file from the phone goes through the server's own routes, and the avatar's pictures for rendering
on the phone (Wav2Lip, visemes) come with that phase.
