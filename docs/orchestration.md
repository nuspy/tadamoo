# Orchestration: cron, sessions, file ownership, plans

How a schedule, a conversation and a plan can start, message and watch each other without a person at the keyboard. Everything
here runs through the real building blocks (the scheduler, `SessionManager`, the planner service), not a separate system.

## Conversations that message each other

A conversation has a stable name (`alias`, letters/digits/dashes, unique among the ones not closed) and an id. `serve/agent_sessions.py`
resolves either to the same conversation (`resolve(ref)`).

- `session_send(ref, message)`, `session_status(ref)`, `session_spawn(workspace, prompt, background, alias, rules)`,
  `session_digest(ref, n)`: tools in `tadamoo/orchestration_tools.py`, a thin client (`tadamoo/integrations/orchestration_client.py`)
  over the server's loopback routes. A conversation proves which one it is with the id and token it was started with
  (`TADAMOO_SESSION_ID`/`TADAMOO_SESSION_TOKEN`, an env var, never a CLI argument); the server checks the token and the message's
  `sender` is read from it, never from what the caller claims.
- These tools, and `cron_*` (below), are **not in every conversation's prompt**: they cost tokens most conversations never use. They
  are registered only for a conversation started with `--orchestration` (every conversation with an alias, `unattended`, spawned by
  `session_spawn`, or itself unattended through a routine already gets it), or on request: `find_capability` lists `builtin:orchestration`,
  and `load_capability("builtin:orchestration")` turns the tools on for the rest of that conversation (`tadamoo/capabilities.py`'s
  `activate` hook, the same idea as `take_place`).

## Delivery, not a direct call

A message to another conversation is **held by the server** (`serve/deliveries.py`), not written straight to the target's stdin:
the target may be asleep, at work, or gone.

- States: `pending` (waiting for the target to be idle or dormant), `delivered` (its process took it as a turn), `dead` (reason
  `unreachable`, `busy_timeout`, `loop`, or `budget`), `dismissed`.
