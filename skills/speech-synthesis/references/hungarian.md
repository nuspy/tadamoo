# What was found for Hungarian

- The voice service ignores `seed`, `temperature` and `speed`; two readings of a text differ in pace, intonation and pauses, and a reading may stop in the middle of a name ("Orbán, Viktor") or read a
  word wrong. A reading is measured, never trusted.
- A short fragment read alone is slow (3.3 syllables a second) with pauses after names; a whole sentence is read in one breath. Pieces are as long as the engine reads well.
- Digital-zero gaps between sentences joined by hand sound like a gate; pauses are filled with the pieces' own noise floor (XTTS's own silence is about -90 dBFS, far under any room).
- Whisper medium writes "napu tagság" for a "NATO-tagság" that is said right; a CTC model writes the letters it hears but writes English for the numbers a voice says in Hungarian: judge the two read-backs together.
- A voice that drops the last syllable of a sentence ("elnöke" read as "elnök") is the commonest fault: the last written word must be read back whole.
- A transcript writes "november 12-én" as "november", "12", "-én" and "4,5%-kal" as "4", ",5", "%", "-kal": group the pieces of a number before spelling them.
- The Hungarian editorial wording lists (framing phrases, accusation words) are in `tadamoo/research/editorial.py`.
- Hungarian-capable speech-to-text for scoring: Whisper medium (voice service), Whisper large-v3, `GaborMadarasz/wav2vec2-large-mms-1b-hungarian`, `jonatasgrosman/wav2vec2-large-xlsr-53-hungarian`
  (kept under `E:\Modelli\basalt\asr-hu\`, run on the CPU when the GPU belongs to someone else's loaded model).
- Specialist Hungarian TTS found on the hub, not yet evaluated: Maxdorger29/f5-tts-hungarian (cc-by-nc-4.0), legekka/diana-hungarian-tts-vits (cc-by-4.0, one speaker), Cseti/VibeVoice 7B LoRA.
