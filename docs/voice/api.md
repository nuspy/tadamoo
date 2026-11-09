# callAPIcall's routes on TaDaMoo's port

The routes other programs used on callAPIcall keep their paths and are answered by TaDaMoo's server, on its own port, through the voice
service. `serve/voice_compat.py` holds the table; `serve/handler.py` hands a request to it before anything else. Nothing is under `/v1`,
which stays the OpenAI and Anthropic interface of the loaded model, and nothing here is under `/api`, which is the page's own.

## Who may use them

| Kind | Who |
|---|---|
| `api` | a program with the server's API key (or the control secret) as `Authorization: Bearer <key>` or `x-api-key`; with no key set, a program on this PC |
| `local` | the same, and only from this PC, whatever the key |

- With a key set, a request without it is `401 unauthorized`. The voice service's own token opens nothing here.
- With no key set, a request from another PC is `403 loopback_only`, and a request whose `Host` is not `localhost`, `127.0.0.1` or
  `[::1]` is `403 forbidden`.
- Unless it sends the API key or the control secret in a header, the server's origin guard refuses first (`403 forbidden`, with `reason`) a
  request from a page of another site (`Origin`, or `Sec-Fetch-Site: cross-site` on a write) and, with no key set, a DNS name that is not
  this PC's ([server security](../server-security.md)). The same guard covers `/api/*`, `/api/voice/*`, `/api/telegram/*` and the gateway WebSocket.
- The listener for phones offers none of these routes (`403`).
- A route marked "gated like a join" is also refused with `403 programs_join_off` (texts in `error` and `message_it`) unless the person turned on `meetings.allow_programs_join` in Settings > Voice > Meetings; it is read at every request.
- An upload is read away when the request is refused, so the caller sees the answer and not a reset.

## What passes through

The path, the query, the `Content-Type` and the body go to the service; the answer comes back with its status, `Content-Type`,
`Content-Disposition` and `Cache-Control`. No other request header is passed on: `X-Call-Origin: agent`, which tells the service that the
agent asked the person itself, is dropped, so a call placed through this port counts as a program's call and follows the service's rules for those.

- A file (`/analysis/start`, `/transcribe`) is copied through in pieces and needs a `Content-Length`: `411 length_required` without one,
  `413 too_large` over the limit (2 GiB for an analysis, 512 MiB for `/transcribe`). A JSON body is at most 64 KiB (64 MiB for
  `/voices/clone`).
- A long wait (`GET /external/<id>/heard?timeout_s=T`) is waited for: the timeout is 30 s plus `T` (at most 120 s).
- A JSON or text answer is read whole and searched for the service's token, which is replaced by `[hidden]`; a binary answer streams.
- `503 voice_service_down` says why the service cannot be reached (not started, no token, not answering).
- A path in the table with another method is `405`; a path outside it is the server's usual `404`.

Errors made here are `{"error": "<text>", "code": "<code>"}`, the form callAPIcall used.

## The table

