# Short answers and long final texts

Why a final text comes out shorter than asked, what fixes it cheaply, and a design for producing long final texts in
segments. Paths are relative to the repository root. Facts marked (measured) come from `tests/QUEUE_night.md` item N10 and
`tools/length_probe.py`; everything else was read in the code. The design is not built.

## 1. Causes, by weight

1. **The model ends its turn early (measured, dominant).** Thinking off, cap 24000, 10000 words asked: median 5045 words
   (min 4446, max 5152), every run `finish=stop`. 6000 asked gives 3285, 3000 asked gives 2073: about 50%, 55%, 69%. Agent mode
   is the same. `end_turn_bias` helps little: 0 to -3 reaches up to 8261 words (median 6649 at -3), from -5 the text loops.
   It is off by default (`docs/precision.md`) and refused with parallel slots (`serve/api/logit_bias.py`). A continue-on-length
   loop alone cannot fix this: the finish reason is `stop`.
2. **The model emits the end token mid-word.** About 1 in 48 on a clean history; a cut reply left in history raises later
   cuts to 20-40%. Reproduces at temperature 0 and with speculation off. The Chat flags and drops such a reply
   (`serve/web/chat.js`); the Agent and the API do not. The loaded file is IQ3_XXS with KV q4_0: a plausible contributor,
   not measured. Compare with an IQ4_XS file and KV q8_0.
3. **The Agent path is greedy and ignores `finish_reason`.** `tadamoo/agent_loop.py` sends temperature 0 on every turn
   (`tadamoo/client.py` always puts it in the payload), which overrides the model file's 1.0 / 0.95 / 20. The loop never
   reads `finish_reason`: a message without tool calls is FINAL, so a cut reply is delivered as complete. The pipeline retry
   says "write it again" and hits the same ceiling (`tadamoo/pipeline.py`). On the cloud route `DEFAULT_MAX_TOKENS = 4096`
   (`tadamoo/llm/client.py`).
4. **The model sees only part of the source.** Tool results outside `read_file`, `read_attachment`, `load_skill`,
   `load_capability` are collapsed to the first 15 and last 20 lines above 40 lines (`tadamoo/context.py`); fetched pages
   are cut at 40,000 characters, `read_attachment` shows 12,000 characters per call, local knowledge search returns 4 chunks
   of about 900 characters. A rewrite or translation of what was found is short because the input was short.
5. **Silent clamps on the server.** No default output cap (unset means the rest of the context), but `fit_max_tokens` is on:
   the cap shrinks to the room left and the only trace is `finish_reason: length` (a log line showed a reply cut at 60 tokens
   by a 65k prompt). The Chat keeps 1024 tokens of room; the default `compact` policy replaces earlier turns with a summary of
   at most 1536 tokens, which destroys a source document placed in an earlier turn.
6. **One budget for thinking and answer.** Thinking is on by default with no budget; there is no separate answer cap. The
   repeat guard only catches 256 identical tokens: two logged runs went about 40k tokens at 100% draft acceptance (a text
   loop) before the cap.
7. **No client help on `length`.** The Chat stores `finish` but shows no notice and no Continue button; the Agent page has
   Continue only for `max_turns`; the Android app shows nothing. Billion Context (proxy that compresses long conversations
   above 90% of the window) must be checked so it never compresses the text being translated or rewritten (risk, not proven).

TaDaMoo's own prompts do not ask for brevity (grep of `harness/`, `serve/`, the template and the web files).

## 2. Cheap fixes, in order

