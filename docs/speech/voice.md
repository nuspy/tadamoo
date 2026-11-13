# The voice, in Hungarian: respelling, takes chosen on measurements, joins without clicks

What it takes for a synthesised voice to be good enough to put on a news video, and the parts that do it. The speech text is first normalised
([text.md](text.md)); this page is what happens after.

## What was found

- The voice service's `/tts/test` takes **no seed and no sampling settings**: `speed` and `temperature` in the request change nothing. Two readings of one text differ in pace,
  intonation and pauses, a reading may stop in the middle of a name ("Orbán, Viktor") and some read a word wrong. So a reading cannot be trusted; it has to be measured.
- A long text read in one call is joined inside the engine, and a text read sentence by sentence and joined by hand had **no fade and a digital-zero gap** between the sentences
  (the speech is cut off at its own level into a silence that has no room noise at all): a listener hears a cut and a dead gap.
- A **short fragment read alone is read slowly and with pauses** ("a Fidesz és a Tisza elnöke" came out at 3.3 syllables a second with pauses after the names). The engine reads
  a whole sentence in one breath, so the pieces are as long as the engine reads well (`max_clause_chars`, 150).
- Whisper medium (the voice service's) writes "napu tagság" for a "NATO-tagság" that is said right; a CTC model has no language model and writes the letters it hears, but
  writes English words for the numbers a voice says in Hungarian. The two read-backs are therefore judged together, not each by a strict limit of its own.

## The parts

| Part | File | What it does |
|---|---|---|
| Respelling | `respell.py`, `data/lexicon/hu/*.json` | Rules and a lexicon (word to respelling, per voice, plain JSON a person can edit); only the text sent to the voice is respelled, never the screen text. |
| Optimizer | `optimize.py`, `asr.py` | For each hard word tries the systematic respellings in a carrier sentence, reads each back with several speech-to-text models, keeps the best that beats the plain word by a margin. |
| Local XTTS | `xtts_run.py`, `clausevoice.LocalXtts` | XTTS v2 in its own Python with a fixed seed and the sampling settings under control, the model kept loaded, the seeds of a piece made in one batch ([Time](#time)); takes already made are not made again. |
| Judging | `clausevoice.Rater`, `voiceprint.py` | A take is rejected for a bad read-back (Whisper words and CTC letters), a pause inside a phrase, another speaker, a pace outside the range, a wandering pitch. |
| Joining | `audiojoin.py` | Each piece is cut at both ends, de-clicked, faded 15 ms, and the pause for its punctuation is filled with the room noise of the pieces. |
| Loudness | `audiofx.py` | One linear gain to -16 LUFS (two passes), so the timbre is not touched. |
| Checks | `audiojoin.py` | `silences`, `pause_audit`, `click_report`, `splices`, `dead_gaps`: the objective numbers on any recording. |

## Choosing a take

Each piece is read in rounds of a few seeds (`seeds`, 4; up to `rounds`, 9) at a low temperature (0.35; the engine's own 0.75 samples wider intonation and pauses). The measures of every take are kept by
the hash of its audio, so a take is measured once and judged again whenever a limit changes. A take is *right* when all of these hold:

- the Whisper read-back matches at least 0.70 and the CTC letters at least 0.84, and the two together at least 0.87; no words after the last written word. A piece that holds a
  number the normaliser spelled out (kétezer-huszonnyolc) has no CTC judgement: that model writes such a number in English, so the letters are taken as the Whisper's score;
- no silence longer than 150 ms where the text has no punctuation (the speech-to-text's word times say where a silence falls), and none longer than 450 ms at a comma;
- the speaker embedding is within the voice's own spread of its profile (`voiceprint.build`, the mean of the voice's readings);
- the pace of the piece is 4.8 to 5.8 syllables a second, the engine's natural pace being about 5.2; no time-stretch is used;
- the pitch does not wander more than 1.3 times the voice's own.

Among the right takes the best combined score is kept. When a piece has no right take after `rounds` rounds the whole text **fails loudly** (`VoiceFailed`, with every take's reasons):
nothing is shipped that did not pass.

## Time

Measured on one newscast (RTX 5090, power limit not recorded): XTTS makes a take in 2.3 s for 6 s of speech, 0.36 s for each second, faster than real time; 60 takes took 136 s.
The newscast took far longer than real time for these reasons, and the code changed as follows:

- Twelve takes of every piece were made and rated before any was kept, three rounds when none was right: 3 to 4 seconds of speech time for each second of speech before any rating.
  Now a round is 4 takes and only the pieces with no right take go on to the next round (the most takes of a piece is the same, 36).
- The XTTS process was started for every block and every round, and loaded the model each time (about 20 s). Now one process is started for the whole voice phase and ended at its
  end, before any heavy job takes the GPU.
- A dead CUDA context in the speech-to-text worker answers HTTP 500 for ever, and the production waited for it for hours (80 minutes, then 30 more). Now the worker is reloaded once
  (`POST /components/stt/reload` of the voice service) and a second failure stops the production with the service's own message.
- A piece with a spelled number could never pass: the CTC model's letters match 0.70 to 0.76 for it, the limit is 0.88 (36 takes spent, then the production stopped).

### Making the takes of a round faster

- **Batched seeds.** The takes of a piece in a round share a text and settings, so one autoregressive pass makes them all (`num_return_sequences`, `xtts_batch`, 12 at most): a decoding step
  is bound by the launch of its small kernels, so a step for four takes costs about what a step for one does. The batch is seeded once, with the seed of its first take; each take is then cut at
  its own stop token and turned into audio by itself, as a single take is. A batch that runs out of memory is made again in two halves. A piece with a take missing from the cache is made again whole.
  A take made in a batch is not the take its seed made alone (the draws are shared); a right take is judged by measurement, so this changes nothing in what is right.
- **Precision.** `xtts_precision` `fp16` or `bf16` runs the autoregressive pass under autocast (the latents and the vocoder stay fp32). The default stays `fp32` until takes made in half precision
  have been heard and their share of right takes is the same.
- **No graph compiler.** `torch.compile` and CUDA graphs need Triton, which the XTTS environment does not have on Windows, so they are not used. The inference mode and the cached conditioning
  latents already were.

## Joining

Pieces are never overlapped. Each is cut to its sound (the silence and the noise at both ends removed, 30 ms kept), its DC offset removed and faded in and out over 15 ms. After a
piece ending in a comma comes 250 ms, after a colon 320 ms, after a sentence end 500 ms (`comma_ms`, `colon_ms`, `sentence_ms`), filled with noise at the level of the pieces' own quietest
tenth (never above -50 dBFS) and band-limited like a room, so that the pause is neither digital zero nor a different noise from the speech.

## The checks

- `silences`: every silence of 120 ms or more inside the speech, by a threshold 38 dB under the speech level.
- `pause_audit`: puts each silence after a written word (the recognised and the written words are matched by sequence; a misheard word by its place) and marks it a defect when
  it is longer than 150 ms with no punctuation there.
- `click_report`: the largest sample-to-sample jump and the energy above 6 kHz in each join, against the recording's own usual values.
- `splices`: speech cut into, or starting out of, digital silence within 5 ms. `dead_gaps`: runs of exact digital zero.

## Settings

`tempo*` (a pitch-preserving time-stretch, 1 means none), `pause_s`, `pace_min`/`pace_max`, `voice_tries`, `voice_strict`, `voice_judge`, `speech_level_db`, `loudness_lufs`,
`respell`, `respell_rules`, `lexicon_file` (settings.py). The clause voice's own parameters are `clausevoice.Params`; they are written next to every audio it makes, with the seed of each
chosen take, so the same text gives the same audio again.
