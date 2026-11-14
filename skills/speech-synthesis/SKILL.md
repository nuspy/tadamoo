---
name: speech-synthesis
description: Make a voice recording of a text that is good enough to publish: language normaliser (numbers, dates, abbreviations), a pronunciation lexicon with an optimizer, takes chosen on measurements (read-back, speaker, pace, pitch), no spurious pauses, joins without clicks, one loudness. Hungarian is the worked case. Use when a task needs spoken audio of a text, for a video, a podcast or a call.
tags: speech, tts, voice, xtts, hungarian, normaliser, lexicon, read-back, loudness
requires: python
---

# Speech synthesis

The code is `tadamoo/speech/`; docs for a programmer in `docs/speech/`. TTS engines are run locally: XTTS v2 (the voice service's and a copy under our control), Piper, MMS. **No cloud TTS is used
unless the person allows a named engine and a key** (see [references/engines.md](references/engines.md)).

## When to use it

A text has to become a recording that a person will hear on a published video. Not for live conversation (the voice service does that) and not for editing the picture (`video-editing`).

## The pipeline

1. **Normalise** the text for the voice (`textnorm.normalise(text, lang, pronunciations)`): a rule table; numbers, dates, clock times, percentages and decimals spelled in the language (Hungarian with
   vowel harmony: tizenkettedikén), abbreviations, symbols, URLs and emoji dropped, no full stop that the engine reads as the word "dot".
2. **Respell** the words the engine reads wrong (`respell.Lexicon.for_voice(engine, voice)`, `respell.respell`): a plain JSON lexicon per voice that a person can edit, found by `speech.optimize` (tries
   systematic respellings of a hard word in a carrier sentence, scores them with several speech-to-text models, keeps the best that beats the plain word). Only the speech text is respelled; the
   screen text and the captions keep the correct spelling and the read-back compares with the correct spelling.
3. **Make takes** (`clausevoice.speak`): XTTS in its own process (kept loaded for the whole job) with a fixed seed and a low temperature; a whole sentence per piece (a short fragment is read slowly, with pauses); the seeds of a piece are made together in one batched pass.
4. **Choose** by measurement: the Whisper words and a CTC model's letters read back, no pause over 150 ms inside a phrase, the speaker close to the voice's profile, the pace in range, a steady
   pitch, the last word whole. A piece with no right take fails the run loudly; nothing is shipped silently.
5. **Join** (`audiojoin.assemble`): cut at both ends, 15 ms fades, comma pauses 250 ms (the engine's own are brought to that), sentence ends 500 ms, filled with the pieces' own noise floor;
   then one loudness (`audiofx.loudnorm`, -16 LUFS by a linear gain, the timbre untouched).
6. **Check** (`audiojoin.silences`, `pause_audit`, `click_report`, `splices`, `dead_gaps`; `voiceprint`; `asr`): objective numbers on any recording, including a competitor's.

Parameters and limits: [references/parameters.md](references/parameters.md). What was found for Hungarian: [references/hungarian.md](references/hungarian.md).

## Speed: what is installed and how to use it

XTTS runs in its own Python (`tts-venvs/xtts`): PyTorch 2.11 with CUDA 12.8, transformers 4.57, the same torch as LTX Studio's inference environment. What it uses: PyTorch's SDPA attention, the model's
key-value cache, the model and the speakers' conditioning latents kept loaded, the seeds of a sentence in one batched pass, takes kept by text and seeds (`--cache FOLDER`), and the longest time of a run
from the length of its audio (`tadamoo/jobtime.py`).

Optional, off until measured: `--precision tf32|fp16|bf16`, `--compile on`, `--rating-device cuda`. For `--compile on` Triton must be in the XTTS Python; on Windows that is the `triton-windows` package, the
one LTX Studio installs. Check and install it only when it is missing: `python -m tadamoo.media.components check speech-synthesis`, then `... install speech-synthesis`. Without Triton `--compile on` does
nothing and says so. The time and the quality of each option are in `tests/QUEUE_daytime.md` until they are measured; do not turn one on as a default before that.

## Heavy-swap behaviour

Light: XTTS about 3 GB of VRAM, the CTC read-back on the CPU, Whisper through the voice service. It coexists with the loaded language model; it does **not** use a heavy lease. A large Whisper or a hub ASR
should run on the CPU while someone else's job holds the loaded model.

## Scripts

`scripts/speak.py TEXTFILE OUT.wav [--speaker "Gitta Nikolina"] [--speed 1.14] [--seeds 4] [--batch 12] [--precision fp32|tf32|fp16|bf16] [--compile on] [--cache FOLDER] [--rating-device cpu|cuda]` makes one recording through the pipeline; `scripts/audit.py WAV [--text TEXTFILE]` prints the pauses,
clicks, splices, loudness and pace of any recording.
