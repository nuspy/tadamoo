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
  this PC's ([server security](../server-security.md)). The same guard covers `/api/*`, `/api/voice/*`, `/api/telegram/*` and the gateway WebSocket (its messages, `render_modes` among them, are in [gateway](gateway.md)).
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
| `/session/state`, `/session/language` | GET | local | the language, its mode and the languages enabled: `docs/voice/language.md` |
| `/session/start`, `/stop`, `/new`, `/language` | POST | local | `/language` takes a language the person enabled, or `auto` |
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
| `/personas` | GET, POST | local | answered by this server from TaDaMoo's own personalities, not by the service (see "Personalities") |
| `/personas/<name>` | GET, PUT, PATCH, DELETE | local | the same; DELETE needs `?confirm=true` |

## 501 not_available_yet

These answer `501` with `code: not_available_yet` and the reason, and reach no service:

| Route | Why |
|---|---|
| `/tts/model`, `/tts/voice` | the voice and the engine are chosen in Settings > Voice |
| `/models/load`, `/models/unload` | loading a function plans the VRAM and may unload and reload the language model, so it needs the person's confirmation in the Voice functions dialog in the Loader (`loading.md`); callAPIcall's one-call `{stt_model, tts}` has no safe equivalent |
| `/tts/stream`, `/stt/stream` | the service has no streaming speech route behind its token; only the old Handy server (`handy_compat_enabled`, off by default, port 8720, no token) speaks these WebSockets, and relaying it would put an unauthenticated socket behind the key |
| `/personas/<name>/...` | the memory, clips, poster and avatar files of a personality: the memory is the person's, the avatar files come with the avatar phase |
| `/avatar/...` | the animated avatar comes with its own phase |

## Personalities

`/personas` keeps callAPIcall's paths and is answered here by `serve/personas_compat.py` over the same store as Settings > Personalities
(`docs/personas.md`); the voice service is not asked. For this PC only, with the key when one is set, because a personality's text steers the agent.

| callAPIcall | TaDaMoo |
|---|---|
| `name` in an answer | the slug, which the other routes take; `display_name` is the name |
| `name` in a body | the display name (the slug is made from it and then never changes) |
| `voice {engine, voice}`, `personality`, `system_prompt` | the same fields |
| `avatar`, `language`, `knowledge` (archive ids), `learn` | TaDaMoo's own fields, read and written with the same names |
| `knowledge_project` | no equivalent: refused when not empty, ignored when empty |
| `clips`, `clip_count`, `has_personality_docs`, `knowledge_chunks` | not in an answer |

- `POST` answers `201 {status, name, display_name, persona}`, `409 exists` for a name already used. `PUT` and `PATCH` change the fields sent.
  `GET /personas/<name>` is new. A path takes the slug or the name (spaces or underscores, any case).
- Memory is never in an answer and cannot be written: the short profile, the notes and what was learned. `profile` and `memory` in a body are `400`.
- `DELETE` removes the personality and its memory, so it needs `?confirm=true`; without it `400 confirmation_needed` says how many notes would go.
  callAPIcall's `409` for the active avatar is not offered.
- A body is at most 128 KiB; a field the store refuses is `400` with its reason.

## Calls from the phone app

`POST /api/voice/phone/call {number, greeting?, lang?}` is not a callAPIcall route; it is how the person's phone starts a call as the person.
The origin of a call is decided here from the channel, never from what the client says: the request must come through the **listener for
phones** with a key (a device key or the server's key, phones turned on), and the person must have turned on **Let my phone place calls through
this PC** (`telephony.phone_calls`, off by default). Then the server sends `POST /call` to the service, the route for a call by hand (origin
`user`: no approval, the first sentence still says the caller is an AI, an emergency number is refused). `X-Call-Origin` is neither read nor
passed on, and the body is cut to `number`, `greeting` and `lang`: the phone cannot choose the line, the voice or its own origin.

| Answer | When |
|---|---|
| `200 {status: "calling", transport}` | the call started |
| `403 phone_only` | not through the listener for phones with a key: a program on this PC, with any key or header, is refused and uses `/external/call`, where it is a `program` |
| `403 phone_calls_off` | the switch is off |
| `400 bad_request` | another field, or a number over 40 characters or with control characters |
| `409`, `403`, `502`, `500` | the service's own answer for the call (busy, emergency number, no transport, ...) |
| `503 voice_service_down` | the service cannot be reached |

The risk is the person's to take: with the switch on, whoever holds a phone key can place calls on this PC's line to any number, premium-rate
ones included (the deny-list of premium prefixes applies to the agent and to programs, not to a call by hand). Taking the key away in Settings >
Phones ends it. The Flutter app needs no change for the origin (no header is sent); it only has to post this route with its key.

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

`serve/tests/test_voice_compat.py` (the table, who may use it, files, waits, personalities, 501, the token, with a stand-in service),
`serve/tests/test_voice_compat_programs_join.py` (the gate), `serve/tests/test_voice_phone_call.py` (the phone's call and its origin), `serve/tests/test_external_surface.py` (the paths outside `/v1`), `voice/tests/test_analysis_service.py` and `test_speech_routes.py`
(the service side with stand-ins for the STT, the diarizer and the model).
