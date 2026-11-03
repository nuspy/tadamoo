# Long answers

A model asked for a long final text ends its turn early, well short of the length asked, however large the output limit is. This page describes what TaDaMoo does about it. The reasons and the design are in
[harness-study/long-answers.md](harness-study/long-answers.md). Code: `serve/longform.py` (the engine of the mode: pure functions and
`LongAnswer`), `serve/longflow.py` (the flow the harness drives: estimate, plan, parts, check), `serve/api/longform.py` (the `long_answer`
field), `serve/web/longform.js` and `longform.css` (the control), `tadamoo/session.py` (`_long_step`, `_long_run`), and the guards in
`serve/service.py` and `serve/util/ngram.py`.

## A reply that is cut is shown as cut

- **Chat.** When the output limit ends a reply (`finish_reason: length`) the Chat asks the model by itself to go on exactly where it
  stopped, with the same hidden turn as the **Continue** button, and the new words join the same message under a line "Continuing by
  itself (2 of 4)". The setting is Chat settings > *Automatic continuations*: default 4, 0 turns it off, kept with the other Chat settings and
  in the presets. Stop ends the chain. A reply that the server stopped because it was repeating itself (`repeated: true`), one that reached
  *Max answer tokens* (`answer_capped: true`) and one that brings no new words are never continued by themselves. When the limit is reached
  the message says it was cut and the **Continue** button stays (it starts a new chain). A looped reply has no Continue (it would write
  the loop again), only *Ask again*.
- **Agent.** The loop asks the model to go on twice by itself (`docs/harness.md`, *How a reply ends*) and the page says so under the
  answer ("asked to go on (1 of 2)") and joins the pieces into the same text. An answer that is still cut after that shows the line "The
  output limit ended this answer" and a **Continue** button that sends one more message.
- **How a continuation joins.** The cut text goes back to the model only up to its last whitespace, so the model writes the unfinished
  word again; the join (`tadamoo/continuation.py`, the same in `serve/web/cuttext.js`) leaves no doubled or missing space, no second
  paragraph break and none of the words the model repeated at its start. The segmented mode joins its parts' continuations the same way.
- **What tells a cut reply from a finished one.** Only `finish_reason`: `length` is the output limit (or the answer cap, or a loop, which carry
  their own flag), `stop` is the end-of-sequence code, which the model also writes when it simply ends early. An early end with `stop` cannot
  be told from a finished reply by code, so nothing continues it by itself; only the segmented mode catches it, because it checks the length
  of every part against what was asked and writes a short part again.
- **`max_answer_tokens`** (a request field and a Chat setting, *Max answer tokens*): the most the answer text may take, not counting the
  thinking. `max_tokens` is one budget for thinking and answer together; this one is for the answer alone. The reply ends as `length`
  and the final chunk carries `answer_capped: true`. The server default is off (`Service.max_answer_tokens`).
- **Loop guard for a passage.** Besides the guard for one token repeated 256 times, a passage of 12 tokens written 4 times, apart from
  each other, within the last 2000 tokens ends the reply as `length`, with a warning line in the server window and `repeated: true` in
  the final chunk. A group of at most 3 different tokens (a table rule, a row of dashes) and everything inside a code fence are never
  counted; nor is a request with a JSON `response_format`, whose keys repeat by nature. `Service.repeat_ngram = (12, 4, 2000)`; `None` turns it off.

## The segmented mode

For a final text that is too long for one turn the text is planned, then written one **segment** per model call (about 2000 words, never
over 2500), and the segments are put together. Each segment is checked by plain code before it is kept.

Two ways to start it, both with the same engine:

1. **By hand.** The **Long answer** button beside Send (Chat and Agent). A small dialog asks what to write (a new text from the message,
   a translation of it, a rewrite of it), the length, the language, a style, a glossary, and whether images, links, code, tables and
   diagrams are kept as they are. It applies to the next message only. In the Chat the options go to the server as `long_answer`, and a new
   text then goes through the same flow as in the Agent (below; the page's length field is a declared length, so no estimate is asked, and
   a plan given as `outline` is followed as it is); in the Agent the message ends with one line, `[long answer: mode=translate; language=Italian]`, which the harness reads and acts on at once:
   no card, and for a new text no length estimate (the person chose it).
