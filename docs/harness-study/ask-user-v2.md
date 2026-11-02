# ask_user v2 and next-prompt suggestions (design)

Spec, UI, files and tests. Survey and sources: [clarification-survey.md](clarification-survey.md). Built as described in [../intake.md](../intake.md). Real-model behaviour (does a Qwen-class model ask at the right times, are the suggestions sensible) is unmeasured and goes through `harness.eval` and `tests/QUEUE_daytime.md`.

4. (c) SPEC: ask_user v2
Replaces v1 outright (no alias, per AGENTS.md "no shims"). Tests and prompt text are updated in the same change.

4.1 Model-facing tool schema (registered with `strict: true` so the server grammar constrains the call)
```
ask_user {
  "intro": string,                       // optional, <=200 chars: why you ask, one sentence
  "questions": [                         // 1..4
    {
      "id": string,                      // optional; harness assigns q1.. if absent
      "text": string,                    // <=200 chars, one sentence, no options inside
      "kind": "single" | "multi" | "free" | "confirm",
      "options": [                       // 2..5 for single/multi; omit for free/confirm
        { "id": string,                  // optional; harness assigns o1..
          "label": string,               // <=60 chars, 1-6 words
          "description": string,         // optional, <=140 chars
          "recommended": boolean }       // optional; at most one for single
      ],
      "allow_other": boolean,            // default true for single/multi (UI row "Other")
      "required": boolean,               // default true; false lets the user leave it empty
      "placeholder": string,             // free kind only
      "default": string,                 // free kind: used when unattended or skipped
      "involves": [string],              // "people: ..." | "booking: ..." | "resource: ..." -> unattended runs park it
      "depends_on": { "question": string, "options": [string] }   // optional, see below
    }
  ]
}
```
Notes:
- `confirm` is a yes/no whose two buttons the harness labels in the UI language. `recommended` marks the suggested side with a bool on the question: `recommended: true|false` (meaning yes/no).
- `depends_on` is accepted and validated (the target must be an earlier question, acyclic), and the UI hides the dependent question until the chosen option is among `options`. I recommend NOT advertising it in the tool description in the MVP. The rule "if an answer changes the next question, ask in two calls" is simpler for a Qwen-class model. Keep the field so the format does not have to change later.
- `intro`, `placeholder`, `required` and `depends_on` are all optional so a minimal call is small: `{"questions":[{"text":"...","kind":"single","options":[{"label":"A","recommended":true},{"label":"B"}]}]}`.
- Harness-side caps (not left to the model): 4 questions, 5 options, 2000 characters in total.

4.2 UI to harness answer payload
Prefix `ask:` + JSON, the same convention as `elicit:`:
```
ask:{"v":1,"status":"submitted","answers":{"q1":{"picked":["o2"],"text":""},"q2":{"picked":[],"text":"a free sentence"},"q3":{"picked":["o1","o3"],"text":"plus a glossary"}}}
```
- `status`: `submitted`, `skipped` (the Skip button: the model goes on with defaults), or `chat` (the user closed the card to answer in the chat; the next message is the answer).
- `picked` holds option ids (never labels). `text` is the Other text or the free answer, at most 500 characters.
- The server validates against the question set it stored, NOT the one the client sends back: known ids only; single means exactly one pick, or Other text if `allow_other`; multi means at least one pick or text unless not `required`; no duplicates; control characters stripped; whole payload at most 4000 characters.
- `agent_sessions.answer()` has a hard-coded `+ 500` limit for `free_prefix`; make that limit per kind (the `ask:` kind gets 4000).

4.3 Message given back to the model (plain text, one formatter in Python)
```
The user answered your questions:
1. Which database? -> PostgreSQL (your recommended option)
2. Which sections? -> Introduction; Conclusion; Other: "a glossary"
3. Anything to leave out of scope? -> (no answer)
Go on with these answers. Do not ask them again.
```
- One line per question: the question text (cut to 80 chars), `->`, the labels picked (never ids), `Other: "..."` for typed text, `(no answer)` when optional and empty.
- A tag when a value was chosen by the harness: `(chosen automatically: your recommended option)` or `(default)`.
- Whole-call outcomes: `The user skipped the questions: decide with your best judgement and say what you assumed.` or `The user chose to answer in the chat: wait for their next message.`
- Plain text beats JSON for a small model. The structured copy goes to the audit record, not to the model.

