# Awareness and continuous improvement: study

What the person asked (1 November): the agent knows what it is and what it can do, what its purpose and its target are, and how far it has got; it checks results
against the target and goes on alone with persistence; it knows its limits (the context length, knowing it can be compressed); it analyses what it did wrong and what
it did right and how to fix it; all of that is kept in a reusable file and drives later work (continuous improvement); it acts before a task fails or is dropped; and
it proposes what to do to the person with the scrollable question cards (arrow keys, the possible answers plus a free one, or keep chatting).

The two features are one loop: **know** (self-model, mission) -> **act and watch** (progress, course checks) -> **record** (outcomes, lessons) -> **know better next time**.
The rule that runs through it: whatever can be code is code (events, counters, files, thresholds); the model is asked only to write the few sentences that need language.

## What exists today

- **Identity** is one short prompt (`SYSTEM_CORE` in `tadamoo/stack.py`). The model is not told its window, that old tool results are masked and the conversation folded,
  its budget, or what it cannot do. `/status`, `/tools`, `/context` show these to the person only; the model cannot call them.
- **Target and progress**: the Target Loop (`tadamoo/target.py`) splits a text into criteria and tasks, judges them with cited evidence and goes on until the criteria hold
  or it stalls. It is the real "persist until done" and it has to be started. A plain turn has no goal state; the gate (`tadamoo/gate.py`) runs only commands the person
  listed; `tadamoo/progress.py` writes a line for the person and never gives it back to the model.
- **Stops and warnings**: loop guard, repeat guard, error classifier, budget warning at 90% with a last report. All are reactive, and all end in a note or a stop.
- **Memory**: project notes (`remember`/`recall`, only when the model chooses), the person's own memories, the index of past conversations, routine outcomes (per routine),
  compaction summaries that keep "decisions and why" and "lessons" inside one conversation. The audit ledger records everything and nothing reads it back.
- **Questions**: `ask_user` cards (`serve/web/askcard.js`): single, multi, free, confirm; recommended option; number keys. There is no left/right move between questions.
- No experiment measures self-knowledge, reuse of lessons or proactive intervention.

## Gaps against the request

1. The model does not know its own limits (window, compaction, budget), what it can do here, or where it stands, unless a marker appears after the fact.
2. Outside the Target Loop nothing keeps the goal, the criteria and "done so far" as a state that survives compaction and is compared with the request.
3. Nothing writes down what failed, why, and what fixed it; nothing reads such a file at the start of the next task or project.
4. Nothing acts before the failure: a drifting plan, two failed gate rounds, rising errors and a context about to fold only show up as a stop or an error.
5. When the agent is about to give up it reports; it does not offer options in a card.

## Proposal

### 1. Self-model (code, no model call)

- A **"What you are" block** built by code at the start of a session and pinned in the prefix (it does not change within a session, so the cache holds): the model
  name, window and thresholds, "old tool results are replaced by a marker with the saved path; the conversation is folded into a record whose originals are on disk and
  can be read again", budgets (steps, tokens, time), the role and permission level, the tool groups, the tools that are missing and why (no vault, no browser...), and where
  lessons are kept.
- A **`self_status` tool** (pure, always allowed): live numbers, so the model can look instead of guess. Context filled and the next thresholds, steps and tokens used and left,
  compactions so far, loop-guard strikes, gate rounds, the mission card (below), the lessons found for this task.
- Before a fold the harness already runs `pre_compact` hooks. Add a deterministic step there: the mission card and the decisions made are written to disk first, so a fold
  cannot lose the purpose.

### 2. Mission card (persistence)

- At intake, code makes a **mission card**: the request as written, the criteria the existing card/intake already extract (words, format, language, files to produce), the
  checklist (`todo_write`), and a `done so far` list updated from tool results (files written, checks passed). It is pinned like the project rules and survives compaction.
