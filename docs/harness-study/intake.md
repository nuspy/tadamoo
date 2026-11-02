# Prompt intake: what runs between the user's text and the first tool call

Survey, evidence and a design for our harness. Sources: the cloned harnesses, `docs/harness.md`, the Anthropic, Claude Code,
Cursor, Aider, Kiro, Spec Kit, Deep Research and Manus material, and papers; every threshold below is a starting value to tune,
not a measurement. Built as described in [../intake.md](../intake.md) (phase 1, and the registry part of phase 2). Verified gaps of the study: Devin and Windsurf docs, the exact Claude Code question limits, the
engine's grammar subset (`maxItems`, `maxLength`) and how a second request affects our prefix-cache checkpoints.

## 1. What other systems do

No surveyed system runs an automatic model call before the first tool call. Hermes, OpenClaw and Goose leave everything to the
loop (Hermes: act on the obvious interpretation, ask only what tools cannot retrieve, headless runs never ask; Goose removed
its planner). Roo and Claude Code and Cursor make planning a mode the user picks (Plan mode: read-only exploration, clarifying
questions, an editable plan, approval). Roo's "Enhance prompt" is an opt-in button with undo. Aider alternates `/ask` and
`/code`; its architect/editor pair (two calls) gains a few points, most for the weakest single model. Kiro and Spec Kit are
spec-driven (requirements, design, tasks); Spec Kit's `clarify` asks at most 5 questions, one at a time, 2-5 options with a
recommended first, and skips a question whose answer would not change implementation. OpenAI Deep Research clarifies and rewrites
first because its research model cannot ask. Anthropic: start simple, add stages only when they demonstrably help; chaining for
fixed subtasks, routing needs reliable classification, orchestrator-workers for unpredictable subtasks. Cognition and Anthropic
both warn against splitting coupled coding work. Manus: append-only context, stable prefix, a todo list restated at the end.

## 2. What helps, what is ceremony

Helps: deterministic checks before any call (our `pipeline.extract_card`); asking when a request is underspecified AND a wrong
guess is costly (Ambig-SWE: up to 74% gain, but models detect underspecification poorly; question-efficiency papers get 1.5-2.7x
fewer questions with value-of-information scoring), so gate "ask" by structured fields (findable by tools, reversible, cost),
not one "ambiguous?" label; putting all requirements in one place (multi-turn conversations lose about 39% on average);
as-needed decomposition (ADaPT: split when the executor fails); a visible checklist restated near the end; a verification
command per task; cache-friendly append-only staging.

Ceremony or negative: an LLM triage on every message, automatic rewriting that replaces the user's text, spec documents for small
tasks, parallel sub-tasks on one GPU (the gain is speed), multi-agent splitting of coupled code, asking by default (clarification
states raised prompt-injection success in one study: build questions from harness data, never from tool or web text). Format
restriction can hurt reasoning, so the triage is classification and extraction with short quotes, grounding field first.

## 3. Design

Each stage is free until proven necessary; most messages skip the model call (hypothesis 60-80%, to measure).

- **S0 Capture.** The message is stored verbatim (`Session._add_message` keeps `raw` beside `text`).
- **S1 Hygiene.** The model-visible text gets lossless cleaning only: control characters except newline and tab, zero-width and
  bidi characters, ANSI escapes, CRLF to LF, trailing spaces, runs of blank lines, Unicode NFC. A separate NFKC, case-folded
  `probe` copy is used only by rules and the triage input. Never: spell correction, translation, summarising, reordering,
  paraphrasing; fenced code, paths, URLs, identifiers and numbers are protected.
- **S2 Signals.** The existing `extract_card` plus: word and sentence counts, enumeration markers, action-verb lexicon (IT and
  EN), deictic words without antecedent, attachments, share of pasted code or logs, URLs, named paths (check they exist), a risk
  lexicon (delete, send, pay, book, call), language, whether the last assistant turn ended on an intake question. Relative dates
  are annotated from the local time line as assumptions, never substituted.
