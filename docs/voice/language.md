# The avatar's spoken language

The avatar follows the language the person speaks, or keeps a language the person asked for. The code is `voice/callbridge/language_follow.py`
(when to switch), `language_names.py` (the commands), `language_voice.py` (which voice), `language_session.py` (the state of one session); the
settings are the card Settings > Voice > Spoken language (`language` in `voice.json`).

## What it does

- **Follow** (the default). Whisper reports, for every utterance, the language it heard and how sure it is. The avatar changes language when
  the new one is enabled, differs from the current one, reaches the confidence threshold (default 0.80) and is not a filler. An utterance
  shorter than the short-utterance duration (default 1.5 s) must be followed by another in the same language before the avatar changes.
  One or two words never change it, and neither does one foreign word inside a sentence in the current language (whisper reports the
  language of the whole utterance). The recognizer stays on automatic detection: no model is reloaded when the language changes.
- **Locked.** "Avatar, speak French", "parla francese", "parle français", "sprich Deutsch", "habla español", "fala português" (the verbs are
  matched in Italian, English, French, German, Spanish and Portuguese; the language may be named in any of them or in its own: "français",
  "italiano", "日本語") hold the language until "back to automatic" ("torna automatico", "retour en automatique", "automatisch", "volver a
  automático", "follow my language"...). While locked, detection is ignored and the recognizer is hinted with the held language.
  A lone word "automatico" in a conversation is not a command unless the language is locked.
  The way out is heard in any language: while the language is locked, a short utterance (4 words or fewer) that the hinted recognizer did not turn
  into a language command is transcribed once more with detection and no hint, and that text is used when it is a command ("Torna automatico"
  under an English lock). Cost: one extra transcription of a short audio, only for short utterances that were not commands while locked; follow
  mode and long utterances pay nothing. The follower stays locked unless a command matched.
- **Meetings and calls.** The language is the one set (the plan, the persona, the call's language) and never follows the speech: those sessions
  have no language state, whatever whisper hears.

A command names a language and nothing else. No phrase selects an engine or a model ("use Piper", "cambia motore", "use the Italian engine" do
nothing and go to the brain as ordinary speech); two languages in one phrase ("from English to French") are ambiguous and do nothing.

## Who may change it

Only the person: their own microphone (push-to-talk and avatar sessions), their own phone (the Avatar tab's speech, its typed text and its
`language` message), and the settings page. Speech of a meeting participant, the other end of a call, and text from a document, a page or a tool
result never changes it: a session that listens to someone else does not run the follower, and an utterance with another source is passed to the
brain as it is. The PC's `/session/language` route and the page ask through the service's token, from this PC.

## The voice

The selected engine and voice stay whenever they can speak the new language. For a new language L with the selected engine E:

1. E speaks L (the engine catalogue's languages): a multilingual engine with cloned or built-in speakers (XTTS, Chatterbox, CosyVoice,
   Qwen3-TTS, Dia) keeps the same voice and identity, with the new `lang`; Kokoro uses the voice the person mapped to L, else its standard voice
   of L; Piper uses the voice mapped to L, else an installed Piper voice of L.
2. E cannot speak L, or has no voice for it: the reply is synthesised with Piper, as a routing decision for the turn: the Piper voice mapped to L,
   else an installed Piper voice of L. The selection is not rewritten, the person is told once, with the voice that was speaking ("French is not
   available in the voice if_sara, so I use Piper"), and the next language E speaks goes back to E. The existing one-engine-in-memory path of the
   synthesis does the swap; the follower never loads or unloads anything.
3. Piper is not installed or has no voice for L: the answer stays in the previous language and the person is told once, naming the Install
   button in Settings > Voice. Nothing is downloaded by itself.

The map `language.voices` holds `language -> a voice name of the selected engine`; an entry that belongs to another engine is ignored.

## The brain, the recognizer and the avatar

- The brain is told with `set_reply_language(name, code)`. The direct brain gets "Reply in French." in its system message and, once for each
  change, a line `[state] reply_language=fr` after the notes of the turn (never in a cached prefix). The harness has no field for it: the
  line goes in front of the person's message in the next turn, and again in a new conversation. The line is made from a validated code, never from
  the speech.
- The synthesis `lang` is spelled the way each engine takes it: Kokoro's codes (en->en-us, pt->pt-br, zh->cmn, in
  `engines/kokoro_lang.py`, used by the engine and its GPU helper), the ISO code for the others. The recognizer's hint follows the language; in follow mode the recognizer stays automatic. A fixed `stt_language`
  in the config turns following off (the avatar cannot follow what a forced recognizer hears) and is reported as a warning, as is an
  English-only (`.en`) recognizer model with another language enabled.
- The lip-sync takes the language of the voice: the visemes of the gateway carry `lang` of the language spoken.

## Settings

`voice.json` `language`: `follow` (true), `enabled` (`["it","en"]`, 1 to 16 codes of the languages the avatar knows), `threshold` (0.5 to 0.99, 0.8),
`short_s` (0.5 to 5, 1.5), `voices` (map). `GET` and `POST /api/voice/language`, this PC only, each value checked with a message in English and Italian.
A saved choice applies from the next session or phone connection. With `follow` off the language changes only on request, and a phone's `lang`
rules as before.

## Where the language shows

- `GET /session/language` (through the service): `{language, active, mode: follow|locked, follow, engine_fallback, warnings, languages}`;
  `POST /session/language {"language": "fr" | "auto"}` is the person's own panel (a language they enabled, or back to automatic). `GET /session/state`
  also has `language_mode`.
- Gateway: `ready` and `state` carry `language` and `language_mode`; `language {language, mode, reason, warning, engine_fallback}` is sent when
  the language or its mode changes, or a request is refused; the phone may send `language {language: "fr" | "auto"}` (`gateway.md`). With
  `follow` on, the `lang` of a `start` only gives the first language; later turns keep the followed one.
- The settings card shows the language and the mode with a "back to automatic" button.

## Tests

`voice/tests/test_language_follow.py` (the switch rules, with a clock), `test_language_command.py` (the commands in all the languages, what is not a
command, no engine or model), `test_language_session.py` (the voice order with a router that records every engine change; owner and third party;
Piper and back), `test_language_detection.py` (whisper's language through the engine, the client and the shared process, with stand-ins),
`test_language_streaming.py` (a session's loop with a stand-in STT and TTS), `test_language_gateway.py`, `test_brains_language.py`, and
`serve/tests/test_voice_language.py` (settings, bridge, page).

## Not proven

- Whisper's language probabilities on short audio and on real code-switching: the thresholds are defaults chosen to be careful, not measured.
- Real voices: Kokoro's and Piper's voices of each language, the time of the first swap to Piper, and the VRAM when the engine changes.
- Persona-level maps (the map is global to the voice settings), and the verbs of commands in Japanese, Chinese, Hindi or Hungarian (only the
  names of those languages are matched, plus "beszélj" for Hungarian).
- The settings card in a real browser at 360, 390 and 1280 pixels; it was checked by tests and by reading, not by eye.
