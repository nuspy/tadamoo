# Speech: `tadamoo/speech`

The speech side of the harness, reusable by any skill that makes a voice: a production uses it, so can a podcast, an audio article or a phone call.

| File | What |
|---|---|
| [text.md](text.md) | the text made fit for a voice (the rule table, numbers and dates, abbreviations), the read-back, what was found for Hungarian |
| [voice.md](voice.md) | the clause voice: takes chosen on measurements, joins without clicks, the pause and click audit, the respelling lexicon and its optimizer |

Modules: `textnorm` (normaliser), `respell` and `optimize` (lexicon), `asr` (speech-to-text models for scoring), `voicecheck` (read-back), `voiceprint` (speaker, pace, pitch),
`audiofx` and `audiojoin` (stretch, level, loudness, joins, checks), `clausevoice` and `xtts_run` (the voice made here), `service` (the voice service's route). Tests: `tests/test_speech_*.py`.
The skill that describes it for a person and for the agent is `skills/speech-synthesis` (installed in `E:\AI\skills`).
