# Parameters (`tadamoo.speech.clausevoice.Params`, and the newscast settings that feed them)

| Parameter | Default | Meaning |
|---|---|---|
| `speaker` | Gitta Nikolina | one of XTTS v2's 58 built-in speakers |
| `lang` | hu | the language of the text (the normaliser, the editorial rules and the engine) |
| `temperature`, `top_p`, `top_k`, `repetition_penalty` | 0.35, 0.85, 50, 5.0 | sampling; the engine's own 0.75 gives wider intonation and random pauses |
| `speed` | 1.14 | the engine's speed setting (latents shortened, pitch kept); at the low temperature the engine reads about 15% slower than its default. Faster than about 1.17 costs clarity |
| `seeds`, `rounds`, `base_seed` | 4, 9, 1000 | takes of each piece in a round, rounds before giving up (a piece with a right take is done, only the others are read again), the first seed; the same text and settings give the same audio |
| `xtts_batch` | 12 | takes of one piece made in one autoregressive pass (a round's seeds); a pass that runs out of memory is made again in halves |
| `xtts_precision` | fp32 | `fp32`, `tf32`, `fp16` or `bf16`: the last two are autocast over the autoregressive pass only, the vocoder stays fp32 |
| `xtts_compile` | off | `on`: torch.compile of the autoregressive pass (needs Triton in the XTTS environment); a compile that fails is given up |
| `rate_threads` | 4 | takes measured at the same time |
| `xtts_cache` | empty | a folder where the takes of a sentence are kept by text, seeds and settings and found again by any production |
| `comma_ms`, `sentence_ms`, `colon_ms` | 250, 500, 320 | pauses; the engine's own pause at a comma is brought to `comma_target_ms` |
| `defect_s` | 0.15 | a silence longer than this inside a phrase, away from punctuation, rejects the take |
| `similarity_min`, `letters_min`, `combined_min` | 0.80, 0.88, 0.90 | the Whisper read-back (words), the CTC read-back (letters), both together |
| `clause_pace_min/max`, `pace_centre` | 4.7, 6.2, 5.4 | syllables a second of a piece; the natural pace of the engine is about 5.3 |
| `max_clause_chars` | 150 | a sentence longer than this is cut at the comma nearest its middle |
| `loudness_lufs` | -16 | the integrated loudness of the result |

Settings of the newscast: `voice_mode` (`clauses` or `service`), `voice_speed`, `voice_speed_firm`, `voice_temperature`, `voice_seeds`, `voice_rounds`, `comma_ms`, `sentence_ms`, `xtts_python`, `xtts_model`, `xtts_batch`, `xtts_precision`, `xtts_compile`, `xtts_cache`, `rating_device` (`auto`, `cpu` or `cuda`: the CTC read-back and the speaker embedder; auto is the GPU when it has 4 GB free), `read_back` (`auto`, `local` or `server`: the Whisper of the read-back), `respell`,
`respell_rules`, `lexicon_file`, `speech_level_db`, `loudness_lufs`.

Output: 24 kHz mono 16-bit WAV (no lossy re-encode); an MP3 for a phone is made with ffmpeg from it.
