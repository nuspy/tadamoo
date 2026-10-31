# Intake, questions and suggestions

What the harness does between the person's text and the model's first step, how the agent asks the person, the chips offered after an
answer, and the optional rewrite of each message. Code: `harness/intake.py`, `harness/optimize.py`, `harness/questions.py`, `harness/suggestions.py`, the hooks in `harness/session.py`, `serve/intake_prefs.py`,
`serve/web/askcard.js`, `serve/web/promptswitch.js`, `serve/web/optimize.js`, `serve/web/intake.js`, `mobile/lib/src/ask_card.dart`. The designs and their sources are in
[harness-study/](harness-study/README.md); this page says what is built. Unattended runs (routines, the Target Loop in an unattended
conversation) never wait for any of it.

## Settings

Settings > Inference holds them: the **Prompt analysis** switch first, then Questions and suggestions. Each control is explained on the page;
the values are saved on this PC (`intake.json` in the data folder, read again after a restart of the server or the app, `/api/intake`) and a conversation started afterwards uses them (`--intake`, `--side-call-tokens`,
`--suggestions`, `--optimize-prompt` of `python -m harness session`).

| Setting | Values | Default |
|---|---|---|
| Prompt analysis (the look at each request) | `off`, `rules`, `full` | `off` |
| Optimize prompt (rewrite each message first) | on, off | off |
| Suggest what to write next | on, off | on |
| Longest conversation for the extra model calls (tokens) | 500 to 200000 | 6000 |

### The Prompt analysis switch

Three buttons in a row (`role="radiogroup"`, arrow keys, Home and End move and choose; 30 px high, visible focus, the chosen one has a tick
as well as a colour). It is drawn in two places that always show the same value: Settings > Inference, and the Chat toolbar, which the
Chat and the Agent tab share (hidden in the model-only chat, which has no look at a request). A change in one is shown in the other at
once and saved after a short pause through `POST /api/intake` (so arrowing across the three saves where it stops); a refused save puts the
old value back with the reason. Saving also tells the running conversations (`update_intake`, the process command `intake`), so the next
message of an open conversation is looked at the new way.

Each state has a `title` and the same text as an accessible description; a "?" button beside the switch opens a panel with the three texts,
for touch screens, and Escape or a click elsewhere closes it:

| State | Text |
|---|---|
| Off | Your message goes to the model exactly as you wrote it. Nothing is checked or changed. |
| Rules | Quick checks, no extra model call. It removes invisible characters and extra blank lines, writes the real date next to words like 'tomorrow', and reminds the model when you listed several items. It costs nothing. |
| Full | Everything in Rules, and the model also reads your request first to see whether something is missing or risky. It may ask you a short question before it starts. It adds a few seconds to each message. |

The texts are in English and Italian (`ps.*` in `serve/web/i18n.js`).

`rules` makes no model call. `full` adds one structured call, only when the gate says so. The suggestions never run in an unattended
conversation, and the triage in one never asks.

## The look at a request

Only for a message the person wrote (not a planner note, a command, a Target Loop message or the answer to a card). In order:

1. **Cleaning** (every mode but `off`). The model reads the message without control, zero-width and bidi characters, ANSI escapes, CRLF,
   trailing spaces and runs of blank lines, in NFC. Fenced code is left as it is. Nothing is spelled, translated, summarised or reordered.
   The person's own text stays as `raw` beside it and is what the page shows.
2. **Signals and gate.** Words, listed items, an action verb (EN and IT), a word that points at nothing, a risk word (delete, send, pay,
   book, call), attachments, relative dates. The gate says "triage" for 35+ words, two listed items, an action with a pointing word, a
   risk word or an attachment with an action; in a conversation without tools the bar is 60 words or three items. It says "skip" for a
   short chat, a continuation ("ok", "continua"), a command, an answer to a card, and a conversation longer than the size setting whose
   cache is warm (an extra request could cost the next turn its cached start).
3. **The triage call** (`full` only). One request, `response_format` json_schema `strict`, temperature 0, thinking off, no tools, about 600
   tokens. Input: the message (3000 characters) and a facts block (attended or not, attachment names), never tool results or web text. The
   system part is byte-identical each time. The form: evidence quotes first, intent, size, ambiguity, the missing facts (what, why, a
   default, whether files, the web or memory could supply it, the cost of a wrong guess, whether it can be undone, who must decide),
   risks, constraints in the person's words, and the parts of a large request.
4. **Validation.** Every quotation must occur in the person's text (case and spacing aside); one that does not is dropped, and the whole
   form is dropped when more than half do not. Caps: four missing facts, six parts, dependencies only on earlier parts. Invalid JSON or a
   failed call is a skipped call: the request goes on as if there had been none.
