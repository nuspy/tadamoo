# The gate at the end of a turn, hooks that talk back, and the loop guard

What stands between the model's last message and the end of a turn, and what watches the turn while it runs. Code: `harness/gate.py`
(the verify gate), `harness/loop_guard.py`, `harness/hooks.py`, `harness/todo.py`, `harness/error_classifier.py`, and the wiring in
`harness/agent_loop.py` (`_before_end`, `_run_gate`, `_observe`, `_budget_warning`). The settings page is Settings > General > Checks before
an answer ends (`serve/web/gate.js`, `serve/gate_prefs.py`, kept in `gate.json` in the user data folder). The design comes from
[harness-study](harness-study/README.md).

## What happens when a final answer arrives

A reply with text and no tool call is not yet the end. In this order:

1. the answer is measured against the request's card (`harness/pipeline.py`), once;
2. the **verify gate**, if it is on and files changed since it last looked;
3. the **checklist** (`todo_write`): if items are still open the model is reminded once;
4. the **stop hooks**: a hook may deny the end.

Each of 2 to 4 that sends the model back puts a hidden message at the tail of the conversation (`note: true`, starting with `[harness]`;
the Chat shows it as a note and the history does not list it), so the cached start of the prompt never changes. A turn that ends in a
cut reply, an empty reply or a loop does not pass through this. A subagent never does: its parent's turn has the gate, the checklist and
the stop hooks.

## The verify gate

Off unless the person turns it on. Settings:

| setting | option | default |
|---|---|---|
| the commands | `--verify-command CMD` (repeatable), up to 8, one line each | none |
| when | `--verify-gate off\|unattended\|always` | `off` |
| fixes allowed | `--verify-rounds N` (1 to 3) | 3 |

`unattended` means a conversation started with `--unattended` (routines, the phone's automatic conversations) or a headless run without
`--interactive`. **Whether the gate is on by default for unattended runs is the owner's decision**; today it is off everywhere.

- The gate looks only after files changed: it compares the sandbox's count of recorded file changes (file tools and the text files a shell
  command changed) with the count when it last ran. A turn with no edits runs nothing.
- Each command runs as an ordinary `shell_command` call of the conversation (`AgentLoop._execute`): argument checks, pre-tool hooks, the
  deny rules, plan mode, checkpoints, the audit record. **Listing a command in the setting approves that exact string**
  (`PermissionManager.approve`), so it does not ask at the Default level; a deny rule or the built-in refusal of dangerous commands still
  refuses it. A command named by a project file is never run: only the list the person typed on this PC counts (`/api/gate` answers only
  to this PC).
- The runs are shown like the model's calls (events `tool_start` / `tool_end` with `gate: true`) and then one event `gate` with
  `decision` `pass`, `retry` or `stop`, the round and each command's outcome. The audit has `verify_gate` records.
- **Pass:** the turn ends. **Fail:** a hidden message with the command, the exit code and the last 60 lines of its output plus every earlier
  line that looks like an error (up to 40), inside an untrusted-data fence, and the instruction to fix the cause and not to change or skip
  the checks. The model works again; the gate runs again when it answers.
- **Stops:** after `rounds` fixes that still fail; earlier when the same failure comes back unchanged (the failing commands and the tail of
  their output, digits ignored); at once when a command cannot be run at all (exit 127 or 9009, "not recognized", "no tests ran",
  "collected 0 items", `pytest` or `ruff` missing, blocked by a rule). Those are the person's to fix, not code bugs, so they are not sent
  back. The run ends with stop reason `verify_failed` and an `error` text that says which; headless exit code 8. The model's last answer is
  kept as the answer, and the work is reported as not verified.