4.4 System-prompt rules (added only when the tool exists; about 170 words; same style as other harness prompt blocks)
```
Asking the user (ask_user).
- Ask only when (a) the answer changes what you will do in a real way, (b) you could not find it with your tools in a call or two (read files, search, list), and (c) guessing wrong costs more than one question. Otherwise choose the sensible default and say in your answer what you assumed.
- Never ask for permission to proceed, to confirm a plan, to run a safe tool, or about a risky action (the harness asks that itself). Never ask what the user already told you. Never ask for passwords or keys.
- One call, up to 3 questions, only questions whose answers do not depend on each other. If an answer changes the next question, ask it in a second call.
- Each question is one sentence in the user's language. Give 2 to 4 short options a person can choose as they are: complete, concrete, no placeholders, different from each other. Put the one you recommend first and set recommended. Do not write an Other option; the screen adds it. Never put the options inside the question text.
- Use kind multi only if several options can be true together; free when no list makes sense; confirm for yes or no.
- After the answer, go on with the work. Do not repeat the question or the answer back.
Example of a good call: {"questions":[{"text":"Which database should the service use?","kind":"single","options":[{"label":"PostgreSQL","description":"Already used by the other service","recommended":true},{"label":"SQLite","description":"No server, single file"}]}]}
```
Tool description (short, shown every turn): "Asks the user 1 to 3 questions with proposed answers and waits. Use only for a decision that is theirs and that tools cannot answer. See the rules."
- Include one positive example (above). Add one negative line: `Bad: asking "Shall I continue?"`.
- Small models follow an example better than a rule.
- The user's language matters: the IT user will write Italian, so the rules say "in the user's language".