5. **Ask or act, decided in code.** A fact the files, web, memory or tools can supply is a line "find this first". The rest is scored:
   chance of a wrong guess (none 0.05, minor 0.25, material 0.5) times cost (1, 4, 15), doubled when it cannot be undone or is not the
   user's to decide. It is asked when the score is above 3 in a conversation without tools, 2 in the Agent before any tool was used, 6 after.
   At most one question there (two if both are costly), three in the Agent, one card. At the Auto permission level only another person, a
   booking or a resource is asked; unattended, those are parked and everything else is assumed.
6. **The card.** The harness builds it from the form's own fields: the question, the default as the recommended option, a row for the person's
   own answer. The turn ends there; the answer is the next message and skips the look.
7. **The note.** A deterministic message after the person's, flagged `note` (it is not shown in the transcript and cannot stand for their
   confirmation), headed "not the user's words": the assumptions, what to find first, the person's requirements quoted, dates from the
   clock ("domani" = a weekday and a date), a reminder when the message lists three or more items, and the parts of a large request. The
   page shows a one-line version.
8. **Parts in the task registry.** A request the form calls large, with three or more grounded parts, is recorded in `/api/tasks` as
   `req-<turn>` with children `req-<turn>.1`, ... (`metadata.kind` is `intake`; title, the person's words, a "done when" and the parts it
   follows). They are closed when the request ends, never marked done on the model's word. The checklist is also in the note, so the model
   works through it in the same conversation. Running the parts one by one with their own prompts, and re-planning, are not built (the
   Target Loop does that for a described target).

## Optimize prompt

A checkbox beside the Prompt analysis switch (Settings > Inference, and the Chat and Agent toolbar, always the same value; saved as `optimize_prompt`
in `intake.json`, told to running conversations by the process command `optimize`, started with `--optimize-prompt on`). When it is on, the
message the person sends is rewritten by one short call before the model reads it. It works with the analysis in any state, including Off.

**Available only with the technical session.** Without one it cannot be switched on: the box is disabled, with the reason written beside it ("Needs the technical session") and a tooltip
that says how to turn it on (Loader > Technical session, then load the model), whenever the server's `/v1/status` (`concurrency.side_slot`, the
source `harness/side.py` reads) reports no session. The page asks when it opens, on every change of view and after a load in the Loader finishes
(`basalt:loader`). The server refuses to save `true` without a session (a 400 with the reason); `false` is always saved. If the session goes while the
box is ticked, the saved value stays, the box shows a dash and the reason "Saved as on, but not used", and the harness makes no call and rewrites
nothing (it asks the client for `side_extra()` each time, and never uses a route that is not this server). The box stays clickable then, for one
thing only: a click switches it off (it can never be switched on without the session). Tooltip: "Before your message reaches the model, a quick helper pass rewrites it as a clearer, more complete instruction. The chat
still shows what you wrote. Needs the technical session."

