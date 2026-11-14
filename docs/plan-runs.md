# Plan runs: parameters, requests to the person, taking up again, recovery

## Parameters: the values a plan starts with

A plan's **initial values** are never asked when it starts. Each is a **parameter** (`parameter` node): a plan value with a default, set by the person in the plan's **Parameters** tab at any time and
read by the steps as `{name}`. A parameter is settled from the start (`completed`), has no connectors, is not drawn in the diagram or the list and is not counted among the steps. Its `spec` is
`prompt`/`prompt_en` (the label), `help`/`help_en`, `into` (the plan value), `kind` (`text`, `number`, `choice`), `choices`, `other`, `default`, `group`/`group_en`; unlike an answer it may be
empty (the plan says what an empty value means). The value is `shared_state[into]`: the default until it is set.

- **Web** (`serve/web/planparams.js`, the tab *Parameters*): the groups under their headings, each parameter a label, its explanation, a field by kind (a text is one line when empty and grows; a number;
  a choice of two is two buttons; a longer choice is a menu with "Other, I write" when `other` allows it), its default with a button to use it; Save sends only what changed.
- **API**: `GET /api/plans/<id>/parameters` -> `{parameters: [{node_id, name, prompt, prompt_en, help, help_en, group, group_en, kind, choices, other, default, value}], active}`;
  `POST /api/plans/<id>/parameters` `{values: {name: value}}` (event `plan:parameters`): every value is checked before any is kept. The snapshot carries `parameters` too.
- **The rule for a plan's first values**: a value the person gives before a run is a parameter, not an `input` node with its parents only starts. `PlanStore.get` migrates a plan saved the old way when it is
  loaded (`PlanDAG.migrate_initial_questions`): such an `input` (a value request whose parents are only starts) becomes a parameter, its answer or default is the plan value, and what waited for it waits for the start.
  Approvals, tasks and questions that come later in a run stay requests, below.
- The Chat's view of a plan (`plan_get`) lists the parameters as `name=value` in one line.

## A request to the person (the `input` node)

The `input` node is the plan's request for human intervention during a run: an approval, a value that only a later step can ask for, a task. One node is one question. `spec`:

- `ask`: `value` (default: what older plans have) or `approve`.
- `prompt` (up to 300 characters): the question, in the person's language (Italian); `prompt_en`: the same in English. `help` and `help_en` (up to 1000): one sentence of explanation shown under it.
  The page shows the one of its own language and the other when that is missing.
- `content` (up to 20000): what to look at, can use `{values}` of the plan and of the wired inputs.
- `into`: the plan value the answer is written to. Needed for `value`; for `approve` it may be left out (the answer goes to `<node id>_approved`).
- `kind` (`text`, `number`, `choice`) and `choices` (up to 40): a choice is `{value, label, label_en}`: the value is what the plan gets and is never shown; a choice that is only a text
  is shown as it is, so plans saved before keep working.
- `other`: a menu of more than two choices also takes what the person writes ("Other, I write"); `other: false` closes it to the choices. Two choices are two buttons and never take another answer.
- `default`: the answer proposed; it must be one the question would take. What was answered the last time the plan ran is proposed instead, when there is one.
- `group` / `group_en` (up to 80): the heading the question is listed under; an `action` for the person has them too.
- A start node is never a question: it is set in the plan's editor only.

A request is **asking** when its turn has come (its parents are done, it is not a draft). `GET /api/plans/human` lists all of them:
`{requests: [{plan_id, plan_title, node_id, title, ask (value, approve or task), prompt, prompt_en, help, help_en, group, group_en, content, kind, choices: [{value, label, label_en}], other, into, since, index, proposed}]}`,
by when they began asking and then in the order of the plan (`index`); a paused plan asks nothing.

**A step for the person** (an `action` assigned to `user`, whose outputs are at most one text) is asked too: `ask: "task"`, the title as the question, its description (with `{values}`) as what to do. It is answered with
`{done: true, note}`: the step is completed and the note, if there is one, is its output (the one text it declares in `produces`, or `<id>_note`). A task with other outputs stays for the Plans page.

The answers are sent **together**: the event `plan:answers` (`POST /api/plans/<id>/events`, `{type, answers: {step id: answer}, actor}`). Each is `true`/`false` (or yes/no, sì) for an approval, text,
a number or the value of one of the choices for a value. Every answer is checked before any is taken: one that is wrong, or for a step that is not asking now, leaves all of them as they were
(`PlanDAG.answer_many`). Then each step is completed as `plan:answer` does it (written to `into`, `state_variables.answer`, a line in the journal). `plan:answer` takes one answer alone, for the Chat.
A decision can branch on the values. When the person answered (not the Chat for them), the agent is started for what became ready.

### Where it shows

