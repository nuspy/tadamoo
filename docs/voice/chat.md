# Voice: talking with the Chat and the Code tab

The voice has two brains behind one interface (`voice/callbridge/brains.py`). A session asks `respond_stream(text, source)` (the gateway may add `context_note`, read by the direct brain for that turn only: `interruptions.md`) and gets text to
speak; `ask_stream(text)` gives the full answer, for what is read on a screen (analyses, Telegram, the copilot). Every voice session, push-to-talk
or call, talks to a `ModeBrain` that holds the two brains and a mode. Nothing here imports `serve` or `tadamoo`: the brains speak to TaDaMoo's
HTTP API on loopback (`voice/callbridge/tadamoo_api.py`).

| Brain | Talks to | Tools | Spoken |
|---|---|---|---|
| Direct | `POST /v1/chat/completions` (the loaded model) | none | the model's words, with code removed |
| Harness | `/api/agent/sessions` (an agent conversation, like the Chat tab) | the agent's, with its permissions and Rule of Two | one sentence about what was done |

## Direct

The system message is the voice base prompt (`direct_system_prompt`), the personality block, a reply-language line, then the notes and archive
passages TaDaMoo recalls for the request. Both come from `POST /api/personas/<slug>/compose {goal}`: the server builds the block, recalls the
notes and fences the passages as data, so a document cannot give orders. The rolling context keeps the last `direct_history_turns` turns. Only
`content` is used: reasoning pieces are never read. A server that does not answer, or refuses, is one spoken line (`The assistant refused the request.`)
and nothing is added to the context. `ask_stream` is the model alone: no personality, no history.

## Harness

The brain makes one conversation (`POST /api/agent/sessions` with the personality and the folder of `voice.json`, and no level), sends the
words (`/send`), and reads `/events` until the turn ends. A changed personality is sent to the same conversation (`/persona`); a new session
(`reset_session`) makes a new one. Its title is Voice, so it is in the Chat's list.

**The level.** The voice never asks for a permission level: the server gives every request that carries the service's token the level of the
`voice` channel ([permission levels](../permission-levels.md)). The conversation the voice made starts at it and takes it again at each turn and
when it changes; a voice turn sent into another conversation lowers it to that level when it is higher. A request of the voice that names a
higher level is refused (`403`, `level_above_channel`), and the token does not open `/mode`.

**What is spoken.** Never code, commands, tool output or reasoning. The words come from the events, as counts (`spoken.Facts`):
files written or edited, test runs (a command that starts a test runner) and whether they failed, other commands and failures. The sentence is
`Done: 2 files changed, tests passed.`, `Finished with problems: 1 file changed, tests failed.`, in English or Italian (the session's language).
When nothing was done and the agent only answered, its answer made speakable is spoken: fenced and inline code, indented and command lines,
markup and `<think>` are removed, two sentences at most; an answer that is only code sends the person to the screen. The full text, the
sentence spoken and the personality that answered are in `last` of `GET /brain/state`, which the Voice tab shows.

**Approvals.** The same questions as in the Chat, with one rule for the voice: only a file change (`write_file`, `edit_file`) is asked by voice
("The agent wants to change the file app.py. Say yes or no."), only when the words come from the person's own microphone (`source == "mic"`:
push-to-talk sessions; a call or the phone app is `remote`), and the next words are read as yes or no (`spoken.parse_yes_no`, Italian and
English, short utterances only). Everything else is left on the screen with the line "An approval is waiting on the screen": phone calls,
payments, any command, orders found in outside text, the Rule of Two, a hook of the person, plans, codes and the agent's own question cards. Saying yes to
those sends nothing. A "yes" for an approval answered meanwhile on the screen is treated as a new message.

## Choosing the brain

- The Voice tab and `voice.json` (`brain.mode`) set the brain a session starts with. A session has its own copy (`ModeBrain.fork`), so a change
  made by voice lasts for that session only.
- Voice commands, as the whole utterance: "switch to the agent", "use the harness", "passa all'agente", "usa l'agente", "parla con l'agente";
  "switch to direct", "go direct", "passa al diretto", "torna alla conversazione diretta". The session answers in its language.
- Hotkeys (from the config, `push_to_talk_hotkey*`): Shift+F1 and Shift+F2 the agent with speakers and with headphones, Ctrl+Shift+F1 and F2 the
  agent with the avatar, Ctrl+Shift+F4 and F5 direct (with the avatar when there is one). They are registered only when `brain.hotkeys` is on.
- Analyses, the copilot and anything that reads another person's words use the direct brain's `ask_stream`: a transcript never reaches an agent with tools.

## voice.json and the token

```json
{"brain": {"mode": "direct", "persona": "maria-rossi", "workspace": "D:\\Work\\proj",
           "base_url": "http://127.0.0.1:7242", "hotkeys": false}}
```

The level is not in `voice.json`: the Voice tab and Settings > Voice show the Chat's radio buttons and words, read and write the server's level
of the voice (`/api/permission-level`), and say the levels of the Chat and Telegram next to it. A `brain.permission` found in the file is handed
to the server once, when the server has none for the voice, and removed. The server writes `base_url` at every start. The service sends its own token (`voice-service.token`) to the server, which accepts it on loopback for
these routes and no others (`serve/voice_service.py`, `SERVICE_ROUTES`): `/v1/chat/completions`, `/api/personas/<slug>/compose`, `GET /api/personas`,
and the agent routes for a session (create, events, send, answer, persona). With an API key or a control secret set, the token still opens only those.

## Routes

Service (token): `GET /brain/state` (mode, personality, what waits for an approval, the last exchanges, the session),
`POST /brain/say {text}` (a test sentence with the loaded TTS; 409 `tts_not_loaded`), and `POST /session/start {mode, brain}`, which needs the
STT and the TTS loaded (409 `not_loaded`) and says why when push-to-talk cannot be made here (503 `unavailable`).
Server (this PC only): `GET` and `POST /api/voice/brain`, and `POST /api/voice/service/call {method, path, body}`, which forwards only
`GET /status /functions /brain/state /session/state /diag/brain` and `POST /session/start /session/stop /session/new /brain/say`, with the bodies checked.
The browser never holds the token or the port.

## What is proven and what is not

Tests use stand-ins: a fake TaDaMoo API (`voice/tests/fakes.py`), a loopback stand-in for the client, and the service in-process. They cover the prompt,
the events to the sentence, the approvals and the no-voice rule, the switch in both languages, the proxy list and the token. Not proven without
hardware: a real microphone and speakers through a push-to-talk session, the global hotkeys, the TTS of a test sentence, a real agent turn.