4.5 Harness nudges that make a Qwen-class model ask well (Ambig-SWE says the model's own judgement is unreliable)
- Ask budget: at most 2 ask_user calls per user message. The third returns "Do not ask again; decide and say what you assumed."
- Repeat guard: the same normalised question text again returns "Already answered: <answer>". `harness/repeat_guard.py` exists; reuse its hashing.
- Plan mode and the first message in an empty folder (the "Plan first" case in vibe-coding.md): add one line to the mode prompt, "ask up to 3 questions with ask_user before presenting the plan if the goal, the stack or the constraints are not stated".
- Optional phase 3: a cheap grammar-constrained "ambiguity check" call before the first step of a NEW Agent-tab task, which lists at most 3 missing facts or an empty list. This is the detect/execute split from "Ask or Assume?". It adds a model call per task, so make it off by default and measure with `harness.eval` before enabling.
- Evaluate with a new `harness.eval` set: some underspecified tasks where asking is right, and some fully specified tasks where asking is wrong. Report ask rate and over-ask rate. Label all numbers with the GPU power limit. Real-model runs go into `tests/QUEUE_daytime.md`.

4.6 Harness behaviour

Attended (the person is there)
- MVP: keep today's end-the-turn behaviour. The tool puts the question to the person and ends the turn (`stop_reason "asked"`), and the formatted answer is the next user message.
  - Reason: no new waiting machinery, it survives a process restart and a closed app, and the tested path stays. The prompt cache prefix is the same either way.
  - New: the answer goes to `/answer` as structured JSON and the SERVER formats the message (see 4.3), so the web and the Flutter app do not each re-implement the format.
- Target (phase 2): block like `_ask`. The tool returns the formatted text as its tool result, in the same turn. Slot, `clock_paused()` and no timeout are reused. The tool result is then the answer, so the model keeps its tool-call state.
  - On a process restart the open question turns into the end-turn kind, so nothing is lost.
- Per-question default: the harness fills it only when the user presses Skip, or, if the owner turns on an optional auto-pick countdown (Roo's idea: default OFF, visible timer, paused when typing), when it expires. Default per question is the `recommended` option (single), the recommended options (multi), `default` text (free), or no answer. Never auto-answer a `confirm` question without a recommended side; leave it unanswered.
- No timeout otherwise: a waiting task is not stuck (as `_ask` today). Do not copy OpenClaw's 900 s default.

Unattended (nobody answers)
- Never block. For each question:
  1. If any `involves` names people/booking/resource, park it (`_park`, source `people|booking|resource`), tell the model "pending, go on with other work".
  2. Else pick the recommended option (single), the recommended options (multi), the `default` text (free), or the recommended side (confirm). Without any of these, the answer is "no answer: decide yourself, conservatively".
- Emit one `auto_answered` per question with `kind:"question"`, `value` (the chosen option ids), `source:"recommended"|"default"|"none"`. The model gets the same formatted text with `(chosen automatically)` tags.
- Collect these into the run's result as an "assumptions made" list (the Cron Jobs tab shows results), so the owner sees what was decided without being asked.
- This matches the stated rule: auto-answer, park only people/bookings/uncontrolled resources.

Validation and repair of malformed payloads
1. Grammar first: `strict: true` on the tool, so the arguments parse. Keep the schema within enums/min/max items and short strings.
2. Lenient normalisation in `harness/questions.py` (new, pure functions, easy to test). These are repairs, not compatibility shims:
   - strings in `options` become `{label}`;
   - a missing `kind` becomes `single` when there are options, else `free`;
   - one option only: drop it and make the question `free`;
   - duplicate labels (case-insensitive) are removed;
   - more than 5 options: keep the recommended and the first;
   - two `recommended` in a single: keep the first;
   - a label ending in "(Recommended)" has the suffix stripped and `recommended` set;
   - a label over 60 characters is cut with an ellipsis (v1 rejected it, which cost a model round trip);
   - an option named "Other"/"Altro"/"None" is removed when `allow_other` is true;
   - more than 4 questions: keep the first 4 and tell the model in the result that the rest were not asked;
   - duplicate ids are renumbered.
3. Optional heuristic: options written inside the question text ("A) ... B) ..." on separate lines) are parsed into options when `options` is empty.
4. Hard failures (no usable question text at all) return one error line with a one-line valid example. After 2 failed attempts the harness shows the question text as a plain assistant message with a free answer box. The user is never stuck on a model slip.
5. Answers are validated server-side against the stored question set (4.2). The client cannot invent options.
6. Injection stance: the model may have read untrusted pages, so option labels and descriptions are rendered as TEXT only (`textContent` / Flutter `Text`), capped in length, and control characters stripped. A question never carries a URL as a link.

Audit records
- Use the existing hash-chained ledger (`harness/audit.py`). One record when the question is shown: question ids, kinds, option counts, which option was recommended, `source: user|auto|default|skip|chat`.
- One record when answered: picked option ids per question, the length of the typed text but not the text, and the seconds it took. The text lives in the conversation history anyway, like any chat message.
- Auto-answered and parked questions are recorded the same way, so the "assumptions made" list can be rebuilt from the ledger.

5. (d) NEXT-PROMPT SUGGESTIONS
Why a separate cheap call and not "the model writes them in the same turn"
- Claude Code and Open WebUI both generate them in a separate call with the same model/task model.
- In the same turn the model must append a block to every answer. It pollutes the answer, a Qwen-class model will forget or format it differently, and free text cannot be grammar-constrained inside a normal answer.
- A separate call can be made parse-proof with `response_format json_schema`, can be skipped cheaply, and can be cancelled.

When it is generated
- After a turn ends with `stop_reason "final"`, only when all of these hold:
  - the setting is on;
  - the conversation is attended;
  - no message is queued and the engine has no waiting request, so the user's next send is never delayed;
  - the final text is not tiny (say under 30 tokens, like "Done.");
  - the answer did not end in an ask_user card (the card already has proposals);
  - the turn did not end in error, cancel or "cut";
  - the conversation is not in plan mode before the plan is accepted;
  - for a remote or cloud-routed conversation, the user's existing consent covers it (it sends the same data to the same endpoint, so reuse that consent; if unsure, skip).
- Generate it after the answer is shown. The user is reading, so the delay is hidden.
- Cancel the call immediately when the user starts a new send.

The call
- Same client and model as the conversation. Messages are the conversation history plus one trailing instruction message that is NOT stored in the history. This keeps the prompt cache prefix; only the instruction (about 150 tokens) and the output (about 60 tokens) are new work. I give no timings: none measured. To be measured on the 5090 and the 4090 laptop, labelled with the power limit.
- Thinking off (if the server supports a per-request switch; verify), `max_tokens` about 160, low temperature, a hard timeout of 8 s. On timeout, error or an empty list show nothing and log one terse line.
- Grammar schema:
```
{"suggestions":[{"label": string(<=40), "prompt": string(<=240)}]}   // minItems 2, maxItems 4
```
If `label` is missing, repair it from the first 40 characters of `prompt`. Drop duplicates and near-duplicates (the same text after lower-casing and stripping punctuation).
- Instruction text:
  "Write 3 messages the USER might send next, in the user's language, each in the user's voice (a request or a question, never a thanks and never a question for the user). Make them different in kind: go deeper, apply it, check or compare. Use real names from the answer. If nothing sensible follows, return an empty list. Output only JSON."

How shown
- Chips under the last assistant answer: a `role="group"` with an `aria-label`, buttons of at least 30 px (44 px on the phone), wrapping to the width. Text from `label`, with the full `prompt` as `title` and `aria-label`.
- One click puts `prompt` in the input box, focuses it, and does NOT send. The user edits or presses Send. This is the opposite of Roo's default and of Open WebUI's default, as the owner asked, and it is also the safest choice (see privacy).
- Optional extras: Tab in an empty box takes the first suggestion (Claude Code, Open WebUI). Digits Alt+1..4 pick a chip. Do not make Tab the only way in, because discoverability is a known problem: the chip is the primary UI.
- Chips disappear when the user sends or starts typing something unrelated. Persist the last set in the session record so a reload shows them, and drop them at the next turn.
- Show nothing, with no empty placeholder, when there are none.

Cost control
- Skip when the cache is cold (a long idle gap), when the engine is busy, on very short chats of the first turn if the owner wants that, after the answer was an error.
- Back-off like Claude Code: after 3 suggestion sets in a row where none was used, generate every second turn, and reset when one is used.
- Per-session and global toggle in Settings, default ON in GUI Chat and Agent tab; OFF for unattended runs, headless, and ACP.
- Count tokens used by suggestions separately in the usage stats so the owner can see the cost.

Privacy and safety
- The call goes to the same local model, so nothing new leaves the machine. For a cloud-routed conversation see "when it is generated".
- Suggestions never enter the model's history and are not written to long-term memories.
- Audit: record count, milliseconds, tokens, and which index was used; not the texts.
- Suggestions can be influenced by what the agent read on the web (the prompt-injection priority in memory). Controls: they are plain text, capped at 240 characters, and are only put in the box; the user sees them and presses Send. Never auto-send. Never pass a suggestion to a tool.
- Do not generate from conversations marked incognito or whose content the user excluded.

6. (e) UI DESIGN
6.1 GUI chat (WebView2, 1024 to ~3600 px; shared by Chat and Agent tabs through agentchat.js)
One new ask block kind, `b.kind === "questions"`, drawn like the MCP elicitation form (same class family `.aask*`, same pattern: build once and do not redraw while the person types).
- Card: `.aask`, max-width about 720 px so it stays readable on 3600 px screens, full width below that. Optional intro line.
- For each question a `<fieldset>`: a `<legend>` with a small header chip (the first words of the question or its id as fallback) and the question text. Show "n of m" only when m > 1.
- Single: a vertical list of option rows with `role="radiogroup"` and `role="radio"` (`aria-checked`, roving tabindex, arrow keys move and select, Space selects). Each row is a full-width button: bold label, description underneath in `.aask-h`, a "Recommended" text badge on the recommended one (text plus an icon, not colour alone). Rows are at least 36 px tall (spec says 30 px minimum), 8 px apart.
- Multi: the same rows as real checkboxes (`<input type=checkbox>` inside a label); Space toggles. Do not advance on one click.
- "Other" row last (unless `allow_other` is false): a radio/checkbox row labelled with the translated "Other"; selecting it reveals a text input (min-height 44px as `.aask-b input`) and moves focus to it. Typing into the input selects the row by itself.
- Free: a textarea (or an input if the placeholder is short) with the placeholder; Enter submits only when it is the sole question, Shift+Enter is a newline.
- Confirm: two buttons (Yes / No) with the recommended side primary.
- Footer: primary "Send answers" (disabled until every required question is valid, with an inline `aria-live="polite"` note naming the first missing one), a secondary "Skip", and "Keep chatting about this": it sets the card aside without answering (nothing is sent), puts the questions quoted (`> ...`) in the message box if it is empty and the focus there, and leaves a "Reopen the questions" button that brings the card back with the choices kept; writing a normal message closes it as before.
- Fast path kept from today: when the card holds exactly ONE question of kind single or confirm, a click on an option submits immediately (that is how v1 buttons behave). With more than one question or any multi, Send is required.
- Several questions are tabs (`role="tablist"`, one `tabpanel` shown, 44 px tabs that wrap, a check mark and a spoken "answered" on the done ones). Keyboard: Left and Right move between the questions (also from an answer, landing on its first answer), Home and End on a tab; Up and Down move the focus between the answers without choosing; Enter picks (single, which then moves to the next question) or toggles (multi); on a card of one single question Enter answers at once. In a text field the arrows stay the caret's.
- Keyboard: Tab moves between controls. Digits 1..5 select an option of the focused group when focus is not in a text field. Ctrl+Enter submits from anywhere in the card. Esc does nothing destructive (it does not skip). Focus goes to the first question when the card appears if the chat input is empty; it does not steal focus when the user is typing (check `document.activeElement`).
- Layout at 1024 px: one column, text wraps (`overflow-wrap:anywhere`), no horizontal scroll. At 2560 px or more the card keeps its 720 px width, left-aligned with the assistant column.
- After answering: the card collapses to a read-only summary ("You answered: ...", the existing `ch.answered` style), picked rows marked with the existing `.picked` outline plus a check icon; auto-picked answers say "chosen automatically". Parked questions use the existing pending path.
- Hidden ask_user tool cards stay hidden (lines 356, 364, 411).
- Suggestion chips: `.asuggest` row under the last assistant block, class `.btn`, group `aria-label` = `ch.suggest.label`.
- i18n keys, each in EN and IT in `serve/web/i18n.js` (the static test checks placeholders and that EN differs from IT): `ch.ask.send`, `ch.ask.skip`, `ch.ask.chat`, `ch.ask.other`, `ch.ask.other.ph`, `ch.ask.recommended`, `ch.ask.required`, `ch.ask.missing`, `ch.ask.auto`, `ch.ask.default`, `ch.ask.answered`, `ch.ask.skipped`, `ch.ask.yes`, `ch.ask.no`, `ch.ask.n_of_m`, `ch.suggest.label`, `ch.suggest.title`, `ch.suggest.off`, plus settings labels and an `h.` help line for the toggle. The `ch.q.` list at `test_web_static.py:40` is for the harness's own question kinds, so it needs the new kind added only if it uses `askQuestion()`.

6.2 Agent tab
- Same widget (shared file). Extras: plan mode asks, then presents the plan (`present_plan` is unchanged). Worktree and permission cards stay distinct from question cards by their kind and by an icon in the legend.
- Terminal and ACP (optional, phase 3): print numbered options and read a number, comma list, or text; in ACP, send an `elicitation/create` form when the client advertises `elicitation.form`, otherwise a permission card with the choices plus Skip (Hermes' `acp_adapter/elicitation.py` is a model; no code copied).

6.3 Android app (Flutter, `mobile/lib/src/`)
- `chat_model.dart`: add `questions` (list of maps) to `Block`, parse it in the `ask` case (line 286-294), and an `answerQuestions()` that posts `{'ask': id, 'value': 'ask:' + json}` to the existing `/answer` endpoint.
- `chat_screen.dart`: a new `_QuestionsCard` StatefulWidget replacing the branch in `_ask` for `kind == 'questions'`, as `_CodeCard` is done:
  - single: `RadioListTile` per option;
  - multi: `CheckboxListTile`;
  - rows at least 48 dp, description as `subtitle`, "Recommended" as a `Chip` with text;
  - "Other" row with a `TextField` (the existing voice input can fill it);
  - bottom `FilledButton` "Send" plus `TextButton` "Skip";
  - safe-area padding; text scales with the system font.
- Suggestions: a horizontal-wrap of `ActionChip`s (48 dp targets) above the composer; a tap sets the controller text and focuses, never sends.
- Strings go in `i18n.dart` as [it, en] pairs.
- The parked-questions screen (`jobs_screen.dart`) already lists pending options; point it at the same card.
- The mobile app shows the same server events, so it needs no new endpoint.

7. FILES TO CHANGE
Harness
- `harness/questions.py` (new, pure, no I/O): normalise/repair a payload, assign ids, validate an answer, format the message for the model, pick auto answers. About 200 lines.
- `harness/planner_tools.py`: replace the body of `register_ask_tool` (the signature stays); schema as 4.1 with `strict: true`; the ask budget and repeat guard; remove the old `MAX_OPTIONS`/`LABEL_CHARS` constants.
- `harness/session.py`: `_question` (attended: emit the ask event with the normalised questions and `facts.involves`; unattended: auto-pick or park as 4.6); a validator for the `ask:` kind in `answer()`/`_answer_parked`; phase 2: route through `_ask`.
- `harness/session.py` module docstring (line 29-30) says ask_user "does not end the turn... nobody can answer"; update it. Also the "ask_user" paragraph in `docs/harness.md` (line 226) and `docs/harness-manual*.md` (EN and IT).
- System prompt text: wherever the tool prompts are assembled with the planner tools (`harness/planner_tools.py:159` mentions `ask_user`); keep it in one place.
- `harness/audit.py` callers: the two audit records of 4.6.
- Suggestions: `harness/suggestions.py` (new): build the instruction, call the client with `response_format`, repair and filter; triggered from `Session` after a `final` done event, emitting `{"event":"suggestions","turn":n,"items":[{label,prompt}]}`. Reuse `harness/client.py` `response_format` (line 168-186).

Server
- `serve/agent_sessions.py`: add `questions` and `intro` to `ASK_FIELDS`; per-kind answer limits (replace the two `+ 500` checks at 1399 and 1414); for the end-turn MVP, an `/answer` branch for `rec["question"]` asks that formats the answer with `harness/questions.py` and sends it as the next user message (so both clients post JSON); keep `rec["suggestions"]` for reload.
- A setting for suggestions on/off (per session in the create body and a global default).

GUI
- `serve/web/agentchat.js`: the `questions` ask block draw function (model on lines 541-601), the `suggestions` event, `answer()` branch, chips, `setPrompt`.
- `serve/web/app.css`: `.aask-opt`, `.aask-badge`, `.asuggest` (next to `.aask*` at 953-966).
- `serve/web/i18n.js`: EN and IT keys listed in 6.1.
- `serve/web/chat.js`: nothing new except that `window.chatUi.setPrompt` already exists (line 636) and is the hook for chips.

Mobile
- `mobile/lib/src/chat_model.dart`, `chat_screen.dart`, `i18n.dart`, and a small test in `mobile/test`.

8. TESTS (CPU only, in the existing styles)
- `tests/test_questions.py` (new): normalisation and repair table-style cases (each repair in 4.6); answer validation (unknown id, two picks on a single, empty required, overlong text, control characters); message formatting (all four outcomes, labels never ids, "(chosen automatically)"); auto-pick policy (recommended, default text, confirm without recommended gives no answer, `involves` parks).
- `tests/test_planner_tools.py`: replace the v1 case at line 237-248; keep the "Nobody can answer" test, changed to the new unattended policy.
- `tests/test_session.py`: replace the case at line 495 and 870: attended question ends the turn, the structured answer formats into the next user message (model not called before); unattended auto-picks and emits `auto_answered` per question; a parked `involves` question appears in `pending`; ask budget (third call refused); repeat guard.
- `tests/test_suggestions.py` (new): scripted client returning valid, malformed, duplicate and empty JSON; skip rules (queued message, tiny answer, ask card, unattended); the instruction message is not in history; cancel when a new send arrives.
- `serve/tests/test_agent_sessions.py` and `serve/tests/session_stub.py`: the `ask:` answer accepted and rejected cases; limit per kind.
- `serve/tests/test_web_static.py`: new keys exist in EN and IT with the same placeholders; the draw function and `send`/`answer` markers exist.
- `serve/tests/slash_check.js` style: a node check that the draw function builds one fieldset per question and sets `aria-checked` (if the repo's JS checks run under node).
- Mobile: a widget test for `_QuestionsCard` (single, multi, Other, Send disabled until valid).
- Real-model behaviour (does Qwen ask at the right times, are suggestions sensible) is NOT CPU-testable: add it to `tests/QUEUE_daytime.md`, and add the ask/no-ask set to `harness.eval`.

9. MINIMUM VIABLE VERSION, SIZE, RISKS
MVP (about 3 to 4 days for a focused session, my estimate):
- v2 schema with kinds single, multi, free, confirm, `recommended`, `allow_other`, `required`; no `depends_on` in the prompt.
- End-turn semantic kept; structured answer via `/answer`, formatted server-side.
- Unattended: auto-pick the recommended option and park `involves`.
- GUI card with keyboard support and EN+IT strings; no Flutter yet (it keeps the old button rendering, which still works if `options` is also provided for a single-question call - decide whether to keep that fallback or ship Flutter in the same change; I recommend shipping Flutter together, about 1 more day, because the phone shares the same sessions and an unrenderable card would block it).
- Tests as in section 8.
- Estimated size: `questions.py` ~200 lines, `planner_tools.py`/`session.py`/`agent_sessions.py` ~120 lines changed, `agentchat.js` ~200, CSS ~40, i18n ~40 keys x 2, tests ~400.
Phase 2 (1 to 2 days): suggestions chips (`suggestions.py`, event, chips, toggle, tests), then blocking mode, then Flutter polish.
Phase 3: ACP/terminal rendering, optional ambiguity-check call, `depends_on` in the UI.

Risks
- Qwen-class behaviour: it may ask too much, too little, or put options in the text. Mitigation: the ask budget, the repeat guard, strict grammar, lenient repair, and the eval set. Unknown until measured.
- Auto-picking the model's own "recommended" in unattended runs is the model deciding for itself, now visible. Restrict it to low-stakes: parking via `involves` depends on the model setting it, so a model that forgets it can auto-answer something that really belongs to a person. Mitigation: the existing `question_source` also reads the text for the phone/booking cases only partly; keep payments and calls behind their own permission questions (they already park), and list every auto answer in the run result.
- Changing the end-turn behaviour to blocking (phase 2) interacts with process restarts and `rec["ask"]`/`rec["question"]` states; test restart and the "dormant" state before enabling.
- Prefix-cache cost of the suggestion call if the cache has been evicted (conversation-cache budget): skip when cold; measure.
- Suggestion call competing with the next request on a single GPU: gate on idle engine and cancel on send.
- A new answer-length limit touches `agent_sessions.answer` validation, which also guards codes and MCP elicitation; keep the limits per kind so the old kinds keep their 500/32 limits.
- I did not check how the engine's grammar converter handles `maxLength`, `pattern` or `oneOf`; Hermes notes llama.cpp's converter rejected maxLength of 2000 or more. Use small bounds and test `strict:true` on the real server.
- Docs: `docs/harness.md` is 1368 lines (over the 300-line rule already) so the new text should go in a new short file, not into it, and `docs/precision/` is unaffected (no numerics change).