The questions of one plan are **one scrolling list** with all of them at once: full screen on a phone, a wide panel on a desktop, the same window over any tab.
Each question is the question in large type, its explanation under it, and its field; a plan may group them with `group` / `group_en` on the node (a short text such as *Who presents*): the heading is shown
before the first question of each group, in the person's language, and nothing is shown when a plan has none. The field follows the answer, the same everywhere:
two possible answers (an approval, or two choices) are **two buttons** with the answers in words; more than two are a **menu** with "Other, I write", which opens a text field; a free text or a number is the **field**;
a step for the person is in the same list: its description, an optional **note** and two buttons, "I did it" and "I can't now". Choosing an answer never sends anything.
At the bottom, always in sight, are *Later* and one *Send all answers*, with how many questions are not answered yet. Sending with some missing sends nothing: it shows which, and brings the first into view.
"I can't now" leaves that step out of what is sent and it comes back after 10 minutes (the rest of the plan's questions are asked as usual).
*Later* leaves the plan's questions for 10 minutes with the answers kept. What the plan proposes (or was answered last time) is already in each field.

- **Web**: `serve/web/human.js` polls every 4 s and opens a modal window over whatever tab is open (`#hr-card`). Enter in a text field goes to the next field. Esc is Later.
  Targets are 48 px, the window is full screen from 600 px wide down; strings in Italian and English.
- **App** (`mobile/lib/src/human_requests.dart`): the same, over any tab while the app is open; looked for every 4 s, not while it is in the background.

`serve/tests/test_human_page.py` drives the web window in a real headless browser at 360x640, 390x844 and 1280x800 (and in Italian): the whole list with its headings, the bottom line, Send with some missing, Other, one Send with every answer, Later, a step for the person.

The texts of a plan are written in Italian and English **on two lines**, Italian first: a title as `Italian\nEnglish`, a description as the Italian summary and then the English; the plan page keeps the line break.

## A run that was cut short takes up again

A run is **active** from when a start is taken, or a step is marked `in_progress`, until the plan finishes (`bridge.run`, saved with the plan). When the program starts (`resume_interrupted`, once):

- a plan that is active, or has a step `in_progress`, and is not paused or finished is taken up again;
- the steps done stay done, with the values they wrote; a step that was being done goes back to `pending` with `state_variables.interrupted`; waits go on counting from when they began;
- the agent is started for what is ready;
- a plan taken up again 3 times in a row without one step more done is marked `stalled` and left for the person (`RESUME_LIMIT`).

## A step with several inner steps goes on from the one it reached

A step that does several things in a row (the Video step: voice, stills, takes, B-roll, edit) keeps in the plan which of them it has finished, so that after an interruption (a crash, a restart of the app or of the PC)
it goes on from there and does not do them again.

- **What is kept.** `state_variables.inner_done`: `[{name, result, at}]`, saved with the plan, so it survives a restart of the PC. The result is the essential (the files made, the values: plain data up to 2000
  characters), not the content. An `action` may name its inner steps, in order, in `spec.steps`; then only those can be recorded and the plan knows which is next and which are left. Without `steps` any name is taken.
  Recording a step again replaces it; `undo` takes it off the list so that it is done again. A rewind forgets them (the history keeps them).
- **How the agent says it.** The tool `plan_step_done(plan_id, node_id, step, result, undo)` after each stage, for a step it started with `plan_step_start`. `plan_step_start` and `plan_get` tell what is
  already finished.
- **How the program's commands say it.** `tadamoo/planner/inner.py` `record(step, result)`: the command finds the step by the conversation that runs it (`TADAMOO_SESSION_ID`; the step it is doing:
  `plan:mark in_progress` carries the session) and the server by `TADAMOO_URL` and `TADAMOO_API_KEY`, which the conversation passes to its commands; or it names `plan_id` and `node_id`. It is `POST /api/plans/inner`.
  A server that does not answer is a stage not recorded, never a stopped command. `phase_recorder` turns the `phase` events of a production into stages: `python -m tadamoo.newscast make` reports each phase it ends
  (voice, stills, takes, broll, edit) with its folder and what it made.
- **When it is taken up again** (a restart, or a recovery): the step goes back to waiting with its inner steps kept and the conversation that was doing it forgotten; the words the new conversation is told say
  *Already done inside this step (do not do them again): voice (...); stills (...). Continue from: takes. Still to do: takes, broll, edit.* The same words are in what the recovery of a failed step is told.

## A step that fails or runs out of time is recovered

Every kind of step is covered: one the agent does, one the plan works out itself (`set_var`, `get_var`, `output`, `log`, `start_plan`...), a wait, a condition.

- **Failed.** `plan_mark ... failed` takes a `reason`, kept on the step (`failures`, the last 5). A step the plan works out itself that cannot be done (an expression that fails, a value that is not there) is looked at
  again at every turn of the clock, so it is done the moment the cause goes away; the tries of its `retry_policy` (default 3, 30 s, doubling) are counted over time, and when they are used up it has failed (`advance` reports `failed`).
- **Timed out.** A wait that passes its `timeout_s` with `on_timeout: fail`, and a step reported with `plan:timeout`, stop with `state_variables.timed_out` (`advance` reports `timed_out`).
- **Recovery.** The steps below are parked and the agent is started with the step, its kind and spec, what is known, and the order to find the real cause, **write or change code** if that is what it takes, change the
  step, set the values it needs or force another way that passes; then mark the step `pending` (a step marked pending waits afresh and has its tries again) and do it again, or `completed`/`skipped`, or `failed` with the reason.
  This is *recovery n of 3* (`RECOVER_LIMIT`, `state_variables.recoveries`); a step that is still stuck after the third is left for the person, with a line in the journal.
- **At start-up** a failed or timed-out step of a run that was active is recovered too, counted with the step's other recoveries.
- What is for the person (`assignee: user`: a request, a task of theirs) is not recovered by the agent.

Tests: `tests/test_planner_human.py`, `tests/test_planner_resume.py`.
