# Starting a plan: start nodes, schedules in words, a plan that starts another

Every plan has at least one **start node** (`start`), where it begins. The steps a start leads to wait for it. A start has a number and says how it begins.

## The start node

`spec`: `number` (1 to 99, unique in the plan), `launch`, and what the launch needs.

| `launch` | Starts when | Needs |
|---|---|---|
| `manual` | the person presses Start on the node, or the Chat calls `plan_start` | nothing |
| `event` | an event arrives (the planner's mailbox, `POST /api/plans/events`) and the condition holds | `when`: an expression over `text` (lower case), `source`, `at` and the plan's values, as a sensor's |
| `interval` | the schedule comes due | `words`: the time as people say it; `schedule` is made from it |
| `caller` | another plan's `start_plan` step names this start | nothing |

A start has no input connector and no parents. `PlanDAG.begin(number, cause)` takes one start (`completed`, with `started: {at, cause, caller?, matched?}` and a count of `runs`)
and skips the other starts and the steps only they lead to; a merge below goes on with the branch that is alive. A plan that is `running` is not started again
(`PlanError: already running`); one that has `finished` (every step done or skipped) is rewound from its starts and begins again; an `idle` one just begins.
`run_state()` says which; the snapshot carries it as `run: {state, active, since, resumes, progress, stalled}`.

**A plan with no start gets one.** When a plan is loaded or made, `ensure_start()` adds `start` (or `start_2`...), `manual`, number 1, already `completed` and marked
`implicit`, with a flow edge to every step that waited for nothing. It was always there, so nothing it leads to is held back: old plans load and run as before.
It is not counted among the steps (the list's `nodes` and `completed`, the card's steps).

A start added with a number that is taken gets the next free one; changing a number to a taken one is refused.

## Schedules in words

`tadamoo/planner/schedule_words.py` `to_schedule(words)` makes the text of the routines' own schedules (`tadamoo/scheduler.py` `Schedule`), in Italian or English. Without a time
of day a fixed day is at 09:00. What it does not understand is refused with examples; nothing is guessed.

| Said | Becomes |
|---|---|
| ogni sei ore, every 6 hours | `every 6h` |
| ogni due giorni, every 2 weeks | `every 2d`, `every 14d` (an interval has no time of day) |
| ogni giorno alle 7, every day at 3pm | `daily 07:00`, `daily 15:00` |
| ogni lunedì e venerdì alle 18 | `mon,fri 18:00` |
| ogni giorno feriale alle 8, every weekend at 10 | `weekdays 08:00`, `weekends 10:00` |
| ogni primo lunedì, every first Monday at 9 | `cron 0 9 * * 1#1` |
| ogni ultima domenica del mese alle 15 | `cron 0 15 * * 0L` |
| il 15 di ogni mese alle 10 | `cron 0 10 15 * *` |

A schedule already in the routines' form, or five cron fields, is kept. The cron day-of-week field takes `d#n` (the n-th weekday of the month, 1 to 5) and `dL` (its last), beside the
ordinary list; they combine with the day-of-month field as the day-of-week field of cron does.

## Interval starts

`PlanService` keeps, for each plan, when each interval start is next due (`_index`, refreshed at every save). The clock of the routines calls `watch_tick(now)`; a start that
has come due starts the plan (`start_plan(plan_id, number, "its schedule")`) unless the plan is running or paused (that time is skipped, with a line in the journal). It is counted from
the last time it fired (`last_fire`), or from when the plan was first seen with this schedule (`armed`), so a schedule missed while the program was off fires once when it starts.

## A step that starts another plan

`start_plan` (`spec: {plan, start}`) is worked out by the service, like a wait: the other plan is started at the start whose number it names, with
`caller: {plan, node}` on that start, and the step is done at once; the plan does not wait for the other to finish. When it cannot be (no such plan or number, the plan is running) the reason is kept in
`state_variables.failures` and the step follows its retry policy, then fails.

## Carrying the plan on with the agent

A plan started this way (or by the person's Start button, or an event) is carried on by an Automatic conversation of the agent. `PlanService.launcher(plan_id, name, text)` starts it; the server
sets it to one routine for each plan (`dedup_key: planrun:<id>`, run by hand, in the chat folder), so it shows in Cron Jobs with its history and can be stopped. It is started only for
steps that are ready, for the agent, and that no conversation was started for yet, and not while one is at work on the plan. What the agent does itself in a conversation (its own start, its own marks)
starts nothing more. Human requests and the steps for the person are not the agent's. See [plan-runs.md](plan-runs.md).

Tests: `tests/test_schedule_words.py`, `tests/test_planner_starts.py`.