- **S3 Gate.** SKIP when the origin is not the person, a steer, a skill run, a slash command, an answer to an open intake
  question (merge it into the ledger), a continuation phrase, a short conversational message without action verb or risk word, or
  a long warm-cache history. TRIAGE in agent mode at 35+ words, 2+ enumeration markers, an action verb with an unresolved deictic
  word, a risk word, or attachments with an imperative; in ask-only Chat the bar is 60+ words, 3+ enumerations, or a card with
  `min_words` of 1000+.
- **S4 Triage call.** One grammar-constrained JSON call (`response_format` json_schema, `strict: true`, temperature 0,
  `reasoning_effort: none`, about 450 output tokens, no tools). Input: the `probe` capped at 3000 characters plus a three-line
  facts block (attended or unattended, workspace, attachment names); never tool results, memories or web text. The static
  system part is byte-identical on every call.
- **S5 Validation.** Parse (invalid JSON means skipped, no retry). Grounding: every `evidence`, `stated_constraints` and
  `subtasks[].span` string must occur in the person's text after whitespace normalisation; non-matching items are dropped; more
  than half ungrounded discards the whole result. Caps: 4 missing_info, 6 subtasks, `depends_on` only to lower indices.
- **S6 Policy** (code, not prompt; section 3.2). **S7 Clarify** (one round, one card; questions built by the harness from
  `missing_info[].what` plus its default, never free model prose; the default first and recommended, a "go with the defaults"
  choice, free text; the answer skips triage). **S8 Intake note** (deterministic template, `note: True`, appended after the
  user's message at the tail so the cache prefix is untouched; labelled "not the user's words"; assumptions, constraints in the
  user's own words, plan only when large; the UI shows editable chips and an edit is a steer). **S9-S12** decompose, execute with
  focus prompts, verify with the existing `Pipeline.verify`, re-plan.

### 3.1 Triage schema (key order matters: evidence first)

`{evidence[<=3, <=160 chars], intent enum(answer, explain, search, edit_code, create, analyze, operate, plan, chat, command),
size enum(trivial, small, medium, large), ambiguity enum(none, minor, material), missing_info[<=4]{what, why, default,
findable enum(no, files, web, memory, tools), wrong_guess_cost enum(low, medium, high), reversible bool, owner enum(user,
other_person, booking, resource, none)}, risks[<=4] enum(destructive, external_effect, money, other_people, secrets, long_running,
needs_login, private_data), needs_plan bool, stated_constraints[<=5, <=160], subtasks[<=6]{title<=80, span<=200, depends_on[int],
acceptance<=120, fresh_context bool}}`, all objects `additionalProperties: false`, all keys required. The subtask shape matches the
planner's `NODE_SCHEMA`, so a later `subtasks_to_diff()` is a field copy; `owner` maps to `question_source` in `session.py`. If
the engine rejects `maxLength` or `maxItems`, drop them and truncate in S5.

### 3.2 Triage prompt (about 330 tokens, free-text fields in the user's language)

You read one message a user wrote to an assistant that can use tools. Do not answer it and do not do the task; fill the form;
copy quotes exactly, never paraphrase inside evidence, stated_constraints or span. `size`: trivial = one answer or one small
action; small = a few steps; medium = several files or sources; large = three or more separable deliverables the user lists or
clearly implies. `ambiguity` material only if two reasonable readings give different results AND the wrong one costs real work;
when in doubt, none. `missing_info`: only facts needed to start that the message does not give; not what files, search or
memory can supply (mark those `findable`); `default` is the assumption you would use; `wrong_guess_cost` high only when hard to
undo or involving money, other people or sending things out. `needs_plan` true only if the user should approve steps before
anything is changed. `subtasks` empty unless size is large, each pointing at the user's words in `span`, ordered so none depends
on a later one. Keep the block byte-stable; add two short examples only if the eval shows weak labelling.

### 3.3 Ask-or-act policy

Drop `missing_info` whose `findable` is not "no": they become "resolve by reading before asking" lines in the note (the agent may
still call `ask_user` itself). Score the rest: `risk = P(ambiguity) x C(cost) x (2 if not reversible or owner is not none)`
(ambiguity none 0.05, minor 0.25, material 0.5; cost low 1, medium 4, high 15; starting values). Ask only items whose risk exceeds
the cost of asking: attended Chat 3, attended Agent before the first side effect 2, after work has started 6, auto or unattended
infinite. Everything else is a labelled assumption.

- Attended Chat: default to act; at most 1 question (2 only if both are high cost); tool-less Q&A is never asked about.
- Attended Agent: up to 3, one card, before the first side effect; a large plan below the auto permission level goes through the
  existing `present_plan` gate.
- Auto permission mode: act on assumptions and report them; the named exceptions (other_person, booking, resource) are still
  parked or asked.
- Unattended: never ask, never block; defaults are recorded as assumptions; a named exception uses the existing `_park`, dependants
  are `blocked` in the journal, independents proceed; if nothing is left, finish with a report of what is needed and a distinct
  stop reason.

### 3.4 Decomposition and re-planning

Split only when the triage says `large` and the sub-tasks are grounded: three or more deliverables listed or clearly implied,
independent areas, a long-answer request, a task expected to exceed the context. Tightly coupled work is one task with a
checklist; medium and below run as one task and split only on failure (ADaPT). Layout: sequential turns in the same conversation
(sub-task k appended as a harness note plus a short instruction at the tail); `fresh_context: true` runs through a fresh subagent
loop. Focus prompt per sub-task, from a deterministic template: the original request verbatim, this sub-task (title and span),
global constraints (card plus `stated_constraints`, always), acceptance, only the handoff records of `depends_on`, boundaries
("the other sub-tasks are handled separately"), completion ("end with the handoff record"). Handoff record of about 150 tokens
built mostly by the harness (outcome, artifact paths, decisions, open problems, evidence), checked where verifiable (files exist,
acceptance command passes): a child's summary is a self-report.

Registry mapping: parent task `req-<turn>`, children `req-<turn>.1` ... in `children`; `status` pending, running, blocked or
closed; `result` the trimmed handoff; `metadata` carries `kind: "intake"`, title, span, `depends_on`, acceptance, budget,
assumptions; no journal schema change (`depends_on` is enforced by the intake runner). `serve/agent_runs.py` already records one
task per run; children attach under that id. Durable, dated or multi-person plans stay in the planner with its own consent; "make
it a plan" converts sub-tasks into an `add_nodes` diff through the normal approval path.

Re-plan on a deterministic trigger (a sub-task ends with a stop reason other than FINAL; the final fails verification twice; two
loop strikes; a blocked report; a steer adds an uncovered requirement; a contradicted assumption): one grammar-constrained call
returns only the remaining sub-tasks (pattern in `planner/replan.py`), at most 2 per request.

### 3.5 Budgets

One triage call per request (target under 5% of a median run's wall time); one clarification round (2 questions in Chat, 3 in
Agent); at most 6 sub-tasks with `max_turns` by size (trivial 3, small 8, medium 20, large 12 per sub-task) inside the existing
`max_total_tokens` and `max_seconds`; 2 re-plans. `--intake off|rules|full` per conversation and environment; `rules` runs S0-S3,
S6, S8 without the model call and is the safe default until measured.

## 4. Where it plugs in

New `harness/intake.py` (about 400 lines: `clean`, `signals`, `gate`, `SCHEMA`, `PROMPT`, `triage`, `validate`, `policy`, `note`);
phase 2 `harness/subtasks.py` (about 500 lines). Hook in `Session._run_task` just before the `agent.run(None)` loop (worker
thread; not `_begin_main`, which is on the request path, and not `AgentLoop`). Question events reuse the `ask` event and
`_policy`/`_park`, with `kind: "intake"` in `question_source`. `pipeline.py` accepts up to 3 extra qualitative constraints from
`stated_constraints`. `stack.py` builds `Intake` beside `Pipeline` and `Router`. The assumption chips and the question card are
the UI of the `ask_user` v2 work ([ask-user-v2.md](ask-user-v2.md)). New short doc `docs/harness-intake.md`. `BasaltClient.chat_completion`
already takes `response_format` and `extra`; `note: True` messages are stripped from the wire format and ignored when grouping
turns; plan-change quotes must come from the person's latest message, so the note cannot stand for their confirmation.

## 5. Minimum viable version and measurements

Phase 1 (2-3 days): hygiene, signals, gate, one triage call, validation, policy, the note, one attended clarification card;
unattended defaults plus ledger. About 450 lines of code, 450 of tests, 80 of web. Phase 2 (4-5 days): sequential sub-tasks,
handoffs, journal mapping, re-plan. Phase 3 only if the eval justifies it: fresh-context sub-runs, planner diffs, decomposition on
failure.

**Measure the cache effect first; it can veto the design.** `docs/running.md` says each slot keeps two checkpoints (end of the
system prompt, last turn). If a hybrid-state model cannot truncate a checkpoint (inference), a triage call with a different prompt
may replace the last-turn checkpoint and force the next main turn to re-read the whole history. Probe: main, triage, main against
main, main, main at 1k, 8k and 30k tokens, comparing `cached_tokens` and time to first token (`harness/eval/alternate.py` as a
base). If the loss is large, gate triage to short histories or a cold cache and use `rules` elsewhere.

CPU tests (scripted mock engine): cleaning (protected spans unchanged, original untouched); a labelled corpus of about 60 IT and
EN prompts for gate recall and skip rate; validation (ungrounded quotes, cycles, caps, invalid JSON); the policy table for every
mode (unattended never asks, named owners park); the note is deterministic, `note: True`, and not accepted as a plan-change quote;
session integration (an ask event ends the turn, the answer skips triage, unattended produces `auto_answered` or `pending`); journal
children and blocked dependants; the triage payload (strict schema, no tools, byte-stable system part).

Real-model set `intake` in `tests/QUEUE_daytime.md`: `--intake off|rules|full`, k=5, an A/A pair for the noise floor, every number
labelled with the GPU power limit. Fully specified tasks (about 10: over-ask rate must be 0), underspecified tasks with a hidden
fact (about 10: success, question precision, blocker recall, number of questions), multi-deliverable tasks (about 8: pass^k, tokens,
seconds, sub-task count, constraint retention), findable-by-tools tasks (must not ask), triage quality (JSON validity, ungrounded
rate, agreement with hand labels). Ship `full` only if success rises by more than the noise floor (about 3 points) within the 5%
time budget; otherwise keep `rules`.

## 6. Pitfalls

Over-asking (cost rule, one round, `findable` exclusion, recommended defaults, a "go with the defaults" choice, over-ask rate as a
first-class metric); prompt drift (never replace the text, quote-anchored fields, the original always in context, constraints
re-checked at the end by the card, the triage output never fed to another triage); losing constraints (every sub-task prompt
carries the card and `stated_constraints` verbatim, a steer merges into the same ledger, drop-one-constraint mutation in the eval);
double cost on one GPU (one triage call, deterministic everything else, no up-front plan below `large`, judge calls capped at 3);
cache invalidation (separate short triage prompt, no change to the system prompt or tool list between turns, the note at the tail,
append-only sub-tasks, mind the plan-mode `present_plan` re-registration); prompt injection (intake input is the person's text and
harness facts only; questions come from harness data); a wrong "large" splitting coupled work (allow "do the rest together");
unattended deadlock (never wait; park only named exceptions; blocked dependants visible); an answer treated as a new task (bind
the reply to the open question).