1. Agent: stop forcing temperature 0 (use the model file's sampling, pass the Chat preset into Agent sessions); read
   `finish_reason`: on `length` with no tool call, append a note "continue exactly where you stopped", at most 2-4 times,
   emit a visible `cut` event; on empty content after thinking, retry once with lower reasoning effort.
2. Chat: Continue button when `finish === "length"`; a notice when the server clamped `max_tokens`; raise the 1024 reserve
   (8192 when no cap is set).
3. Server: `max_answer_tokens` separate from the thinking budget; a multi-token loop guard (a 12-gram seen 4 times in the
   last 2000 tokens) next to the 256-token one.
4. Harness: put fetch/browse and knowledge results in the verbatim set with their own bounds, or let the long-answer mode
   read the saved artifact file so the whole source never has to fit one turn.
5. Check Billion Context and `compact` with an 80k-token document and a verbatim-quote test; prefer `stop` policy when a
   document or attachment is present.
6. A visible "Long text" preset (`end_turn_bias` -2, thinking off, no tools) for Chat only: it cannot replace segmenting.
7. Repeat `tools/length_probe.py` and queue items N3/N4 on IQ4_XS and KV q8_0 (the owner's decision).

None of these fixes "50% of a 10000-word request"; that needs the mode below.

## 3. Design: a long-answer mode (final texts only)

Never ask for 10000 words in one turn: the model delivers 700-1500 words per turn reliably. Plan, then one segment per turn,
stitched into one streamed answer. Used only for final prose (RAG rewrites, translations, summaries, chapters, reports,
analyses), never inside a tool step.

**Choice for a local model with a prefix cache.** One orchestrator with two plan sources: source-aligned (translate, rewrite,
RAG rewrite: the source blocks are the plan, the source dictates the length) and outline-expanded (generate). A
continue-on-length loop stays as a safety inside each segment. Map-reduce summarisation only as a digest stage for a summary
of a source larger than the context. Layout: transcript (segment k is a new user turn after the previous segments as
assistant turns, so each prompt is the previous prompt plus the previous answer plus one instruction; the engine re-reads
only the new tail); fall back to a rolling layout past about 60% of the context. Keep the prefix (brief, glossary, outline)
byte-identical between segments and build history from the generated tokens, not re-rendered text.

**Flow.** Intake (mode, task, source, target length and language, style, budget) -> plan (source blocks of 800-1500 tokens
from a Markdown-aware splitter, or a grammar-constrained outline `{title, sections:[{id, heading, goal, target_words,
key_points, must_cite}]}`; for translation a one-pass glossary and style sheet) -> stable prefix -> segment loop (thinking
off, model-file sampling, cap 1.6 x target, optional `end_turn_bias` -1.5 to -2; measure words against target, one
token-exact continuation if under 70%, adapt the next ask) -> memory update (small grammar-JSON call: new terms, entities,
open threads, one-line summary, delivered in the variable tail) -> stop rules (plan done, token / segment / time budget, two
segments under 50%, repeated-segment detector, client disconnect or stop) -> assembly and verification.

**Rich content across chunks.** Protected units are found before any model call and never split: fenced code, mermaid,
tables, `$$` math, HTML blocks, front matter, list items with continuation, footnote definitions, block quotes, headings.
Opaque tokens replace images, URLs, link targets, `basalt-file:` ids, artifact references, citation keys, footnote refs,
inline and block code (`⟦IMG3⟧`, `⟦URL12⟧`, `⟦CODE4⟧`); the model copies them, the server restores the originals byte for
byte. Mermaid in translation: labels out as JSON, translated, block rebuilt deterministically. Tables are atomic; one cut by
the cap is discarded back to its start and regenerated. Footnotes and numbering belong to the orchestrator (segment-local
keys, renumbered at assembly, every reference checked). RAG citations: a source list S1..Sn in the brief, the allowed
sources per segment, the reference list built at assembly. The assembled answer is saved as an output file and exported through
`serve/exportdoc.py` and `exportoffice.py`. A small Markdown state machine holds back text until a fence or table closes
when streaming, so the client sees one continuous stream with at most a paragraph of delay. Seams: the last ~120 words of the
previous segment as anchor, overlap of 8+ words trimmed.

**Verification (deterministic first).** Every protected token exactly once, in order; fences balanced, table column counts
equal; heading and footnote consistency; link and image targets equal the originals; words per block against the expected
ratio (en to it about 1.1-1.25), blocks under 0.7 flagged; repeated 6-gram ratio, duplicate paragraphs, leaked instructions,
source-language leakage; glossary terms, digits and capitalised names present. Repair only flagged blocks, one retry each,
never a whole-document rewrite (it returns to the ceiling in section 1).

**Hard rules in the prefix.** Write only the requested segment, start exactly after the previous text, never restate earlier
sections, never summarise or conclude unless last, copy every `⟦...⟧` token unchanged, use the glossary terms exactly, keep
numbering and headings, keep the target length, output nothing but the text.

**Where it plugs in.** New `serve/longform.py` (pure functions: splitter, protect/restore, schemas, verifier, state
machine; unit-testable without a model); new `serve/api/longform.py` with `long_answer_chunks(...)` modelled on
`structured_retry_chunks` in `serve/api/openai.py`; a request field `long_answer` on `POST /v1/chat/completions`
(`{mode, target_words, target_language, source, style, glossary, sections, budget, preserve}`; `true` means defaults; the last
chunk carries the metadata); an optional `POST /api/chat/long` with SSE events (`plan`, `segment_start`, `delta`,
`segment_done`, `verify`, `done`) for progress, abort and resume; a "Long text" toggle and progress line in `chat.js`; a
harness tool `compose_long_answer(...)` and a hand-off at FINAL in `agent_loop.py` when the pipeline card has a large
`min_words`. The engine needs no change.

**Minimum viable version.** Modes translate/rewrite (user glossary only) and generate with an outline; transcript layout to
60% of the context; protection of fences, tables, images and URLs; deterministic verification; one retry per short segment;
budgets; abort by disconnect. About 600-800 lines for `longform.py`, 250 for the wrapper, 60 for the handler, 120 web, 600
of tests; 3-4 days. Risks: later segments still shrink (ask factor and retries), seam repetition (anchor and trimming),
mangled tokens (inventory, one regeneration, visible warning), one long job monopolising the single stream (decide whether it
holds the turn or yields), cache loss if another request interleaves, `end_turn_bias` unavailable with `--parallel`.

**Measure.** Delivered over requested words per segment and total against the single-shot baseline (50%, 55%, 69%): target at
least 90%, no segment under 70%; retry rate; overlap trimmed; protected-token loss (target 0); chrF++ against single-shot
translation; glossary compliance; wall time and tokens re-read per segment; overhead of plan, glossary and memory calls
under 10%. Real-model runs go to `tests/QUEUE_daytime.md`.