| Route | Methods | Kind | Notes |
|---|---|---|---|
| `/external/call`, `/external/<id>/say`, `/hangup` | POST | local | the calls driven by a program; the service side is the telephony module's |
| `/external/<id>/heard`, `/transcript`, `/state` | GET | local | `heard` waits |
| `/analysis/start` | POST | api | multipart `file` or JSON `{url}`; `speakers`, `analyze`, `title` |
| `/analysis/list`, `/analysis/<id>`, `/analysis/<id>/transcript` | GET | api | |
| `/analysis/<id>/speakers` | POST | api | `{names: {"Speaker 1": "Ada"}}` |
| `/analysis/<id>` | DELETE | api | |
| `/transcribe` | POST | api | multipart audio; `?lang=`, `?segments=1`, `?words=1` |
| `/tts/test` | POST | api | `{text, voice?, lang?, engine?}` gives `audio_b64` (WAV) |
| `/tts/models`, `/tts/voices?model=` | GET | api | |
| `/voices/cloned` | GET | api | |
| `/voices/clone`, `/voices/cloned/<name>` | POST, DELETE | local | samples in `<models folder>/tts/voices` |
| `/models/status`, `/models/available` | GET | api | |
| `/session/state`, `/session/language` | GET | local | |
| `/session/start`, `/stop`, `/new`, `/language` | POST | local | |
| `/meetbot/join` | POST | local | the meeting bot; a join needs `rights_confirmed: true` and the announcement and consent rule apply. Refused with `403 programs_join_off` unless `meetings.allow_programs_join` is on (`meetings.md`) |
| `/meetbot/leave` | POST | local | |
| `/meetbot/status`, `/meetings/list`, `/meetings/<id>`, `/meetings/<id>/transcript` | GET | local | |
| `/meetings/<id>` | DELETE | local | deletes the transcript and the minutes |
| `/calendar/status`, `/calendar/events` | GET | local | inert without the person's own OAuth values |
| `/calendar/<google\|microsoft>/connect`, `/disconnect` | POST | local | |
| `/calendar/events/<id>/autojoin` | POST | local | `{enabled}` sets Participate of that event's plan item; gated like a join |
| `/meetings/plan` | GET | local | the plan: `{items, platforms, autojoin, lead_s}` |
| `/meetings/plan` | POST | local | add `{url, start, title?, duration_min?, participate?, role?, persona?, camera?, consent_mode?, notes?}` -> `201 {item}`; gated like a join |
| `/meetings/plan/<id>` | PUT, PATCH, DELETE | local | change the named fields, or delete a manual item; gated like a join |
| `/meetings/plan/<id>/join-now` | POST | local | `{rights_confirmed: true}` -> `202`; gated like a join |
| `/copilot/state`, `/copilot/toggle` | GET, POST | local | the copilot window |

## 501 not_available_yet

These answer `501` with `code: not_available_yet` and the reason, and reach no service:

| Route | Why |
|---|---|
| `/tts/model`, `/tts/voice` | the voice and the engine are chosen in Settings > Voice |
| `/models/load`, `/models/unload` | functions are loaded and unloaded from the Voice functions dialog in the Loader |
| `/tts/stream`, `/stt/stream` | WebSocket streaming is not offered on this port yet |
| `/personas`, `/personas/...` | the personalities are TaDaMoo's own (`/api/personas`); the callAPIcall form comes with the avatar phase |
| `/avatar/...` | the animated avatar comes with its own phase |

## Analysis of a recording

`/analysis/*` runs inside the voice service (`voice/callbridge/api_analysis.py`), one job at a time.

- The recording is cut into blocks by ffmpeg and each block goes through the service's own STT (the installed Whisper model); the models
  are only read from the models folder, never fetched. `409` with `code: model_not_installed`, the `model` and the plain reason when the
  Whisper model is not installed. A second job while one runs is `409` with `code: busy`.
- The speakers are told apart by the diarization models when they are installed. Without them (or without `sherpa-onnx`) the transcript has
  no speaker labels and the analysis still runs.
- The summary, the highlights and the tasks come from the direct brain (`ask_stream`: the loaded model, no tools, no personality). A
  transcript never reaches an agent. `analyze=0` stops after the transcript and no model reads it.
- Results are in `<user data>/voice/analyses/<id>/` (`source.json`, `transcript.json`, `analysis.json`, `summary.md`).
- The harness's `transcribe_attachment` posts a file with `analyze=0`, polls `/analysis/<id>` and reads `/analysis/<id>/transcript`: that
  contract is unchanged. Its client reads every `409` as busy, so a missing model shows there as a busy service.

## Pointing the harness at this port

`tadamoo/integrations/call_api_client.py` reads `CALLAPICALL_URL` (default `http://127.0.0.1:8770`) and `CALLAPICALL_TOKEN`. To use these
routes, set the URL to TaDaMoo's address (`http://127.0.0.1:<port>`) and the token to the server's API key (empty when none is set and the
program runs on this PC). The default is not changed here.

## Tests

`serve/tests/test_voice_compat.py` (the table, who may use it, files, waits, 501, the token, with a stand-in service),
`serve/tests/test_voice_compat_programs_join.py` (the gate), `serve/tests/test_external_surface.py` (the paths outside `/v1`), `voice/tests/test_analysis_service.py` and `test_speech_routes.py`
(the service side with stand-ins for the STT, the diarizer and the model).
