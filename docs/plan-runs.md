# Plan runs: requests to the person, taking up again, recovery

## A request to the person (the `input` node)

The `input` node is the plan's request for human intervention, for every plan. `spec`:

- `ask`: `value` (default: what older plans have) or `approve`.
- `prompt` (up to 300 characters): the question. `content` (up to 20000): what to look at, can use `{values}` of the plan and of the wired inputs.
- `into`: the plan value the answer is written to. Needed for `value`; for `approve` it may be left out (the answer goes to `<node id>_approved`).
- `kind` (`text`, `number`, `choice`) and `choices`: what a value is.

A request is **asking** when its turn has come (its parents are done, it is not a draft). `GET /api/plans/human` lists all of them, oldest first:
`{requests: [{plan_id, plan_title, node_id, title, ask, prompt, content, kind, choices, into, since}]}`; a paused plan asks nothing.
The answer is the event `plan:answer` (`POST /api/plans/<id>/events`, `{type, node_id, answer, actor}`): `true`/`false` (or yes/no, sì) for an approval; text, a number or one of the choices
for a value. It is checked, written to `into`, the step is completed (`state_variables.answer`) and a line goes in the journal; a wrong answer changes nothing. A decision can then branch on the value.
When the person answered (not the Chat for them), the agent is started for what became ready.

### Where it shows

- **Web**: `serve/web/human.js` polls every 4 s and opens a modal window over whatever tab is open (`#hr-card`). The oldest request first. An approval: Yes or No, then *Confirm and send*; a value:
  a field, OK (or Enter), then *Confirm and send*. *Back* on the first screen leaves the request for 10 minutes; from the confirmation it returns to the choice; Esc does the same.
  Targets are 48 px and the window is inside the screen from 360 px wide.
- **App** (`mobile/lib/src/human_requests.dart`): the same, over any tab while the app is open; looked for every 4 s, not while it is in the background.

`serve/tests/test_human_page.py` drives the web window in a real headless browser at 360x640, 390x844 and 1280x800.

## A run that was cut short takes up again

A run is **active** from when a start is taken, or a step is marked `in_progress`, until the plan finishes (`bridge.run`, saved with the plan). When the program starts (`resume_interrupted`, once):

- a plan that is active, or has a step `in_progress`, and is not paused or finished is taken up again;
- the steps done stay done, with the values they wrote; a step that was being done goes back to `pending` with `state_variables.interrupted`; waits go on counting from when they began;
- the agent is started for what is ready;
- a plan taken up again 3 times in a row without one step more done is marked `stalled` and left for the person (`RESUME_LIMIT`).

## A step that fails is recovered

`plan_mark ... failed` takes a `reason`, kept on the step (`failures`, the last 5). A step that has used its tries (`retry_policy`, default 3) is not left to stop the plan: the agent is started
with the step, the reasons, and the order to find the real cause, **write or change code** if that is what it takes, set what the step needs or force another way that passes, mark the step `pending`, do it
again and mark it `completed`, or `failed` with the reason. This is *recovery n of 3* (`RECOVER_LIMIT`, `state_variables.recoveries`); each is told what the earlier ones found. A step that fails after the third is left for
the person, with a line in the journal. A `start_plan` step that fails gets the same.

Tests: `tests/test_planner_human.py`, `tests/test_planner_resume.py`.