- **Checking progress** is the existing machinery generalised: before a final answer the gate runs the checks it can infer (tests, build, lint found in the project, plus the
  person's own list), and the card's criteria are compared with the state. Not met -> the harness says which ones and why, and the turn goes on (bounded by the stall rules of
  the Target Loop). Whether this is always on in the agent mode or a "work until done" switch is a decision (below).
- Autonomy stays bounded: the same stall detection, budgets and the person's stop.

### 3. Outcomes and lessons (the persistent file)

- **Outcome record** after every task, by code: result (done, stalled, abandoned, stopped, failed), steps, tokens, time, errors by tool, retries, loop strikes, gate rounds,
  compactions, whether the person corrected the agent. One JSON line in `outcomes.jsonl`.
- **Lessons** in a readable Markdown file the person can open and edit, at two levels: `<project>/.basalt/lessons.md` (shareable with the project, can be git-ignored) and a
  global file in the user data folder for lessons that hold everywhere. Each entry: when, the situation in a few words, what went wrong or worked, the fix, evidence (audit ids),
  how many times it was confirmed, scope (this project / everywhere).
- **Captured by code** from events that already exist, not by asking the model to remember:
  - a tool error followed by success with other arguments (an error -> fix pair);
  - a gate failure that a later edit cleared;
  - a loop-guard strike and what ended it;
  - a permission refusal and the way around it;
  - the person's correction after an answer (words first, the technical session when unsure);
  - a budget that ran out, a fold that lost something the task needed (a re-read of the originals right after).
  The technical session then writes the entry's two or three sentences from those facts in one small call at the end of the task (as the progress line and the
  memory extractor already do). Without the session the entry is stored with the facts only.
- **Reuse**: at the start of a task the lessons that match the request (word match with the same FTS5 machinery as `memory.py`, project first, then global) go into a short
  pinned block "from your earlier work in this project". They are advice, marked as data from the past, never instructions; entries are screened with the same phrase list and
  second reader as outside text, so a poisoned page cannot become a lesson that steers later runs. The person sees them in a Lessons view and can edit, pin or delete; a
  lesson confirmed many times is offered for promotion into `AGENTS.md`.
- **Toward code**: an outcome record that shows the same long sequence of steps again and again is the signal to offer "make it a script" (the routine feature already does this).

### 4. Proactivity

- A **course watcher** in the loop, deterministic, per step. Signals: the same error class twice, a loop-guard note, a gate failing twice, the todo list not moving while the
  budget passes half, context above the masking threshold, silence of the tools.
- Reaction in two stages: first a precise "[harness] course check" note to the model with the facts and the matching lessons, asking it to change approach, ask, or go on with a
  reason; if the signal persists, a **card to the person** (below). Where nobody watches, the existing unattended rules answer from the recommended option and record it.
- Context pressure: at the masking threshold the model is told once that a fold is near and to put into the mission card the decisions that must not be lost.
- After a task the suggestion chips become a card of next steps built from the mission card and the lessons.

### 5. Option cards (Claude Code style)

- A card is produced by code from templates for the moments that need it (about to give up, verify failed, loop strikes, budget grace, direction proposals): 2-4 options built
  from the lessons and the state (try approach X, narrow the scope, stop and report), a recommended one, a free answer, and "keep chatting about this".
- The card UI gets the missing keys: left/right moves between the questions of a card, up/down between the answers, Enter confirms, as in Claude Code.

### 6. Measure before turning anything on by default

Each needs its win rule written before the run, k >= 5, power limit stated:
- **E10 lessons**: the same 8 tasks run twice; the second time with the lessons of the first. Win: fewer steps or fewer failures by a margin, no new failures.
- **E11 awareness under pressure**: a long task that forces folds, with and without the self-model, the mission card and the pre-fold save. Win: more of the early rules and
  facts obeyed after the folds, no loss in success.
- **E12 proactivity**: tasks built to derail (a wrong path, a flaky check), with and without the course watcher. Win: more completed or cleanly abandoned, fewer wasted steps.

### Order

1. Self-model block, `self_status`, outcome records, lessons store with capture by code and the Lessons view (no model needed; testable now).
2. Retrieval and injection of lessons, the technical-session write-up, the screening against poisoned lessons.
3. Mission card, pre-fold save, the progress check before the final answer.
4. Course watcher and the option cards, with the arrow keys.
5. E10-E12 on the GPU.

## Risks

- A bad lesson steers later work: kept readable and editable, screened, marked as advice, with counts so an unconfirmed one weighs little.
- Persistence can burn budget on a task that cannot be done: the stall rules, budgets and the person's stop stay in force, and abandoning is a valid outcome that is recorded.
- More text in the prefix: the self-model and mission blocks are short and fixed per session; lessons are capped by tokens like the memory notes.
