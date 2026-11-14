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

### Where the time went, on one sentence of 8.5 s (Hungarian, one clause, 4 takes in a round, RTX 5090 at 600 W, warm process)

| Phase | Before | After | What changed |
|---|---|---|---|
| XTTS process start and model load (once) | about 15 s | about 15 s | unchanged: import 5 s, build 4 s, checkpoint 5 s, to the GPU 0.4 s; the mmap load of callAPIcall (build on the GPU) is no faster here |
| Making 4 takes | 3.0 s | 3.3 s | one batched pass (already); a take that runs on to 1.5 times the first one to finish is ended |
| Whisper read-back, 4 takes + 1 for the comma pauses | 6.5 s (5 calls, 1.3 s each) | 1.9 s (one pass for the 4 takes, the words kept for the comma pauses) | `localstt.LocalWhisper.words_many`: the takes decoded together, aligned one by one |
| CTC read-back (letters), 4 takes | 4.7 s on the CPU | 0.6 s on the GPU | `rating_device` auto |
| Pitch (pyin), 4 takes | 1.9 s | 0.5 s to 2.9 s while the Whisper runs | hop of 40 ms instead of 20 ms, run at the same time as the Whisper |
| Speaker embedding, 4 takes | 0.9 s on the CPU | 0.4 s on the GPU | `rating_device` auto |
| Pause audit, loudness, joins | 0.3 s | 0.5 s | unchanged |
| **The sentence, process warm** | **17.2 s (2.0 times real time)** | **6.8 s (0.8 times real time)** | |

The first sentence of a process also pays the loading and the first use of the GPU by each model (56 s before, 63 s after). Not measured here: the voice server's own Whisper, which a production
used until now (it answers one request at a time), so the "before" read-back was timed with the same Whisper medium in this process.

What callAPIcall does for XTTS (`call-bridge/callbridge/engines/xtts_tts_helper.py`): the same environment (torch 2.11 cu128, transformers 4.57, coqui-tts 0.27), one helper process kept loaded, the model built
straight on the GPU from a memory-mapped checkpoint, the reference latents kept for each voice, the 58 built-in speakers, `inference_stream` in blocks of 20 tokens (the first sound after 0.9 s), a cap
on the tokens in proportion to the text, the engine's own `speed`. Its half precision is off (it failed), and it has no DeepSpeed, no compile and no CUDA graphs; the speed it measured is 1.5 times real time, slower
than the 0.36 here. Its fast engines are another model (Qwen3-TTS with CUDA graphs, 3.7 times real time) and a quantised one (16 times). So the speed of XTTS was never the delay: the delay was the rating.

### The rating of the takes (what turned 8.5 s of voice into 17 s)

- **Cheap measures first.** The CTC letters, the speaker and the pace of every take are measured first (on the GPU); a take that one of them rejects is not read back (`Rater.measure(partial_ok)`), because no read-back
  can change that verdict. The takes that go on are read back together.
- **One Whisper pass for the round** (`LocalWhisper.words_many`), kept loaded, with the words kept for the comma pauses so they are not asked for again. Several processes do not help: the GPU time-slices
  between processes, a batch in one process does.
- **Takes measured at the same time** (`rate_threads`), the pitch while the Whisper works.
- **The read-back in this process** (`read_back` `auto`: when the GPU has 8 GB free, else the server's Whisper) and **without the server at all** (`speak.py --stt local`): the profile of a new voice is read from the local XTTS
  (16 readings, about 80 s with the model load, once per voice) and the read-back is the local Whisper.

### Making the takes of a round faster

- **Batched seeds.** The takes of a piece in a round share a text and settings, so one autoregressive pass makes them all (`num_return_sequences`, `xtts_batch`, 12 at most): a decoding step
  is bound by the launch of its small kernels, so a step for four takes costs about what a step for one does. The batch is seeded once, with the seed of its first take; each take is then cut at
  its own stop token and turned into audio by itself, as a single take is. A batch that runs out of memory is made again in two halves. A piece with a take missing from the cache is made again whole.
  A take made in a batch is not the take its seed made alone (the draws are shared); a right take is judged by measurement, so this changes nothing in what is right.
- **Precision.** `xtts_precision` `tf32` (fp32 with TensorFloat-32 matrix products), `fp16` or `bf16` (autocast over the autoregressive pass; the latents and the vocoder stay fp32). The default
  stays `fp32` until takes made in lower precision have been heard and their share of right takes is the same.
- **Compile, as LTX Studio does it.** `xtts_compile` `on` compiles the transformer of the autoregressive pass with `torch.compile` in mode `default` (no CUDA graphs: their output buffers are
  reused between runs), `fullgraph` off, `dynamic` on (the cache grows each step) and `torch._dynamo.config.suppress_errors` (a graph that cannot be compiled runs eagerly). That is how LTX Studio compiles
  its transformers, where it is opt-in too. It needs Triton, which on Windows is the `triton-windows` package: the XTTS environment has the same torch (2.11, cu128) as LTX Studio's inference
  environment, which runs `triton-windows` 3.7 on it. `python -m tadamoo.media.components install speech-synthesis` installs it into the XTTS Python, only when it is missing. A compile that
  fails is reported once and the pass goes on without it.
- **Attention and cache.** The attention is PyTorch's SDPA (the default of transformers for this model) and the decoding keeps the model's own key-value cache (`kv_cache`); the line the worker prints when
  it loads the model says so. SageAttention (which LTX Studio uses for its large transformers) is not used: a decoding step has one query, and the saving is in long sequences.
- **Kept loaded and kept made.** The model is read once for a model folder, the conditioning latents of each speaker once for each speaker (a change of voice costs nothing), the seeds of a sentence
  follow its text, not its place, so a sentence said twice is made as the same takes, and `xtts_cache` keeps the takes of a sentence by its text, seeds and settings in a folder that any later production finds.
- **The rating on the GPU.** The CTC read-back (a 1B model) and the speaker embedder run on the CPU by default, to leave the GPU to the loaded language model; `rating_device` `cuda` moves both to the GPU.
- **Longest time.** The wait on the voice is the longest the audio allows (`tadamoo/jobtime.py`: the factor times the median of the recorded ratios times the seconds of audio): the XTTS run
  (`LocalXtts` with a `Timing`), a recording of the voice service and the wait for its speech-to-text. A job that ends well records its ratio; one over its limit is ended with an `Overrun` that says the numbers.

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