**The call** (`harness/optimize.py`, from `_intake_step` after the cleaning and the Long answer check, before the model's first step). One request on the
session through `side_extra()`: no tools, temperature 0, thinking off, 8 s, at most about 1.5 times the input in tokens (floor 128, ceiling 2000), a
fixed system part (rewrite as a clear, complete, self-contained instruction in the person's language; keep every fact, name, number, path, link, code
block, quotation and constraint; add no requirement; do not answer; output only the text). The user part holds the message and, for context only, the
person's own words of their previous message (800 characters at most). Messages over 4000 characters are not rewritten.

**Left alone** (the original goes, no note): a slash command, a note of the harness (`[harness]`, `[planner]`), a Target Loop message, the answer to a
card, the Long answer control line, fewer than four words, an empty message (files only), a run nobody watches (routines, unattended conversations), a
message that arrives while a task runs, and any failure or timeout of the call. A reply identical to the message is also left out.

**Thrown away** (the original goes, with a one-line note saying why): the reply was cut by the output cap (`cut`); it repeats the instruction
(`leak`) or begins like an answer ("Sure", "Ecco", "I'll ...", `answer`); its length is under half or over four times the message (`ratio`); it lost
something the person wrote (`lost`, naming it): any code block or inline code, link, address, absolute or relative path, file name, mention, quoted
string (any quotation marks) and number of the original must be in it, as the same text.

**While it runs.** The page shows the line "Rewriting the prompt…" (`optimizing` event) from the moment the call starts until it ends, whatever the
outcome (`optimized` event, also for a failure, where nothing else is shown). Stop gives the call up at once, says "Rewrite skipped: your original was
sent." and the task goes on with the original; it is the Stop of the whole conversation otherwise (it still clears what waits in the queue).

**What is shown and kept.** The bubble keeps the person's words (`raw`); the model receives the rewrite (`content`); `last_user_text` and the
checks of the answer stay the person's words. Under the message, a closed block "Optimized prompt" holds the text the model got, with a button "Send
my original instead" that sends the person's text again as a new message that is not rewritten (`optimize: false` on the send). The history stores
both (`raw`, and `opt`: `{state: sent}` or `{state: rejected, reason, detail}`, which never go to the server), so a reload shows the same note and a later
turn sees the same bytes (the cached start does not change). The audit records `prompt_optimized` with the state, the time and the lengths, not the text.

Cost: one extra call of a few seconds before each message, in the technical session (it does not touch a conversation's slot or cache). Whether it
helps is the question of experiment E8 ([experiments.md](experiments.md)); nothing says so yet.

## ask_user

One call, one to four questions; the rules the model reads (when to ask, one call, no "shall I continue", options that stand alone) are
added to the system prompt with the tool (`PROMPT_RULES` in `harness/questions.py`).

```
ask_user {intro?, questions: [{text, kind: single|multi|free|confirm, options?: [{label, description?, recommended?}],
          required?, placeholder?, default?, recommended? (confirm), involves?: ["people: ...", "booking: ...", "resource: ..."]}]}
```

- **Repairs, not errors.** A bare string is an option or a question; a missing kind follows the options; one option makes the question
  free; duplicate labels, "Other" and "(Recommended)" suffixes are dealt with; more than five options keep the recommended; two
  recommended on a single keep the first; options written as "A) ..." lines in the text are parsed; labels over 60 characters are cut.
  No usable question at all gives one line with an example; after two such calls the words the model did write are shown as one plain
  free question.
- **Limits in the harness.** At most two calls for one request of the person (an answer to a card is not a new request), never the same
  question twice; the model is told so in one line.
- **The answer** travels as `ask:` + JSON of option ids and typed text (at most 500 characters each, 4000 in all). The server checks it
  against the card it stored, not against what the page says it showed, and sends the person's next message in words: one line per
  question with the labels (never ids), `Other: "..."` for typed text, "(your recommended option)" where it applies. Skip sends a fixed
  sentence telling the model to decide and say what it assumed; "answer in the chat" sends nothing and closes the card.
- **Unattended.** Nothing waits. Each question is answered at once from what the model proposed: the recommended option (single), the
  recommended options (multi), the default text (free), the recommended side (confirm); a confirm without a recommended side and a
  question with nothing proposed get "decide yourself, conservatively". A question whose `involves` names people, a booking or a resource
  is parked in the visible pending list (answered later, even from the phone) and the model is told to go on. Every choice the harness
  made is an `auto_answered` event with `chosen`, and a routine's result ends with "Assumptions made".
- **Audit.** `question_shown`, `question_answered` (seconds and length, not the text), `question_auto` and `suggestions` records go to the
  hash-chained ledger.

## Next-message chips

After an answer that ended normally, a separate structured call (the conversation as the model last saw it plus one trailing instruction that
is never stored; `response_format` json_schema, thinking off, 160 tokens, 8 seconds) returns two to four messages in the person's voice.
They appear under the answer as buttons; a click puts the text in the message box, focuses it and sends nothing. Skipped when: unattended;
the answer is under about 30 tokens, cut, an error or a card; plan mode; a message is queued or a task runs; the conversation is longer than
the size setting; the server held under half of the last request in its cache; or after three unused sets in a row (then every second
turn). A message sent meanwhile cancels the call (the stream is closed). A suggestion is plain text of at most 240 characters. The set
is kept for a reload and dropped at the next message.

## Pages

The card is `serve/web/askcard.js` (the Chat and the Agent tab share it): real radio buttons and checkboxes in `fieldset`s, rows of at
least 40 px (44 px on touch screens), a "Recommended" tag in words and a star, a row of the person's own with a text box, a text area for
free questions, Send (disabled until every required question is answered, with a live note naming the first missing), Skip and
"Answer in the chat instead". One question of single or confirm kind is answered by one click, never by an arrow key. Digits 1 to 9
choose an option, Ctrl+Enter sends. The card is at most 720 px wide and wraps at 1024 px. After the answer it is a read-only summary.
The Android app draws the same card (`QuestionsCard`, rows of 48 dp) and the chips (`SuggestionChips`).

## What is not measured

All of it needs the real model and is queued in `tests/QUEUE_daytime.md`: whether triage pays for itself (success, over-ask rate, time),
the cache effect of the triage and suggestion calls, whether the engine's grammar accepts the schemas and `strict: true` on the tool, whether
a Qwen-class model asks at the right times, and whether the chips are sensible.