- Cost: a slow command times out at 300 seconds (the shell tool's own timeout) and counts as a failure.

### With the Target Loop

The Target Loop's check criteria (`options.verify`, [target-loop.md](target-loop.md)) are commands run in the conversation. When the gate
already ran a command in a task's turn on the files as they are, the loop reuses that outcome instead of running it again and writes
"run by the verify gate after the last change" in the evidence. `TargetLoop._note_gate` remembers the gate's runs from the turn's tool list
(the `gate` flag) and forgets them at the next call that can change files. A check turn that only runs the command does not trigger the
gate, because no file changed. With the gate off nothing changes in the Target Loop.

## Hooks that talk back

Events and what a hook can do (file format in [harness.md](harness.md), "Hooks you write"):

| event | when | can say |
|---|---|---|
| `pre_tool` | before a call | block it (exit 2, `{"decision": "block"}`, or `action: block`); a hook that cannot run blocks too |
| `post_tool` | after a call | feedback: exit 2 or `{"decision": "block", "reason": ...}` puts the reason beside the result, inside an untrusted fence, up to 1,500 characters |
| `stop` | a final answer is about to end the turn | deny it the same way; the reason is told to the model in a hidden message; at most 2 denials per request, then the answer stands |
| `pre_compact`, `post_compact` | the context is over its threshold, and after it got smaller | nothing (payload: tokens before and after, messages, actions) |
| `subagent_start`, `subagent_stop` | around `spawn_subagent` | nothing (payload: task, tools; stop reason, summary) |
| `turn_end`, `session_start`, `session_end` | as before | nothing |

A `post_tool`, `stop` or event hook that cannot run (a timeout, another exit code) never blocks anything: its failure is collected in
`HookManager.errors`. The `stop` payload has `final` (the answer, cut at 2,000 characters), `turns` and `stop_hook_active` (true after the
first denial of a request, so a hook can end its own insistence). Built-in hooks are registered with `register_post_tool_hook` (the function
may return feedback) and `register_stop_hook`. Workspace hook files still need `--workspace-hooks`.

## The loop guard

Two layers. `LoopDetector` (as before) refuses the third identical call in a row, and a same error three times in a row; a change to the
files now clears its history (`progress()`), so the same test run again after an edit is not a repeat. `OutcomeGuard`
(`harness/loop_guard.py`) looks at each executed call as a pair, the call and a hash of its result with times, durations, process ids and
addresses taken out:

| it sees | note | strike (toward ending the run as a loop; more than 2 end it) |
|---|---|---|
| the same call with the same result | at 3 | at 5 |
| a cycle of 2 to 4 calls with the same results (A B A B A B) | at 3 rounds | at the 4th round |
| one result for many different calls of one tool | at 4 | at 12 (not for a shell) |
| one tool failing again and again, whatever the errors | at 5 | at 8 (not for a shell or `run_code`) |

- **Progress** is a change to the files (the sandbox's change count; without it, a successful call of a reversible tool). It clears the
  counts, so work that goes round a bit between real changes is not stopped.
- **Identical-result stubs.** A long result (400 characters or more) of a read-only tool that is the same as an earlier result of the same
  call, while that earlier text is still in the conversation, is replaced by one line pointing at the first call. Errors, results of tools
  that change things and untrusted web text are left as they are.
- `ask_user`, `todo_write` and `present_plan` are asked again by design and never counted.
- Notes are added to the result they follow; each is said once until progress clears it.

## Budgets: a warning and one last report

At nine tenths of the steps (`--max-turns`), tokens (`max_total_tokens`) or time (`max_seconds`) of an answer a note tells the model to finish
what matters and to say what is done and what is not. When a budget runs out the loop makes **one more** model call, with a note that no
tool may be called: its text is kept as the answer, any call in it is not run, and the stop reason stays `max_turns` or `budget`. The budget
is therefore exceeded by that one call. `--no-budget-grace` (Settings: "Warn before a budget runs out and allow one last report") turns both
off. Subagents get neither, and neither do the evaluation runner and the replay (they measure the budget as it is). Events: `budget_warning`,
`budget_grace`.

## todo_write

A small checklist for a task of several steps (`harness/todo.py`). The model sends the whole list each time; items are `pending`,
`in_progress` or `completed`, at most one in progress, at most 50 items of 200 characters. It touches nothing but the conversation's own
list, so it is allowed at every permission level, and it is registered in the fixed group at the start of the tool list: using it, or not
having used it, never changes the cached start of the prompt (`tests/test_prefix_hygiene.py` holds the order). Each change is also an
event `todo` with the items. Before a turn ends with items open the model is reminded once (`todo_open`) to finish them or to update the
list; the answer then stands. The list lives with the tool registry, not in the history, so it survives compaction of the messages.

## The error classifier (proposal, off)

**Awaiting the owner's decision.** Today every failed model call is retried without end, with growing waits, whatever the error; a request
the server will always refuse loops for ever. `harness/error_classifier.py` sorts an error by its HTTP status (0: unreachable) and message:

- `transient`: unreachable, 408, 409, 425, 429, 5xx, or words like loading, no model, busy, try again. Waited for as now.
- `overflow`: the conversation is longer than the context. Retried as now (compacting before the retry is for the Compaction track).
- `deterministic`: 400, 401, 403, 404, 405, 410, 413, 415, 422 without those words. With `--error-classifier` (Settings: "Proposal: end the
  turn on an error that waiting cannot cure") the session emits `model_error` and ends the turn with stop reason `error` and the message
  shown; without it nothing changes.

The trade-off: a visible end tells the person at once that a key is refused or a request is malformed; but it breaks the rule that a model
error never leaves a turn blocked, and a misclassified transient error would end a turn that waiting would have saved.

## Cloud route profile (Core addition)

`harness/llm/profile.py` gives each route a profile read from its type and the provider's record: `cache_control` (Anthropic routes mark
the end of the tools and of the system text as cache points; `"cache_control": false` in the record turns it off), `context_window` and
`max_output` (Providers > the provider's form; the agent plans its context by the window, and a reply is kept inside the output limit), and
`stateless` / `accepts_ids`. The Anthropic response's cache reads are counted in `prompt_tokens` and reported as `cached_tokens`. A route to
the loaded model, and any route with no limits stated, is unchanged.

## Tests

`tests/test_hooks_talk_back.py`, `test_verify_gate.py`, `test_loop_guard.py`, `test_budget_grace.py`, `test_todo_write.py`,
`test_error_classifier.py`, `test_llm_route_profile.py`, `test_gate_settings.py`: all with the scripted mock model, no GPU. What needs the
real model is queued in `tests/QUEUE_daytime.md` (rows 45 to 47).
