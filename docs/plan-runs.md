# Plan runs: requests to the person, taking up again, recovery

## A request to the person (the `input` node)

The `input` node is the plan's request for human intervention, for every plan. One node is one question. `spec`:

- `ask`: `value` (default: what older plans have) or `approve`.
- `prompt` (up to 300 characters): the question, in the person's language (Italian); `prompt_en`: the same in English. `help` and `help_en` (up to 1000): one sentence of explanation shown under it.
  The page shows the one of its own language and the other when that is missing.
- `content` (up to 20000): what to look at, can use `{values}` of the plan and of the wired inputs.
- `into`: the plan value the answer is written to. Needed for `value`; for `approve` it may be left out (the answer goes to `<node id>_approved`).
- `kind` (`text`, `number`, `choice`) and `choices` (up to 40): a choice is `{value, label, label_en}`: the value is what the plan gets and is never shown; a choice that is only a text
  is shown as it is, so plans saved before keep working.
- `default`: the answer proposed; it must be one the question would take. What was answered the last time the plan ran is proposed instead, when there is one.
- A start node is never a question: it is set in the plan's editor only.

A request is **asking** when its turn has come (its parents are done, it is not a draft). `GET /api/plans/human` lists all of them:
`{requests: [{plan_id, plan_title, node_id, title, ask, prompt, prompt_en, help, help_en, content, kind, choices: [{value, label, label_en}], into, since, index, proposed}]}`,
by when they began asking and then in the order of the plan (`index`); a paused plan asks nothing.

The answers are sent **together**: the event `plan:answers` (`POST /api/plans/<id>/events`, `{type, answers: {step id: answer}, actor}`). Each is `true`/`false` (or yes/no, sì) for an approval, text,
a number or the value of one of the choices for a value. Every answer is checked before any is taken: one that is wrong, or for a step that is not asking now, leaves all of them as they were
(`PlanDAG.answer_many`). Then each step is completed as `plan:answer` does it (written to `into`, `state_variables.answer`, a line in the journal). `plan:answer` takes one answer alone, for the Chat.
A decision can branch on the values. When the person answered (not the Chat for them), the agent is started for what became ready.

### Where it shows

The questions of one plan are one path: *Question 3 of 22*, the question large, its explanation under it, the field (a menu of labels, a text, a number, Yes or No), *Back* and *Next*;
then a summary of every answer with *Edit* beside each and one *Send all answers*. Next refuses an empty or wrong answer; Edit from the summary returns straight to it after the change.
*Later* leaves the plan's questions for 10 minutes with the answers kept. What the plan proposes (or was answered last time) is already in each field.

- **Web**: `serve/web/human.js` polls every 4 s and opens a modal window over whatever tab is open (`#hr-card`). The left and right arrow keys move along the path (not inside a text field) and Enter goes on from a field;
  the summary opens on its title, so a second Enter cannot send. Esc is Later. Targets are 48 px, the window is inside the screen from 360 px wide; strings in Italian and English.
- **App** (`mobile/lib/src/human_requests.dart`): the same, over any tab while the app is open; looked for every 4 s, not while it is in the background. A swipe left or right moves along the path, and so do the arrow keys of a keyboard.

`serve/tests/test_human_page.py` drives the web window in a real headless browser at 360x640, 390x844 and 1280x800 (and in Italian): the questions, the keys, the summary, Edit, one Send with every answer, Later.

## A run that was cut short takes up again

A run is **active** from when a start is taken, or a step is marked `in_progress`, until the plan finishes (`bridge.run`, saved with the plan). When the program starts (`resume_interrupted`, once):

- a plan that is active, or has a step `in_progress`, and is not paused or finished is taken up again;
- the steps done stay done, with the values they wrote; a step that was being done goes back to `pending` with `state_variables.interrupted`; waits go on counting from when they began;
- the agent is started for what is ready;
- a plan taken up again 3 times in a row without one step more done is marked `stalled` and left for the person (`RESUME_LIMIT`).

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
