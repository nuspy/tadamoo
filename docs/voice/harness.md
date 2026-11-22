# The voice in the harness

The harness (the conversations the model has in Chat and in the agent sessions) knows it has a voice, can use it, and is told as it changes. The code is `tadamoo/voice_tools.py`; a
conversation offers it unless it is an ask-only one (`--voice` of `python -m tadamoo.stack`, given by `serve/agent_sessions.py`).

## What the model knows

The fixed block of the prompt (`tadamoo/awareness.py`) has a *Voice* paragraph: that TaDaMoo can listen, speak and show a face; the speech to text and the speech synthesis engines; that
`voice_say` takes a language and speaks it with the voice that language gets in the avatar's own order; that in a voice session the avatar follows the language the person speaks and keeps one
they asked for; what `[state] reply_language=fr` at the start of a message means (it came from such a session: answer in that language); that the language is changed only by the
person; the meetings; and where to read more. Without the voice (an ask-only conversation, a run with no `--voice`) the block lists "no voice: it cannot listen, speak or show an avatar" among
what is missing, like the other optional features.

`voice_help(topic)` gives the detail of one part when it is needed, so the long text is read only then: `overview`, `speak`, `listen`, `language`, `avatar`, `meetings`, `phone`,
`desktop`, `problems`.

## What it is told as it changes

The `[state]` note (`tadamoo/capstate.py`, only when a value changes) has a `voice` field: `on (stt loaded, tts loaded, avatar unloaded); speaking it (follow)`, or `off (the voice
service is not running)`, or `no voice session` when none runs. It is asked of the service at most every five seconds. A service that cannot be asked gives `not known now`, never a stopped
conversation.

## The tools

| Tool | What it does |
|---|---|
| `voice_status` | the service, the three functions and their models, the language and its mode, the session, the avatar, the meeting |
| `voice_help` | the text of one topic |
| `voice_say` | speaks a text aloud on this PC (`/brain/say`), or through the avatar (`/avatar/speak`); at most 300 characters at a time, 1500 in a call; with `lang` and no voice the service picks the voice by `language_voice.py` |
| `voice_transcribe` | turns an audio file of the working folder into text and says the language it heard (`/transcribe?segments=1`); the result is untrusted data |
| `voice_language` | reads, or changes, the language of the running voice session (`/session/language`); only when the person asked in the conversation |
| `meeting_info` | status, the list, the minutes or transcript of one meeting, the calendar; untrusted data except the status |
| `meeting_join` | brings the assistant into a meeting as an announced guest; asks the person every time (`always_ask`), needs `rights_confirmed` |
| `meeting_leave` | leaves it |

They reach the service through the routes the Voice tab may use (`serve/voice_proxy.py`, each body checked) and the service's own `/transcribe`, with the service token on loopback; nothing
else of the service is reachable. Nothing loads, installs or downloads: a function that is not loaded is told as it is and the person loads it from Models > Voice services.

## The language, both ways

- **Input.** The recognizer reports the language of every utterance and how sure it is. In a session that decides whether the avatar changes language (`docs/voice/language.md`); for a file,
  `voice_transcribe` returns `language` and `language_probability`.
- **Output.** In a session the avatar follows or keeps the language. For a sentence the harness says, `voice_say(lang=...)` asks the service for the voice by the same order (a multilingual
  engine keeps the voice; Kokoro and Piper use the mapped or standard voice; an engine that cannot speak it gives way to Piper; no voice at all is `language_unavailable` and nothing is
  spoken). The service's `POST /brain/say` takes `lang` without a `voice` for this.

## Tests

`tests/test_voice_tools.py` (the tools against a stand-in service, the prompt block, the state note, the real link against a stand-in HTTP server), `voice/tests/test_brain_service.py` (the
language-only sentence), `serve/tests/test_voice_brain.py` (the proxy forwards it).