2. **By the harness.** A deterministic check of the person's request (`detect_long_request`): a length asked for above 3000 tokens (about
   2000 words; "10,000 words", "10.000 parole", "20 pages", "5k words") with a writing verb or a document kind, a chapter, a translation or
   rewrite of a text over 3000 tokens, an analysis of a very large text. Words become tokens in one place (`tokens_for_words`): 1.35 tokens a
   word for English and 1.5 for Italian and any language not recognised, by the language of the request, so 2100 words ask for parts in
   Italian (3150 tokens) and not in English (2835). A chapter with no length counts as 2500 words. Where the person is, a card offers "About
   10,000 words (13,500 tokens): write it in 5 parts?" (the first option is recommended) and the turn ends on it; where nobody watches (a routine, `--unattended`) the offer is taken at once and
   noted as an assumption. Once taken, **the harness writes the text itself**: the model is not asked to call anything. There is no tool for it
   (`plan_long_answer` was removed, so the stable tool list lost one entry once: a conversation saved before that has a different cached
   prefix on its first request).

A translation or rewrite found by the rules needs what the model used to give: the language comes from the request ("into Italian", "in
inglese"), the instruction of a rewrite and the source from the request's first paragraph when it is short and a text follows it
(`split_request`). When the language or the instruction cannot be read, the harness leaves the request to the model. The Agent's control
line carries them explicitly.

### The flow for a new text

`LongFlow` (`serve/longflow.py`) is `LongAnswer` with a step before the parts and one after. Calls 1, 2 and 4 are short and carry only
what they need (no tools, no history, thinking off, temperature 0, a grammar-constrained form); they also carry the side session's request
fields (`serve.longform._side`, empty where the build has no side session). The parts (3) and the rewritten passages stay on the
conversation's own session, with the earlier text turns of the conversation ahead of them.

1. **Estimate.** The question alone, the form `{words, reason}`. The words are clamped to 50..200000 and bounded: within 0.8 to 1.25 times
   a length the request states, else within a quarter to four times the rule-based words. A reply that cannot be read falls back to the
   rule-based words (`source: rule`). At or under the threshold (3000 tokens) the harness stops and the model answers as usual
   (`estimate` event with `long: false`).
2. **Plan.** The question, the words and the number of parts (about one per 2000 words); the form is `{title, chunks: [{id, heading, goal,
   target_words, key_points, instruction}]}`. Caps are enforced in code: the count must allow parts of 700 to 2500 words (one more try
   with the allowed range, then an even plan with `fallback: true`), the parts are numbered in order (the model's `id` is not trusted),
   their words are scaled to the total and kept within 700..2500, and every field is cut to a limit. `instruction` is the exact task
   for the writer of that part.
3. **Parts.** One call each, by `LongAnswer`: protected content, the checks after each part, continuation of a cut part. The part's message
   carries its `instruction` and key points, and the harness adds where the previous part ended (the last 120 words).
4. **Check.** For each piece of the text that fits (consecutive parts up to 3000 words) a short call sees the request, the plan and the
   piece, and lists fixes `{op: replace | insert_after | delete, chunk, anchor, instruction}`, the anchor being 6 to 12 words copied from the
   text. The harness finds the anchor itself: exact, then ignoring whitespace, quote marks and dashes, in the named part only; under 4
   words, not found, found twice, or crossing two paragraphs, the fix is not applied. The passage is the paragraph that holds the anchor;
   code blocks, formulas, HTML, headings and tables are never rewritten or deleted. A replace or an insert is written by a focused call
   (the passage with its protected content masked, the text just before and after, the instruction), checked as a part is (markers once
   each, language, no remark about the task, fences, tables), its length kept within 0.4 to 3 times the old passage (an insert 15 to 450
   words) and the part within half of its planned words, and then put in. A passage that fails twice is rejected. At most 2 rounds and 10
   fixes a round; the check stops when the verifier lists nothing or nothing could be applied.

Everything found and not applied is reported: `unapplied` in the final event (round, part, op, anchor, instruction, reason), and a
`fix_not_applied` warning each. The final text replaces the text shown while the parts were written (`revised` event) and is the answer
of the turn: the history holds it whole, so a later message can refer to it, and the **Save as file** button saves it.

### Parts from a plan

The number of parts comes from the total, not from the sections: `chunk_count(total, segment_words)` rounds `total / segment_words`
(`segment_words` is 2000 by default, kept between 700 and 2500) and adds parts until none is over 2500 words. So 10,000 words is 5
parts, or 7 with `segment_words: 1500`. A plan given in the request (`outline`) follows the same rule. Each section is then placed on the word axis and a part takes the sections that fall in its
share: a long section is written over several parts ("continues from the previous part"), several short ones share one part, and each
heading is written where its section starts.

For every part the prompt is built from a stable start and a variable end:

- the **stable start**: the rules (write only this part, start exactly after the previous text, never summarise or conclude unless it is the
  last, copy every marker unchanged), the task, the glossary, the style and the **whole plan**;
- what is **already written**: in the *transcript* layout the earlier parts themselves, as assistant turns, so the server's cache reads
  only the new end; past 60% of the context, the *rolling* layout: one line per finished part and the last 120 words as an anchor;
- "**now write part k**": the sections it covers, the goal, the key points, about N words, and whether it is the first or the last.

Without a plan (`long_answer` with only a task and a length) the model is asked once for an outline in JSON; if it cannot give one, even
parts are used and the plan event says `fallback: true`.

For a translation or rewrite the source blocks are the plan: a Markdown-aware splitter cuts the source at paragraph, list and heading
boundaries into parts of about `segment_words`; a heading stays with the text after it, and a part's expected length comes from its source
(English to Italian about 1.15 times the words). A table or a code block left in the source (`protect: false`) is one unit; one longer than
1.4 parts is the only thing cut inside: at rows or lines, the table header repeated and the code block closed and reopened in each piece
(`tadamoo/markdown_chunks.py`).

### Content that must not change

Before any model call the source is scanned and these are replaced by markers `⟦NAME12⟧`, which the model must copy:

| Kind | Marker | Kept as |
|---|---|---|
| fenced code (including mermaid), `$$` math, HTML blocks, front matter, link reference lines | `CODE`, `MATH`, `HTML`, `FM`, `REF` | a block, in order |
| images `![alt](src)` | `IMG` | whole |
| link targets, bare URLs, `basalt-file:` ids | `URL` | the target only; the link text is still translated |
| inline code | `IC` | whole |
| citation keys `[S1, S2]`, footnote references `[^1]` | `CITE`, `FN` | whole |

Tables are not masked (their cells are text) and not split unless one alone is longer than a part, and their shape is checked. At the end every marker is replaced by its
original byte for byte; a marker the model made up is dropped; a marker used twice is kept once. Diagrams are carried through unchanged:
translating the labels of a mermaid diagram is not done.

### Checks after each segment

Plain code, no model: not cut (`finish_reason`, and a paragraph ending on a letter, which is continued), every marker present once and
the block markers in order, length within 0.7 to 1.6 of what was asked, language (a stopword count over English, Italian, Spanish,
French, German and Portuguese), no remark about the task ("Here is the translation"), no repeated 6-word groups (over 30%), no paragraph
of an earlier segment written again, fences closed, tables with a constant column count and the source's shape, and for a translation:
heading levels, the numbers of the source, the glossary terms.

A cut part is continued (up to twice). A part with a hard problem, or too short, is written again once with the reasons in the
message (`retries`, 0 to 3); the failed text is never sent back. What remains after that is **reported, never hidden**: the segment
carries `warnings`, the final event lists them, a marker that was lost is put back at the end of its part (blocks and images) and
named, and the answer's `finish_reason` is `length` when any part is still cut or the text stopped early. In a new text, two segments
in a row under half of what was asked stop the run (`stopped: "short_segments"`). Segments are never rewritten as a whole document.

### Pieces and seams

The rules of a part end with "End the part after a whole paragraph, list item, table or code block, never inside one". A part reaches the
reader in pieces of about 3000 characters (`delivery`), each ending between Markdown blocks, so a page that draws Markdown as it arrives never
shows half a code block or table; a block longer than a piece is one piece. The parts are joined by a blank line, or by a line end where the
second part goes on with the list or the table the first ends in (`join_parts`, `joiner`), and the pieces joined are the text of the answer.

### The request

`POST /v1/chat/completions` with `long_answer`: `true` (defaults), or an object:

| Field | Meaning |
|---|---|
| `mode` | `write` (default), `translate`, `rewrite` |
| `task` / `instruction` | write: what to write (default: the last user message); rewrite: what to change |
| `source` | translate, rewrite: the text (default: the last user message) |
| `target_words` | the length of a new text (50 to 200000, default 2000) |
| `target_language`, `style`, `glossary` | language, style, `{term: translation}` |
| `outline`, `title` | a plan: `[{heading, goal, target_words, key_points}]` (new text only) |
| `segment_words` | 200 to 2500, default 2000; a plan's parts stay within 700 to 2500 |
| `sections` | the number of sections to ask the model for |
| `headings` | write the section headings (default: for 3 parts or more) |
| `retries` | 0 to 3, default 1 |
| `protect` | keep images, links, code and the like (default true) |
| `max_segments`, `max_seconds` | refuse a text needing more parts; stop after this time (default none) |
| `end_turn_bias` | passed to every call |

System messages and earlier turns ride ahead of the parts (so material gathered in the conversation is there); thinking is off for the
parts; the request's sampling fields apply to every call. `tools`, `response_format` and images are refused. A bad field is a 400
before anything is written. Billion Context never compresses a long answer: the Chat leaves its header off, and a client of the API
must do the same.

The stream is ordinary chat completion chunks. Text arrives as content deltas, one checked segment at a time. Progress arrives as chunks
with an empty delta and a `long_answer` event: `plan` (segments, words, `fallback`), `segment_start` (index, of, layout),
`segment_progress` (words so far, about once a second), `segment_done` (words, target, retries, warnings), `verify`, `done` (finish,
`stopped`, words, segments, warnings, seconds, usage with cached tokens). The last chunk carries `finish_reason`, `usage` and the same
`long_answer` totals; without `stream` the response has them as `long_answer`. Closing the connection stops the call in flight; nothing
is written after it. A model error is raised after the segments already delivered.

A new text (`mode: write`) in the Chat has the events of the Agent as well (`estimate`, `review`, `fix`, `revised`, below), and its short
calls (estimate, plan, check) are held to their form by the server's grammar and carry the technical session's fields. When the check
puts a correction in, the `revised` event holds the whole corrected text: the page replaces what it showed with it, and a response without
`stream` has it as the message content. An estimate under the threshold (a request that did not declare its length) still writes the text,
in as few parts as it needs. The text is the answer of the turn and the page keeps it whole in the history. A translation or rewrite goes part
by part from its source, with no plan call and no check.

In the Agent tab the harness drives the same engine with the conversation's own client, so it works the same on the loaded model and on a
cloud route. Its events arrive as `long_answer` agent events: `estimate` (words, `source` model, rule or asked, `long`), then those above,
then `review` (round) and `fix` (round, `n` of `of`, `op`, `chunk`, `status` applied, unlocated or rejected, `reason`), `revised` (the whole
text, when a fix was put in) and `done` with `applied`, `rounds` and `unapplied`. The page shows one line that follows the phase ("writing
part 3 of 10", "correction 2 of 3 made in part 4"), the totals and what was not applied.

## Save as file

Under every assistant message, next to **Copy**, a **Save as file** button (Chat and Agent) opens a small window: the format (Markdown by
default, or Word, OpenDocument, HTML, PDF; the same ones as the conversation export) and the file name. Only that click saves anything. The
text of that message alone goes to `POST /api/export` with `bare: true`: no title line, no export header and no speaker heading, the
headings as written (`serve/exportdoc.py`, `serve/exportoffice.py`); the system's "Save as" window lets the person choose the folder
(`serve/web/export.js`, `window.TadamooSave.message`). In the Agent the text is the answer's text blocks, as Copy gives it.

## Not done

- Abort and resume across connections (`POST /api/chat/long`): closing the connection stops the run, and a new one starts again.
- A glossary or style sheet made by the model, and a one-line "memory" call between parts (new terms, open threads).
- Translating the labels of a mermaid diagram, renumbering footnotes across parts, a reference list built from the citation keys.
- Streaming inside a segment: text appears when its segment has passed the checks, a part at a time.
- `max_answer_tokens` for Agent sessions, a bigger room for the Chat context reserve, and a notice when the server clamps `max_tokens`.
- Map-reduce summaries of a source larger than the context.
- The text is never saved to a file by the harness; only the person saves it.
- The check and the estimate for a translation or rewrite (their source is the plan, and the parts are checked against it).
- Gathering material with tools before a long text: the parts are written from the request and the conversation, not from files the model reads first.
- Finding the language or the instruction of a translation or rewrite that the request does not state plainly.

Real-model checks of the parts are rows 57 to 62 of `tests/QUEUE_daytime.md`, those of the estimate, the plan and the check are rows 70 to 73.

## Tests

`tests/test_longform.py` (protection, splitting, checks, plan, chunking, the run with a scripted client), `tests/test_longform_flow.py` (the estimate, the plan, the
parts with their own task, the check and the placing of its fixes, the side fields), `tests/test_longform_session.py` (the card, the
unattended offer, the control line, the harness writing the text, the tool's removal), `serve/tests/test_longform_api.py` (the request field against the mock engine,
`max_answer_tokens`, the passage guard), `serve/tests/test_longform_chat_flow.py` (the Chat's `long_answer` through the new flow, a scripted
engine answering each kind of call), `tests/test_long_text_pieces.py` (the cuts and joins between Markdown blocks: digest, attachments, delivery),
`serve/tests/test_longform_page.py` (the markup and strings), `serve/tests/test_chat_continue.py` (the
Chat's own continuing, run in node against the page's script), `tests/test_continuation.py` (the join, in Python and in the page's script).
Real-model checks of the continuing, the part size and Save as file are rows 66 to 68 of `tests/QUEUE_daytime.md`.