- A dormant target is woken (`SessionManager._ensure`); a busy one is retried until `busy_timeout_s` (default 30 min); a written
  message not acknowledged in `ack_timeout_s` is written again, and the child drops an id it has already taken
  (`tadamoo/provenance.py`'s `SeenDeliveries`), so a retry after a restart is at most one turn.
- Dead letters: `GET /api/agent/deliveries?state=dead`, `POST .../retry`, `POST .../dismiss`; shown in the Cron Jobs tab for the ones
  a schedule sent (`provenance` starting with `cron` or `plan`).

## Provenance: who may give orders

Every message that is not the person's own carries where it came from (`tadamoo/provenance.py`), read by `AgentLoop.add_user_message`:

- `cron` / `cron:<id>` and `plan:<id>` are put in as an order (a system note, like the planner's own), because the user already
  decided cron and plans are trusted once created.
- `agent:<name>` (another conversation) is **data**: fenced as untrusted, the turn is tainted (the Rule of Two counts it), and an
  instruction found in it makes the permission manager ask before any change — **unless** the receiving conversation was given
  `--cross-commands <name>` (a glob, set per conversation only, never in the global allow list): then the text is read plain, though
  the taint (and so the Rule of Two) still holds.
- A chain of up to 2 hops is kept per message; a sender appearing a third time is a dead letter (`loop`).

## Shared files: one owner

`serve/file_owners.py` (server, `/api/file-owners`) and `tadamoo/file_owners.py` (read by the conversation's own process on every
write and edit, `tadamoo/tools/filesystem.py`) say which conversation's id owns a path, and in what mode:

- `owner`: only that conversation writes or edits it; anyone else's `write_file`/`edit_file` is refused with a pointer to
  `session_send`.
- `append`: anyone may add whole lines with `write_file(..., append=true)`, under a lock held across processes
  (`tadamoo/audit.py`'s `file_lock`, a sidecar `.lock` file); a whole-file write or an edit is still refused.
- A command run through `shell_command` is **not checked**: it can still write an owned file. Ownership guards the two file tools,
  not the shell.

## Schedules (`tadamoo/scheduler.py`, `tadamoo/cron_tools.py`)

A routine's schedule is `every Ns/m/h/d`, a fixed time or day list, a 5-field cron expression, or `at <moment>` (once, then turned
off). Each scheduled fire has an id from the routine and its planned local time (`fire_id`, stable across a DST fall-back), written
to a fire log **before** anything starts, so a restart never runs the same fire twice (`journal/fires.json`). `ttl_min` makes a late
fire `skipped_late` instead of running cold; `dedup_key` keeps routines that share it from firing at once (`skipped_dup`).

`deliver_to` says where a fire goes:

- `{"kind": "new_session"}` (the default): starts a conversation and runs the prompt in it, as before.
- `{"kind": "session", "ref": name}`: delivers `message` (or `prompt` if empty) to an existing conversation, `provenance="cron:<id>"`.
- `{"kind": "plan", "ref": name}` with `link: {"plan": plan_id}`: same delivery, `provenance="plan:<id>"`; skipped
  (`skipped_paused`) instead of sent while that plan is paused (`PlanService.is_paused`, wired through `SessionRoutineRunner`).

`message` is a template (`tadamoo/cron_templates.py`): `{date}` always, `{plan_id}` for a plan delivery, and — through
`PlanService.template_context` — `{plan_title}`, `{ready_step}` (its first ready node) and any of the plan's own shared values
(`{bilancio}` once something has written it). An unresolved name stays literal with a trailing note; nothing uses `str.format`.

`cron_list/create/update/pause/resume/delete/fire_now` (`tadamoo/cron_tools.py`) are thin tools over `/api/routines`
(`RoutinesClient`), registered with the orchestration tools. `cron_pause`/`cron_resume` are `set_enabled`; `cron_fire_now` is
`run_now`. Mutating a routine follows the normal permission gate (asks below Auto, allowed at it) — there is no separate
"created by me" shortcut here, because the user decided cron and plans are trusted regardless of who made them; the shortcut
exists on the **plan** side instead (next section).

## Plans: owner, pause, schedule

- A plan remembers which session made it (`created_by`, sent by `PlannerClient` from `TADAMOO_SESSION_ID`). `Confirmer.check(...,
  owner=True)` auto-approves a change that touches nobody else, for the plan's own session, the same way the Auto permission level
  does — below Auto, another conversation is still asked or refused.
- `plan_pause` / `plan_resume` stop or restart the plan's schedule without touching the plan; a fire tied to a paused plan is
  recorded `skipped_paused`, never lost.
- `plan_set_schedule(plan_id, session_ref, schedule, instruction)` creates (or replaces) a routine with `deliver_to: plan` through
  `cron_create`, and records it on the plan (`schedule: {routine_id, session_ref, schedule, instruction}`); `plan_remove_schedule`
  deletes that routine and clears the record. Needs a `cron_create` tool in the same conversation (loaded with the orchestration
  tools); without one it says so instead of failing silently.
- `plan_get(plan_id, view="compact")` gives a few lines (state, ready steps, values) instead of the full reading.
- `plan_digest(plan_id, n)` reads the plan's own history (`rev`, actor, kind, summary), already kept by the bridge; no new storage.

## Repeating steps (`tadamoo/planner/dag.py`)

A node's `repeat: {every, max_runs, reset_from, stop_when}` makes `mark(..., "completed")` re-arm it instead of finishing it: it
goes back to `pending`, counts a run, and is not `ready_nodes()` again until `repeat_at` (`now + every`) passes. It ends for good,
staying `completed`, once `stop_when` (the same small expression language as a sensor's `when`) holds against the plan's shared
values, or `max_runs` is reached — whichever first; a `stop_when` that cannot yet be read (a name not written yet) does **not**
stop the repetition, it tries again next time. A step that depends on a repeating one waits for every run to finish, not just the
first — so a repeating check with no `max_runs`/`stop_when` would block its own plan's closing step forever; give it one of the two
if anything follows it. `reset_from` (`"value:<name>"`, `"session:<ref>"` or `"skill:<name>"`) is a **label**, not code that moves a
value: the value a run writes with `plan_mark`'s `outputs` is already in the plan's shared state for the next run, because shared
state persists on its own.

There is no separate timer source for repeating steps: the existing cron delivery (above) is what wakes a conversation to read the
plan and mark its ready step when the time comes; `repeat` only tracks the DAG's own run count and stop condition.

## Budget and digest (`serve/session_budget.py`)

A conversation can have `budget_day_tokens` (set at creation or with `POST .../budget`); `SessionManager.over_budget` compares it
against tokens counted so far **today** from the cumulative `total_tokens` each `done` event reports (a lower number than last time
means the process restarted, counted as that call's own usage, not a negative). Over budget, a delivery to that conversation is
`dead/budget` — nothing proactively ends a turn already in progress; the cap only gates new deliveries. `GET .../digest?n=` (and the
`session_digest` tool) gives the last `n` lines of what a conversation answered, took in and sent, oldest first, from its own event
stream and the delivery log — enough to see what an unattended run did without reading its whole history.

## Where things are kept

- Conversations: `<base>/sessions/<id>/` (`history.json`, `inflight.json`, `archive.jsonl`, `tool_effects.json`, `rules.md`, `snapshots/`), plus the records shared by all of them in `<base>/sessions/` (`agent_sessions.json`, `compares.json`, `nodes.jsonl`). `<base>` is `journal/` of the data folder unless one was chosen in Settings > General (`sessions-base.json` in the default data folder; `serve/userdata.py`'s `sessions_base()`). Moving it (`serve/sessions_move.py`, `/api/sessions-base/check|move`) copies the whole `sessions/` folder, checks every file by size and hash, leaves the original, and asks for a restart; nothing moves while a conversation runs. Project memory (`memory/<workspace>.db`), the search of past conversations (`memory/past.db`, keyed by conversation id) and the list in the page do not depend on the folder.
- Plans: `planner/plans/<id>.json` (archived ones in `planner/plans/archive/`) and `planner/planner.json`; schedules in `journal/` as below. Both are global to the user, not inside any conversation's folder, and are written whole through a temporary file and a rename.
- Schedules: `journal/routines.json`, `runs.json`, `pending.json`, `fires.json`, same way of writing.

## What was not built

A separate "PlanTimers" scheduler source (the cron mechanism above already wakes a session for a repeating step); a per-routine
"created by an agent needs more approval" distinction (the user decided cron and plans are trusted once created, regardless of
who made them); proactively warning or ending a turn mid-flight when the daily budget is hit (only new deliveries are gated);
ownership enforcement on `shell_command` (noted above).
