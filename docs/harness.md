# The agent harness, the planner and the interface shell

This page describes `harness/` (the agent), the planner, the changes to the web pages and how to check all of it. The
harness is a separate process: it talks to a running Basalt server over the public OpenAI-compatible API and imports
nothing from the engine. `python -m pytest tests` runs its tests without a GPU, against the server with the scripted mock engine.

- [Running the agent](#running-the-agent)
- [How a tool call is decided](#how-a-tool-call-is-decided)
- [Secrets, payments and phone calls](#secrets-payments-and-phone-calls)
- [Context, memory, skills and reference documents](#context-memory-skills-and-reference-documents)
- [MCP and ACP](#mcp-and-acp)
- [The planner](#the-planner)
- [The web pages](#the-web-pages)
- [Evaluation](#evaluation)
- [Tests and what is not done](#tests-and-what-is-not-done)

## Running the agent

```powershell
python -m harness -p "fix the failing test in calc.py" --workspace D:\work\proj --json
python -m harness.acp_agent --workspace D:\work\proj          # the same agent for an editor that speaks ACP over stdio
basalt-agent -p "fix the failing test" --workspace D:\work\proj   # basalt-agent.cmd: python -m harness from any folder
basalt-agent acp --workspace D:\work\proj                          # = python -m harness acp = python -m harness.acp_agent
```

`basalt-agent.cmd` (next to `Avvia-Basalt.ps1`) uses this folder's `.venv` and packages, so it works from the project you are in. There is
no `pyproject.toml`, so nothing is installed and no command is added to the PATH; put the folder on the PATH, or call the file by its path.

The command line (`python -m harness`), the Chat's process (`python -m harness session`) and the editor's agent (`acp`) take the same
options and build the agent in the same place (`harness/stack.py`: `add_agent_arguments`, `agent_settings`, `build_agent`), so what
the Chat can reach, the others can reach:

| option | meaning |
|---|---|
| `--permission-mode plan\|read_only\|default\|auto` | `plan` changes and runs nothing until you approve a plan (see below); `default` allows edits in the workspace and asks for the rest |
| `--after-plan default\|auto` | the mode the run goes on in when you approve a plan |
| `--allow PATTERN`, `--deny PATTERN` | rules such as `shell_command:pytest*`; a deny always wins |
| `--secret-env NAME[=host,host]` | lets the agent write `{{secret:NAME}}`; with hosts, the value can only be sent to them |
| `--vault-dir`, `--payments-dir` | the Wallet's vault and payment systems (`{{vault:ENTRY.FIELD}}`, `request_payment`) |
| `--browser`, `--browser-headless`, `--browser-private`, `--browser-profile DIR` | the browser tools and how they run |
| `--capabilities` | `find_capability`, `load_capability`, `call_capability`, `analyze_image`: the skills, plugins and MCP servers found on this PC |
| `--lsp` | `diagnostics`, `go_to_definition`, `find_references` when a language server is found |
| `--council` | `ask_council` when a council is set up (the Chat always offers it) |
| `--wiki`, `--wiki-project` | wikibridge tools (`BASALT_WIKIBRIDGE_URL`, `BASALT_WIKIBRIDGE_KEY`) |
| `--skills DIR` | folders with `SKILL.md` skills |
| `--phone`, `--on-behalf-of NAME` | phone tools (`CALLAPICALL_URL`, `CALLAPICALL_TOKEN`); every call asks you |
| `--hooks FILE`, `--workspace-hooks` | your hooks (see "Hooks you write") |
| `--tool-timeout [NAME=]SECONDS` | how long a tool may run before it is stopped; `0` for no limit (see "Time limits") |
| `--context-patterns JSON`, `--context-threshold`, `--end-turn-bias`, `--attachments-dir`, `--outputs-dir`, `--no-memory`, `--no-checkpoints` | as in the Chat |
| `--reference DIR`, `--reference-strategy auto\|cag\|rag\|none` | (headless) reference documents: fixed block, searched passages, or by size |
| `--interactive`, `--ask-server` | (headless) ask on the terminal, or on the server's pages, before risky actions, payments and plans |
| `--max-tokens-budget`, `--timeout-seconds`, `--max-turns` | (headless) stop after this much |

A headless run has nobody to type a second-factor code; a login that needs one fails there. Payments and plans go through the same question as
a risky action (`--interactive`, `--ask-server`); with neither, a payment above the person's waiver is refused and a plan stays a plan.

The run and its file edits appear in the Monitor (tasks and changes) while it works, unless `--no-report`; the reporting
never changes the run, and stops after two failed attempts. An edit made by a shell command (`sed -i`, a script that rewrites
a file) is recorded too: the workspace's text files are compared before and after each command, so it can be reviewed block
by block like the file tools' edits. Binary files, files over 200 000 characters, `.git`, `node_modules` and similar folders
are left out, as are the changes of a command that writes more than 50 files; a workspace of more than 5 000 files or
30 MB of text is not watched.

Exit codes: 0 finished, 1 error, 2 bad usage, 3 turn limit, 4 budget, 5 stuck in a loop, 6 ended on a question for the person
(the answer would be the next message), 7 put aside before it was done, 130 cancelled. With `--json` the result has the stop reason
(`status` is `success` only for a finished run), the changes, token and cache counts, how many calls the permission layer refused
(`permission_denials`, in all and by tool) and the path of the audit log. The Chat's process reports the same count in each
`heartbeat` and `done` (`denied`) and in `closed` (`permission_denials`); the `session_end` hook is given it too.

Data (audit log, checkpoints, memory, approvals) lives in the user data folder, one folder per workspace, never in the
repository; the only thing written in the workspace is `.basalt/artifacts` (full outputs that were shortened).

## Conversations (the Chat's agent)

A conversation is one long-lived process, `python -m harness session`, so a message does not pay for the start (tools,
skills, connections) and the history is kept between messages. The server starts and watches these processes
(`serve/agent_sessions.py`, routes under `/api/agent/sessions`, from this PC and the server's own page only):

| route | what it does |
|---|---|
| `GET /api/agent/sessions` | the conversations, newest first, with their state: `dormant`, `starting`, `idle`, `busy`, `blocked`, `closed` |
| `POST /api/agent/sessions` | creates one: `workspace`, `mode`, `allow`, `ask_only` (answers without tools), `title`, `max_turns` |
| `POST .../<id>/send` | `{"text"}`; a dormant conversation wakes from its saved history; 409 while it is still answering |
| `GET .../<id>/events?after=N` | what happened since event N: `user`, `ready`, `accepted`, `agent` (assistant text, tool start and end), `retrying`, `restarting`, `stalled`, `ask`, `answered`, `done`, `blocked` |
| `GET .../<id>/history` | the saved messages |
| `POST .../<id>/answer` | `{"ask", "value"}`: the person's answer to a confirmation the conversation is waiting for (`asking`); the value must be one of the buttons |
| `POST .../<id>/cancel`, `/resume`, `/close` | stop the turn; go on after a block; end the conversation |
| `POST .../<id>/mode` | `{"mode", "confirm_auto"}`: what the conversation may do without asking, from its next action on; `auto` needs `confirm_auto: true`, as at the start |
| `POST .../<id>/effort` | `{"level"}`: `none`, `low`, `medium`, `high` or empty (the model's own); also a field of the create call |
| `POST .../<id>/favorite`, `/delete` | `{"favorite": true\|false}`: the page lists favorites apart, above the rest (the conversation's place in time does not change); `delete` takes the conversation away for good: its process, saved messages, attached and made files and its own copy of the folder (branches made from it stay) |
| `POST .../<id>/rename`, `/command` | `{"title"}`; `{"name", "args"}`: a slash command the process answers (below), whose answer comes as a `command` event |

The history is saved after every model turn, and a message is on disk before the model sees it. Failures are handled in
two places and never hidden:

* **in the process**: a model call that fails because the server is busy or unreachable (HTTP 0, 408, 425, 429, 5xx) is
  tried again with waits of 2, 5, 15, 30 and 60 seconds, checking whether the model is loaded in between; a refusal that
  waiting cannot change (4xx), a model the server will not load, or retries that ran out end the turn as `blocked`;
* **in the server**: a process that dies, or stops answering its heartbeat, or makes no progress for 20 minutes, is
  started again from its history and the turn goes on (a message it never confirmed is sent again), at most 3 times in
  10 minutes; after that the conversation is `blocked` (`process_unstable`).

A `blocked` conversation waits: `resume` goes on, `close` ends it, a new message also goes on. Nothing is decided for the
person. A tool that cannot be undone is **never run again by a restart**: its start is noted on disk, and if the process
dies while it runs, the next start reports `interrupted_irreversible`, nothing goes on by itself, and the conversation
gets a note that the tool may already have run. A conversation idle for 30 minutes loses its process (it is `dormant`; the
history stays). The processes end with the server. Secrets are masked before the history reaches the disk.

### The mode, the thinking level and the slash commands

The permission mode (*Plan only*, *Read only*, *Ask first*, *Automatic*) can be changed while the conversation lives: a label by the
*New conversation* button says which one is on (*Shift + TAB to change mode: actual …*), **Shift+Tab** in the message box goes to the
next (Ask first, Plan only, Read only, Automatic, and round), and so does pressing the label. Going to Automatic asks the two
confirmations of the risk dialog the first time only (kept in the page's session storage, so until the window is closed); a refusal passes
over it to the next. With no conversation yet, it changes the mode the next one starts
with (the Chat settings' choice); with one, it changes that one only, and the process tells the page (`mode` event). The *Thinking* list
is the conversation's too: a new one starts with its value, a change reaches the process (`effort`), and an opened one sets the list.

A message that begins with `/` and a word is a command (a message that begins with a space is sent as it is; an unknown command stays in
the box). Typing `/` opens a menu: arrows or Tab choose, Enter runs a command that takes no argument. They are the commands of Claude
Code that make sense here, plus Basalt's own:

* **on the page**: `/help`, `/clear` (`/new`, `/reset`), `/model`, `/effort [none|low|medium|high|auto]`, `/mode [plan|read|ask|auto]`, `/plan [what]`
  (plan mode, then the request), `/permissions` and `/settings` (open the Chat settings), `/copy [n]`, `/export` (a text file), `/rename name`,
  `/branch`, `/branches`, `/resume`, `/rewind` (points at the *Back* button), `/stop`, `/diff` (the conversation's copy of the folder),
  `/plans` and `/wallet`;
* **answered by the conversation's process** (`harness/commands.py`, data the page words in its language): `/status`, `/context`,
  `/compact` (the older half of the conversation becomes a summary, only when nothing is under way), `/tools`, `/hooks`, `/mcp`,
  `/memory [search]`, `/permissions` (the mode, the rules, the refusals);
* **requests worded for the model**: `/init` (write AGENTS.md), `/review` and `/security-review` (the changes in the folder), and
  `/research question` (also `/search`), which asks to read the sources themselves and to name the pages it could not open;
* **skills** found on this PC (Settings, Capabilities): `/name text` asks the agent to load the skill with `load_capability` and follow it;
  a skill not yet approved says so instead of running.

Not carried over from Claude Code, on purpose: the ones that belong to its account, terminal or cloud (`/login`, `/usage`, `/theme`,
`/vim`, `/ide`, `/remote-*`, `/install-*`, `/voice`, `/mobile`…), and `/model`'s switching, because the model is the one loaded in
Models.

### The time

The agent always knows the person's local date, time and timezone, and is never asked to look them up: before each request (and each
note that wakes it) the loop puts a line of its own in the conversation, `[harness] The person's local date and time: Monday 2026-10-26
00:52 (UTC+01:00, CET).`, taken from the clock of the PC the harness runs on, and the system prompt says to use it. It is a note
(`note: true`), so the page and the history do not show it and a request still counts as one; it comes after everything already in the
conversation, so the start of the prompt is the same from turn to turn and the server's cache is kept. It costs one short line a request.
With the clock on, each message the loop makes also carries `ts`, its time in seconds, which is saved in the history and never sent to the
model (`INTERNAL_KEYS`); the page draws it at the left of the message: the hour in bold, the date under it, in small type (the tooltip has
the full date and the timezone). A message saved before this has no time, and its column stays empty. `AgentLoop(clock=...)` turns it on
(`build_agent` always does; a loop built by hand has none, as before).

### A message written while the agent works

A conversation is never closed while the agent works. A message that comes meanwhile is looked at before the agent's next model
call (never in the middle of a tool: an action that has begun is finished), and `harness/router.py` sends it one of three ways:

| how | when | what happens |
|---|---|---|
| **steer** | it changes, corrects, adds to or asks about the task in hand; also the default when nothing says otherwise | it goes into that task's conversation and the agent goes on with the new instruction |
| **queue** | a different request that can wait ("when you are done", "later", "not urgent", or the judge says so) | it starts when the task is done, on the same conversation; where the model answers several requests at once (`parallel` in `/v1/status`, a cloud provider) it starts at once, beside the task |
| **urgent** | a different request that cannot wait ("urgent", "asap", "before anything else", or the judge says so) | the task is put aside where it stands; the urgent one is done in a conversation of its own; the first one then goes on, told that it was set aside |

The person's words decide first. A weak word of hurry ("subito", "right now") does not make a request unrelated (it may be part of
the task: "write it right now"): it counts only when the judge says the request is a different one. A strong one ("urgente",
"asap") also stops the model call that is being written, so the message is looked at now; what was half written is dropped, nothing
was done yet. The judge (`--router model`, the default) asks the loaded model for one word and gives up quietly; with `--router rules`
only the words decide. Without a verdict the message is **steered**: the person is talking to the agent, so the likeliest thing is
that they talk about what it is doing, and a steered urgent request is still seen by the agent at once.

The page shows what was decided ("Added to what the agent is doing", "It can wait", "Urgent: the task is put aside") and, for a
request that waits, two buttons: *Do it now* and *Remove*. **Stop means stop**: it ends the task in hand and drops what was put aside
and what was waiting. Requests that waited are kept on disk and, if the process dies, are held for the person to release (`resume`).
An urgent request is not put aside for another urgent one: they go in order.

### The Chat and the planner

A conversation has the planner's tools (`harness/planner_tools.py`), unless it is started `ask_only`: `plan_list`,
`plan_get` and `plan_step_requests` (the call and the wiki question a step prepared) read; `plan_create`, `plan_propose_change`, `plan_mark`, `plan_step_start`, `plan_prepare_call` and `plan_archive` change (`plan_step_start` starts a ready step for the agent: the step is marked in progress and shows in the Tasks panel with its plan, and is marked done or failed after the result is read); `ask_user` asks a question with
buttons. The plans live in the server, in the one store the Plans view uses, and every change made from the Chat names the
Chat as its author in the plan's history.

**Every change to a plan has the person's yes, in every permission mode**, and it is one of two things:

* *their own words*: the call carries a literal quotation (`quote`) of what they asked for. It counts only when it is
  at least 6 characters, is in their latest message, and nothing from outside (a page, a file, a memory, a plan read in this
  turn) came in after that message. It is a brake for the common case, not a proof;
* *a question from the harness*: the question is made from the call's real data (the plan's real title, the real steps, the
  real diff), never from the model's words, and the person answers with a button. The turn waits.

A plan must be read (`plan_get`) before it is changed; the change carries the revision that was read, and a plan that changed
meanwhile is refused until it is read again. A proposed change to a plan's structure is shown to the person as what it adds,
removes and changes, and a no rejects the proposal. With nobody to ask (a headless run) the tool says so plainly and changes
nothing.

`ask_user` ends the turn. The buttons are the question's options (a label and the text sent when it is pressed, at most 300
characters), the person can always write instead, and whatever they press or write is their next message. Permission
questions (a risky shell command, a phone call) are put in the conversation the same way.

The conversation is told about the plans in a short note placed *before* the person's message and only when the plans changed
since the last note (the most recently touched plans first, at most 8), so the server's cache is not disturbed and a claim
about plans never follows a message from outside.

## How a tool call is decided

Every call goes through the same gate, in this order: the arguments are checked against the tool's schema (the model gets
a message saying what to change); pre-tool hooks (dangerous commands are refused even when everything is allowed, and a hook
that fails refuses too); the permission manager; a checkpoint of the workspace before the first change of a turn; the
tool; secrets masked in the result; results of untrusted tools fenced as data; an audit record.

Permissions: plan mode refuses anything that is not read-only; then **deny rules** (matched against every segment of a
shell command and the words inside `cmd /c`, `powershell -c`); tools that always ask (`phone_call`); **allow rules**, which
never cover a command that chains or redirects (`pytest && rm -rf ~` is not `pytest`); then the level: read-only asks for
anything, `default` allows reversible workspace edits and asks for irreversible actions, `auto` allows everything **unless
the Rule of Two is broken**: once the session has read untrusted content (web, MCP, phone, wiki, memory) and can reach
sensitive data (secrets or a vault entry), irreversible actions ask again. Whether the session can reach sensitive data is looked at at each call,
not when the conversation began, so an entry the person adds to the Wallet later counts at once. One tool is outside this check:
`request_payment` carries an allow rule, which skips the level and the Rule of Two, because the payment broker is its only gate (the
person's limit, the waiver, and a question to the person above them), whatever the conversation has read. `write_file` and `edit_file` are reversible, except inside a `.git` folder
(a hook or the config can make git run code later): there the person is asked, once per file, as for an irreversible action (the
level and the Rule of Two decide it, and with nobody to ask it is refused), and the checkpoint before it holds the hooks and the config.

What this is **not**: an OS sandbox. A command runs with your files and network rights, with a scrubbed environment, no
stdin, a time limit and capped output. On Windows each command also runs in its own job object, created before the command
executes anything: 4 GiB of memory and 64 processes for all of it together, and everything it started is killed when it ends
or times out (a server a command leaves running does not survive it). The deny list stops the obvious system-changing tools
wherever they appear as a command word. File and network isolation (a dedicated Windows user, firewall rules, a VM for
untrusted code) is not provided; until it is, `auto` should only be used in a folder you can lose. Hooks, rules and
the secrets check read what a command *says*: a command that builds its address at run time needs a firewall.

Checkpoints are stored outside the workspace and are bounded: they leave out `node_modules`, `.venv`, `venv`, `__pycache__`, `build`,
`dist`, `.basalt` and `.git/objects` (change the list with the `exclude` argument or `BASALT_CHECKPOINT_EXCLUDE`, comma separated),
skip a file over 50 MB with a warning in the log (`BASALT_CHECKPOINT_MAX_FILE_MB`), stop one snapshot at 2 GB, and delete the oldest
snapshots when all together pass 2 GB (`BASALT_CHECKPOINT_MAX_TOTAL_MB`); what a snapshot skipped is listed in its `meta.json`, and a
rewind neither touches nor deletes those files. Of a `.git` folder only the hooks and the config are kept. A rewind takes a safety
snapshot of the current state first (it is never the one rotated away). To go back:
`python -m harness.rewind --workspace D:\proj` lists the checkpoints with what the agent did after each, and `--to ID`
restores the files (`--keep-new` leaves files created since). Restoring files does not undo a command the agent ran, a call
it placed or an MCP tool, so a rewind over any of those lists them and needs `--yes`; the rewind is written in the audit log
and its safety snapshot can be rewound to as well. The audit log
(`audit.jsonl`) is tamper-evident (a hash chain, a head file, secrets masked), not tamper-proof.

**The audit log keeps no file contents.** A tool call leaves one record with the tool, `args_sha256` (the SHA-256 of its arguments after
secrets are masked), `arguments` (a masked preview with every value cut at 60 characters), `permission` (`allowed`, `denied`,
`not_required` with no permission manager, or `not_reached` when something earlier refused it), `outcome` (`ok`, `error`, `refused`, `cancelled`)
and `duration_ms` (how long the tool ran), with the reason for a refusal or an error. Actions: `permission_granted` (written before the tool runs, with
outcome `started`, so a crash leaves a trace), then `tool_executed`, `tool_error` or `tool_cancelled`; `permission_denied`, `tool_blocked`
(a hook or a failed checkpoint), `tool_invalid` and `tool_loop` for calls that were not run; `plan_approved`, `plan_rejected`. Records
written earlier, with the arguments in full, stay in the file and still verify (the chain covers whatever the details hold).
`AuditLedger.verify_integrity()` never raises: a line that cannot be read, a record with a missing field, a broken link, a tampered
record, a damaged head file or a shortened ledger each come back as a reason, and `problems()` lists every one instead of stopping at
the first.

### Plan mode

In plan mode the tools that read work and nothing else does. The model has one more tool, `present_plan(plan)`: the plan is put to the
person like any question the harness asks (a button in the Chat, a prompt in the terminal, the editor's permission dialog in ACP, a page in
the server's approvals), with a note when the agent read content from outside before writing it. A yes ends plan mode and the run goes on, in the
same turn, in the mode the person chose (`--after-plan default|auto`; ACP offers one button for each); `present_plan` goes away with it, and
`session/set_mode` to `plan` brings it back. A no, a Stop, or a run with nobody to ask leaves plan mode on and tells the model so. The approval
and the rejection are in the audit log.

### Stopping and time limits

Stop ends a command that is running: a shell command and everything it started (on Windows the whole job) end within a fraction of a second
after the run is cancelled, instead of when the command finishes. Every tool also has a time limit, 15 minutes unless set otherwise
(`--tool-timeout SECONDS`, or `NAME=SECONDS` for one tool; `0` for none; `ToolRegistry.set_timeout` in code). A tool that runs past it is
stopped and the model is told which tool and how long. The stop is an exception raised in the tool's own thread, so no thread is left running
behind; a tool that is inside one blocking system call stops when that call returns. The clock does not run while the person is being asked
something inside the tool (a plan change, a payment, a code). A shell command keeps its own `timeout` (up to 10 minutes); `transcribe_attachment`,
`run_code`, `spawn_subagent`, `delegate_to_acp_agent` and `ask_a2a_agent` have no default limit, as they wait with a limit of their own or for as long as their task takes.

### Loops

Besides the same call three times in a row, the same error from the same tool twice in a row (the same message, whatever the arguments) adds a
note to the second result telling the model to change what it asks or to take another way; from the third time in a row each one also counts
as a strike, the same strikes that refused identical calls give, and a run with more than two strikes ends as a loop. A model message that is only calls that are cut off or not valid
JSON is sent back with the reason, never taken as the final answer.

### Hooks you write

Hooks are commands (or fixed blocks) that run on events. They are read from `hooks.json` in the user data folder (or the file given with
`--hooks`) and, only if you pass `--workspace-hooks`, from `.basalt/hooks.json` in the workspace: a folder under work can name commands, so it
does not get to without your say. A file that cannot be read, or has a mistake (an unknown key, an event that does not exist), stops the agent
from starting with the reason: a policy that is silently dropped protects nothing.

```json
{"hooks": [
  {"event": "pre_tool", "tool": ["write_file", "edit_file"], "match": {"path": "*.env"}, "action": "block", "reason": "env files are off limits"},
  {"event": "pre_tool", "tool": "shell_command", "command": "python check_command.py", "timeout": 10, "name": "command policy"},
  {"event": "post_tool", "tool": "write_file", "command": "python format_changed.py"},
  {"event": "turn_end", "command": "python notify.py"},
  {"event": "session_start", "command": "echo started"}
]}
```

| key | meaning |
|---|---|
| `event` | `pre_tool`, `post_tool`, `turn_end`, `session_start` or `session_end` |
| `command` | what to run (text, or a list of words), in the workspace, with a scrubbed environment and the job limits of any command; the event is given as JSON on its standard input (`event`, `tool`, `arguments`, and for `post_tool` the `result`, masked; for the other events `turn`, `goal`, `stop_reason`, ...) and in `BASALT_HOOK_EVENT` and `BASALT_HOOK_TOOL` |
| `action` | `block`, instead of a command, only on `pre_tool`: refuses a matching call with `reason`, running nothing |
| `tool` | a pattern or a list of patterns for the tool's name (`*` and `?`); default every tool; `pre_tool` and `post_tool` only |
| `match` | `{"argument": "pattern"}`: all must match the call's arguments (text, case does not matter); `pre_tool` and `post_tool` only |
| `timeout` | seconds, more than 0 and at most 120; default 10 |
| `name` | what the model and the log call the hook |

A `pre_tool` command lets the call pass with exit code 0; it blocks it with exit code 2 (what it printed is the reason) or by printing
`{"decision": "block", "reason": "..."}`. A hook that cannot decide, because it exited with another code, did not finish in time, could not
start or is not allowed to run, blocks the call too, and says so: a check that cannot run never reads as approval. The built-in refusal of
dangerous commands runs first and cannot be turned off by a file. Hooks of the other events cannot change the outcome; their failures are
kept and reported, never raised. **What a hook prints is untrusted**: the model sees it cut to 500 characters, with secrets masked, inside an
`<untrusted_data>` block that it cannot close, with the reason for the block outside it. A fixed `reason` you wrote is shown as it is.

## Secrets, payments and phone calls

- **Secrets**: the model writes `{{secret:NAME}}`; the shell tool fills it in at the last moment and masks the value in
  everything that comes back. A secret without hosts is for local commands only; one with hosts is refused if the command
  names any other host or a network tool without a site. The sites of a command are read from its addresses (every reading of a
  backslash or an `@`, since parsers disagree on them) and, when it runs a network tool, from bare names (`curl evil.example/x`) and
  `user@host`; file names such as `out.txt` are not taken for sites.
- **The vault** (`vault.py`, the **Wallet** view): cards, personal data for bookings, logins and secrets, in one file encrypted
  with AES-256-GCM under the device key (Windows DPAPI wraps the key for this user; code running as the same user can still ask
  Windows for it). The model writes `{{vault:ENTRY.FIELD}}`; the value is put in only where a tool uses it, only toward the
  sites the entry names (none: this PC only; a star: any site), and masked again in what comes back, one-time codes included.
  The list shows which fields are set and the last four digits of a card; the owner can show or copy a value when editing an entry (the page asks the server on that press, nothing the model can call does), generate a password, search and filter, and import the logins exported by Edge, Chrome, Firefox or a password manager as CSV, each one sent only to its own site. `browser_sign_in` fills a login's user name and password into the page's sign-in fields. A conversation that holds a vault
  entry counts as holding a secret for the Rule of Two, decided at each tool call, so an entry added in the page reaches a running conversation.
  What comes back is masked of a value in its forms: as it is, reversed, in the other case, base64, hex, percent-encoded, and a card number
  however its digits are spaced. **A cipher, or a value cut in pieces, cannot be recognised in text**: what keeps the model from deriving
  a value is that it never has one, and a model that can run commands as this same user on this PC can reach more than the vault can
  hide. For that reason a command cannot name the vault, the payment files, the control token or the browser's profile (a fence for a model
  led astray, not a wall), and the real separation is to run the agent as another user. On Linux and macOS there is no DPAPI: the device key
  sits beside the vault, readable only by its owner, so the file is protected from a copy of itself and from other users, not from this user.
  A **backup** (Wallet view) is the entries under a key made from a passphrase the person chooses (scrypt, AES-GCM), to keep or to open on
  another PC; making one and reading a value (`vault_revealed`, by name) are written to the payment ledger.
- **The second factor** is the person's choice for each login: none; a code typed each time (the conversation stops and asks in
  a box whose answer reaches the tool and is kept in neither the conversation nor the history); or the TOTP secret, so that
  Basalt makes the code (RFC 6238) with nobody asked. The last one lets anyone who can use Basalt on this PC sign in: it is
  offered, with that said once, and not refused.
- **Payments**: a payment system is held by a card or a login of the vault (or by a provider's own virtual card, token or
  mandate). The broker (`payments_broker.py`) keeps a state file signed with the device key and a hash-chained ledger under a
  file lock, re-read on every request, so a revocation made in the page applies at once, the server and the conversations'
  processes keep one chain, and any file that fails verification stops all payments. Amounts are exact cents, with every digit kept
  (up to 60, only so that a number cannot fill the memory). The limit is the person's to set, to any amount; until it is set it is 0 and
  every payment asks the person. A waiver (the design's level 4, §4.8.4) lets the agent pay by itself up to a cap per payment
  and a cumulative cap until it expires; above them it asks again. Granting one is the dictated declaration (checked in Italian
  and English against the chosen amount), the release and acceptance dialogs with the name typed again, and an AES-256-GCM
  evidence dossier (needs the `cryptography` package, see `harness/requirements.txt`). The agent can only *ask*
  (`request_payment`, offered when a payment system exists, with the `site` that will take the payment); nothing it can call grants or
  widens a waiver. A waiver may name the only sites it covers. An approved payment has an id and lets the fields of the system's card out
  of the vault, as placeholders, to the site it named, nine times at most and for ten minutes at most, **and only while it still
  stands**: each time a field is released the vault asks the broker, from the signed state and the ledger on disk, whether the waiver is in
  force and nothing was revoked or removed since, so "Stop" in the page closes a card already released, from another process, and also a
  payment the person confirmed by hand. The question to the person is asked with no lock held, so Stop works while it is open. Changing a
  system's currency, credential or card ends its waiver. The person can turn the guard off for a card. **No payment connector moves money by
  itself**: what is paid is paid when the form is sent through the browser tools; the amount is checked when the payment is asked for, and
  what a merchant's form then charges is bounded by the card's own limit, not by this check. The dossier holds the hash of each source
  file of the agent and the server with the version, and the clock's offset from a network time server (one UDP packet; null if none answers);
  it does not hold screenshots of the dialogs, which a page cannot take of itself. Stripe and Google Pay are paid as any web checkout is, in the page, when the person asks the agent to do it: there
  is no connector for them. A Stripe form, hosted by Stripe or embedded in the shop's page, is reached; a Google Pay button needs a browser signed in
  to Google, which is the kept profile above, and its sheet is a window the browser follows. None of these was tried against the real services. Decisions still yours: the default 30-day expiry and a lawyer's review of the three dialogs.
- **The browser** (`browser.py`, `--browser`): Edge or Chrome on this PC, driven over the DevTools protocol, for forms, purchases and
  bookings (`browser_open`, `browser_read`, `browser_click`, `browser_type`, `browser_select`, `browser_press`, `browser_back`).
  A page is read as numbered elements and text, fenced as untrusted. A vault value is typed only if the page's site is one the
  entry names, and a field that holds one is listed as set. Each conversation has its own browser, and the
  browser ends with it, however it ends. By default its profile is thrown away with it; the person can choose to keep one profile, so that
  the sign-ins they make in it (Google, a bank, a shop: **Open the browser to sign in** in the Wallet view) are there in every conversation.
  Only one browser at a time can use that profile; another conversation starts with a fresh one and is told so. Pages on this PC or its
  network (private, loopback, link-local, shared-carrier ranges such as a tailnet) are reached only if the person allowed them: every request of
  a page and of its frames goes through a check before it leaves, so a link, a redirect, a script or an image cannot reach them, and a refused
  one is reported to the model. A name that resolves to another address between the check and the request (DNS rebinding) is not stopped.
  A value from the vault is not typed on a plain `http` page of the internet. The browser's debugging port listens on 127.0.0.1 without a
  password, so a command run as this user can reach the browser and its cookies; this is the same limit as above. It reaches the page, the frames of its own site, the frames of other sites the page
  holds (an embedded Stripe card form: each is a session of its own, and a value typed there goes only to a site the vault entry names), and
  the windows the page opens (a payment sheet), which become the page it works on until they close. Whether the conversations have a
  browser, whether it shows a window, whether it may open pages of this PC and whether it keeps sign-ins is set in the Wallet view, each risky
  choice with a highlighted label and two questions.
- **Phone calls** (`--phone`): `phone_call` always asks, even at the `auto` level and with an allow rule; the call opens by
  saying it is an AI assistant calling for you; what the other person says is untrusted. Tests of real calls are skipped
  and queued in `tests/QUEUE_live_calls.md` until you authorize them (`BASALT_LIVE_CALLS=1`).
- **Recordings** (audio and video attachments): the same callAPIcall client sends a file to the service's analysis routes in
  chunks, follows the job and reads back the transcript (`start_media_analysis`, `wait_media_analysis`, `media_transcript`,
  `format_transcript`). The service runs one job at a time (the client says `busy`), takes only the sound of a video and needs
  ffmpeg; a file it would refuse, or one over the caller's limit, never leaves the machine. The transcript is untrusted text.
  The Chat does not use this yet: the plan is in `piano_chat_planner_modelli.md` (Fase 4b).

## Context, memory, skills and reference documents

- Old tool results are masked only when the context nears its budget, oldest first, with the full text saved and named in the
  placeholder; long outputs keep their head, tail and **every error line** (a line over 2000 characters is cut in the middle and a whole result is held to 40,000 characters, with the
  full text saved and named at the end); JSON is trimmed and HTML reduced to text. The
  system block is byte-stable so the engine's prefix checkpoint is reused; retrieved memories go after the goal as untrusted data.
- `repo_map` lists the source files of a folder with the classes, functions and methods in each (Python through `ast`; JavaScript,
  TypeScript, C, C++, CUDA, Go, Rust and Java through cautious line patterns, so a miss is a missing line, never an invented one), the
  most relevant files first: changed lately (modified time), used by other files (how many mention the names they define), or named
  in `focus`. It stops at a token budget (default 1500, at most 8000) and says how many files it left out; no one file takes the whole
  map. It reads files only, skips dependency, build and hidden folders, and does not use tree-sitter or git.
- `AGENTS.md` (else `CLAUDE.md`, else `GEMINI.md`), searched from the workspace up to the repository root, is part of the prompt.
- Memory is SQLite full-text search per workspace, kept in the user data folder (never in the repository); identifiers such as
  `sm_120` are searchable, and a question whose words are not all in a note still finds it. The model keeps notes itself with
  `remember` (a short note and a category; the same text is kept once, at most 5000 notes), reads them with `recall` and deletes
  one with `forget`. What `recall` returns is untrusted data like the notes that arrive after your message, and none of it goes
  into the system block, so the engine's cached start does not change when a note is added. With `use_memory` off the three tools
  are not offered. Limits, as read in the code: the file is named by the hash of the workspace's full path (case ignored), so a
  conversation working in its own copy of a folder has another memory, and the Chat page, which sends no folder, works in
  `chat-workspace` of the user data folder and shares one memory between its conversations; notes are stored as written (not masked,
  not encrypted); the model alone decides what to keep (the system prompt does not mention the tools); `remember` and `forget` are
  reversible, so they ask nobody at the `default` and `auto` levels; the notes the retrieval adds at the tail stop a quotation from
  confirming a plan change (`untrusted_since_user`) but, unlike a `recall` result, do not set the Rule of Two flag; the search is by
  words (FTS5, no stemming, no embeddings).
- `browse_url` reads a web page with the standard library only: http and https, no user name in the address, no address that is
  not a site on the internet (private, loopback, link-local, carrier-grade NAT and multicast ranges are refused) unless you allowed
  pages on this PC in the browser's settings. The address a name resolves to is checked at the moment of connecting and again at
  every redirect (at most 5); the page is capped at 2 MB and 20 seconds, only text is read, and HTML becomes Markdown. The result
  is untrusted. The page reaches the model as a short header and lines of text with the bare links of menus left out, not as one
  long line (a line over 2,000 characters is cut in the middle, which once left the model only a page's menu and footer).
  `search_web` uses **DuckDuckGo** unless you set another engine: the tool reads the results page of `html.duckduckgo.com` (title,
  address, snippet; advertisements dropped), with no key, so the **words of each search go to DuckDuckGo**. The address is checked like
  any page's. It is a page meant for people, not an API: DuckDuckGo may ask for a check after many searches (the tool says so) or change
  the page. `BASALT_WEB_SEARCH=off` removes it. The other engines add to it and are the default only when DuckDuckGo is off: a search tool of a connected MCP server, or a
  provider of `harness/tools/search/providers.json` for which you have a key (its environment variable, or a vault entry named
  after the provider, e.g. `tavily`, with the key in its `value` field). The provider's server is started when the first search is
  made. The Exa endpoint is reached with the key in an `x-api-key` header, which has not been tried against the live service.
- Skills (`SKILL.md`) are listed in a stable catalog and loaded on request; a skill needs your approval, stored with the
  hash of the text you read, and any change withdraws it.
- Reference documents are offered as a fixed block after the project rules (**cag**: the prefix is reused every turn, but a
  change re-reads everything and the block crowds the context) or searched (**rag**: small context, passages arrive fenced as
  untrusted data). `auto` picks by size; the threshold is a starting point. `python -m harness.eval --set knowledge` asks the
  same questions with no documents, with rag and with cag and reports accuracy, tokens and cache use for each.

## MCP and ACP

- The MCP client speaks stdio and Streamable HTTP and two eras, detected per server. It tries the stateless revision
  (2026-07-28: every request carries its version, client info and capabilities in `_meta`, no session, no handshake) first,
  with `server/discover` on stdio and with the first request on HTTP, where it mirrors `MCP-Protocol-Version`, `Mcp-Method`,
  `Mcp-Name` and the `Mcp-Param-*` headers of annotated tool parameters (values that are not plain ASCII go as base64, and
  tools whose annotations are invalid are left out). A recognized stateless error (`-32020`..`-32022`, or an unknown method on
  a 404) means a stateless server; anything else, including the 400 an older server gives for a missing session, means an
  older one, which is spoken to with `initialize` in revisions 2025-11-25 down to 2024-11-05. `modern=False` skips the probe.
  A server that asks for sampling, elicitation or roots is refused with a clear error, because the client declares none
  of them; a result that only carries `requestState` is retried with that state echoed back and a new id. Pagination,
  `isError`, a drained stderr and a scrubbed environment are as before. Server tools are always irreversible and untrusted
  whatever they claim. `MCPHub` holds servers of both eras and puts their tools in the registry the way Claude Code does: one by one
  (`server__tool`) while there are at most 24, and behind two tools, `mcp_search` (their definitions, searched by words) and `mcp_call`,
  when there are more, so the prompt stays short. OAuth for remote servers is implemented (see "Signing in to remote MCP servers").
  **Not implemented**: `subscriptions/listen`. Two details were inferred, not read: that `requestState` sits in
  the retried request's `params`, and the shape of the `notifications/cancelled` sent on a stdio timeout.
- The ACP agent follows protocol version 1 over stdio: `initialize`, `session/new`, `session/prompt`, `session/cancel`,
  `session/set_mode`, `session/update` (message and thought text, tool calls, edits as `diff` content) and
  `session/request_permission` toward the editor. It edits files itself rather than through the editor's `fs/*`, offers no
  `session/load` and no authentication, and reads text only. It is built like the command line's agent and takes the same options
  (`python -m harness acp --capabilities --lsp --vault-dir ...`), and the `mcpServers` the editor sends with `session/new` (a program to
  start, or a Streamable HTTP address; SSE is refused) are connected for that session: their tools are always irreversible and untrusted,
  so they ask like any action that cannot be undone, and a server that cannot be started is reported on stderr and left out. Plan mode
  asks the editor to approve the plan, with a button for each mode it can go on in.

## The planner

`harness/planner/` holds the plan graph, the plan card, the event filter (System 1), re-planning by the local model (System 2)
and the bridge the page talks to.

- **Long-term plans against short ones** (`horizon.py`, `alignment.py`): a *constant* plan is a long-term one, with aims, an optional
  budget (per month or in all), the periods it has already taken and how often it must be looked at again (at most every 30 days); an
  *occasional* plan has dates and a cost. The page and the Chat's `plan_create`/`plan_set_profile` write them. Two findings, kept apart
  because they are not equally sure: a **conflict** is arithmetic (days inside a period a long-term plan has taken; costs, added per
  month or in all, over a budget) and is found for every plan at once, announced on the plan's stream when a plan is made or its
  dates, cost or kind change, and shown in the list, in the Plans view and in the summary the Chat reads; a **check** is the local
  model's reading of what the aims and the plan mean together (a ski weekend against "heal the knee"), asked only on request
  (`plan_check` with `with_model`, or the button), kept with the plan, said to be out of date once the plan changes, and never stated
  as fact. Plans are not compared for overlap with each other, only short ones with long ones. Not done: aims that are numbers to
  measure (KPIs, habits), a review that records what was found, and reading conflicts between two long-term plans by arithmetic.
- A plan is a graph of action, sensor, decision and merge nodes with shared values. Decisions route by simple conditions
  (`len(guests) >= 2`; never code); merges wait for their inputs; a task the software cannot do is offered to you with an
  alternative and, if you decline, becomes yours and blocks what depends on it. You can rewind a step. A rewind says first which recorded actions of those steps stay done (see *Branches, going back and comparing*).
- Incoming events wait in a buffer (10 events or 5 minutes after the first) and are judged by the filter. **The cloud filter
  (TypeSafe Jev) is off.** It needs your consent in Settings *and* a key in the server's `TYPESAFE_API_KEY`; only the plan
  fields you tick are sent. People's names are replaced by labels (a full name also by its first name and its surname on their
  own, case-insensitively; common words such as "amico" or "nonna" are not taken for names), and e-mail addresses and phone
  numbers are taken out of the events and the card; one switch ("replace names") controls both. **By default the participants'
  health, allergies and personal constraints (`profilo_e_vincoli_partecipanti`) are not sent**; they go only when you tick that
  field in Settings (a highlighted label and two questions), and the filter judges events that depend on them less well without it. The
  answer says whether it came from the cloud, from the local keyword filter or from a failure. When the filter says wake, the local model proposes two or three alternatives
  as diffs; only the ones that apply cleanly are shown, and nothing changes until you approve.
- Every change to a plan raises its **revision** and adds a line to its **history** that says who made it (`user`, `chat`,
  `planner` or `system`; the sender states it, so it is a note and not proof of identity). An edit that writes straight into
  the plan (`plan:update_node`, `plan:mark`, `plan:rewind`, `plan:update_meta`, `plan:fallback_response`) may carry
  `base_rev`, the revision the sender last read; if the plan has moved on it is refused with `stale: true` and nothing
  changes. A proposal needs no revision: approving it fails, and marks it stale, when its diff no longer applies.
  `plan:update_meta` changes a plan's title, its kind (`constant` or `occasional`) and its card, all or nothing.
- A plan is archived, not erased: `POST /api/plans/<id>/archive` moves it out of the list and `POST /api/plans/<id>/restore`
  brings it back with its history; an archived plan keeps its id. The Plans page shows the history with who made each
  change, edits a plan's details, archives it after two presses and lists the archived plans with *Restore*.
- **The gate** (`gate_mode` in Settings): `or` (the default) wakes the local model when the blocker probability is above 0.80, *or* the whole batch
  is affected, *or* the strongest event is serious or critical; `and` also needs a serious/critical impact or a whole-batch scope together with
  the probability. `and` leaves asleep the events the filter rates as relevant but minor (a price rise on goods already bought, a feast of
  bovines for a guest allergic to ovines) and also a serious event it rates below 0.80.
- **One filter per process, with its numbers**: the gatekeeper is one object for the whole server, so its telemetry adds up (judgements, answered
  by the cloud, by the local filter by choice, by the local filter because the cloud failed, tokens, wakes). `GET /api/plans/telemetry` returns it,
  and the Plans view shows a line in *Judge events*. Re-planning runs one plan at a time: a second triage that wakes the model while it is still at
  work on that plan is judged but starts nothing, and says `replan_state: "in_progress"` (`started`, `in_progress`, `unavailable`, `not_needed`,
  `not_asked`); `GET /api/plans/<id>` carries `replanning`. The ids `events` and `telemetry` cannot be plan ids.
- **Steps**: a step with a `retry_policy` (`max_attempts` 1-10, the first try counts; `wait_seconds`; `backoff`) is not marked failed by the first
  failure: it goes back to pending, is not ready until the wait is over, and fails for good on the last try (`plan:mark` answers `retry`, the Chat's
  `plan_mark` says so). A **sensor** step has `conditions: {"when": <expression>, "set": {name: value}}`; once its turn has come (its parents are done)
  it resolves on the first event whose text (in lower case, as `text`), `source` or `at` satisfy `when` (the plan's values can be used too), and writes
  `set` into the plan's values (a text that starts with `=` is an expression, e.g. `"=text"`). A sensor whose condition cannot be judged waits for the
  next event and shows why. A step also carries its `position` on the canvas, an optional prepared `call_request` and a `wiki_query` (below).
  Conditions are bounded (lists and texts up to 10,000, whole numbers up to 256 bits, powers up to 64) and everything that goes wrong is a plan error.
- **Events from the world**: `POST /api/plans/events` takes `{"text", "source", "at"?, "plan_id"?}` or `{"events": [...]}` (up to 100). Each event goes to
  the sensors of the plan it names, or of every plan, always; with the event pump on it also goes into that plan's buffer (only plans with a card),
  and the pump thread judges a buffer when it holds 10 events or 5 minutes have passed since the first, tells the plan's viewers (`plan:triage` with
  `via: "pump"`) and, when the filter says wake, starts the local model, which only prepares proposals. **The pump is off** until it is turned on in
  Settings (`event_pump`); it ends with the server (`atexit`) and when it is turned off, and a pump saved as on starts again the first time the planner
  is used after a restart (not at server start). The read-only **sources** (`sources` in Settings, up to 10) poll on their own threads, with the standard library only:
  an RSS or Atom feed (the first look only learns what is there, a DTD is refused), a mailbox over IMAP (opened with EXAMINE, headers fetched with
  `BODY.PEEK`, only sender and subject, never the body; the login is a `login` entry of the Wallet, read when needed) and a Telegram bot (long polling
  of `getUpdates`, the token a `secret` entry, only the chats in `allowed_chat_ids`; it never sends anything). Settings hold the names of the Wallet
  entries, never a password or a token, and an error shown for a source never contains one. SMS is not done: it needs a provider or the modem.
- **A step and a call, or the wiki**: a step can carry `call_request` `{number, reason, text}` prepared by the plan (`plan_prepare_call`, with the
  person's yes). Nothing places it: `plan_step_requests` shows it, and the Chat places the call only with `phone_call`, which asks every time. A step's
  `wiki_query` is shown with `wiki_context` as the way to ask when the conversation has the wiki tools (`--wiki`). When `phone_call` cannot place
  a call the message says why: the service is not running, it refused the token, a call is already in progress (HTTP 409), or it refused the call
  with its reason; and when the call **may have started** (no answer in time, the connection broke, a server error) the status is `unknown`, the
  tool says so and tells the model to ask the user whether the phone rang. A placement is never sent twice, whatever happened.
- **Canvas**: a step can be moved with the pointer, with the arrow keys when it is selected, or with the *Move* buttons of its panel; the place is saved in
  the step (`position`) through `plan:update_node`, also for a finished step, and a step without one keeps the automatic layout.
- **Going back over a step** that recorded actions which cannot be undone (a call, a payment, a command) first asks `plan:rewind_impact`; the page lists
  what would stay done and goes back only with *Rewind anyway* (`plan:rewind` with `accept_irreversible`).
- API: `GET/POST /api/plans` (`GET /api/plans?archived=1` lists the archived ones), `GET /api/plans/telemetry`, `POST /api/plans/events`, `GET /api/plans/<id>`,
  `POST /api/plans/<id>/events`, `POST /api/plans/<id>/triage`, `POST /api/plans/<id>/archive` and `/restore`,
  `GET /api/plans/<id>/events?after=N` and `/stream` (server-sent events), `GET/POST /api/planner/settings`.

## Attached files and files the agent makes

In the conversations Chat the person attaches any file (button, drag and drop); each is uploaded to this conversation
(`POST /api/attachments?session=&name=`, from this PC and the server's own page only) and kept as it came, with a card (what it is,
decided by its first bytes, size, SHA-256). Limits are the normal inference settings: the model's context window and the policy for
a full context; nothing else caps a file except the server's upload limit.

- **How the model sees them** (`harness/attachments.py`): the documents come first, each in a `<document>` block with its source,
  type and what part is shown, the text inside `<untrusted_data>`, and the person's question last. When the documents are long
  (over 20,000 tokens) the model is asked to copy the passages that matter into `<quotes>` before answering. A file that does not
  fit in 40% of the window shows its start and says so; a table shows its columns, row count and first rows. The model reads the
  rest with `read_attachment` (line ranges), `search_attachment` (BM25, five passages with line numbers) and `list_attachments`.
- **Formats**: text and code; csv, tsv, json; Word, Excel and PowerPoint (read with the standard library, no extra package); PDF (pypdf or
  PyMuPDF when installed; otherwise the manifest says no reader is installed and the model asks the person to approve a PDF skill or install
  one, and a scan with no text layer says it needs OCR); images (`analyze_image` with `attachment:<id>`); audio and video
  (`transcribe_attachment` through callAPIcall, one recording at a time, the job kept so a second call picks it up).
- **What a file cannot do**: what the person typed is all that can confirm a change to a plan or route a message (urgent, queued, steering); a
  quote taken from a file never does. Text in a file cannot close its own fence.
- **Files the agent makes**: `write_output_file` (text) and `publish_output_file` (a file made in the workspace) store it for the person, open it
  again and check it (JSON parses, CSV rows have the same number of columns, Office files open, an image opens). Only a file that passes gets
  its button; the Chat's "Save" button asks for a place with the system's Save as window when the browser has one, and downloads otherwise. Inside
  the Windows app the page starts a normal download and the app opens its own Save as window with the file's name (`DownloadStarting`), then tells
  the page how it ended, so the status line says saved only when the file was written.

Not done: audio and video for a service that is not running (it
says so and keeps the file), and the service's own summary of a recording (not used).

## Capabilities: skills, plugins and MCP servers found on this PC

A conversation in the Chat can look up skills, plugins and MCP servers without carrying a list of them in its prompt
(`harness/capabilities.py`). On a PC with 193 skills and 50 servers that list would be about 15,800 tokens on every call; the tree's
top and the four tools cost about 400.

- **Discovery only reads.** Sources: `~/.claude/skills`, the workspace's `.claude/skills` and `.agents/skills`, the plugins in
  `~/.claude/plugins` (their skills and `.mcp.json`), `~/.claude.json`, the project's `.mcp.json` and
  `%APPDATA%\Claude\claude_desktop_config.json`. It keeps names, descriptions, tags and the command of a server with anything that
  looks like a key blanked out; it never reads a server's environment variables. A folder named `*.disabled` is off.
- **Nothing found is active.** An entry is *to approve* until the person opens it in Settings, reads its text (a skill) or its command
  (a server) and approves; the approval is tied to the hash they were shown, and a file that changes afterwards is *to approve* again.
- **The tree**: vision (analysis, generation), documents, data, web, code, audio and video, knowledge, communication, other. A declared
  `tags`/`category` decides; else the words of the description; the person's correction wins and is kept.
- **Tools**: `find_capability` (local FTS5/BM25, five results at most, results are untrusted text), `load_capability` (the text of an
  approved skill, read again and checked against its hash; it is the text the person approved, so it is not fenced as outside content
  and does not count towards the Rule of Two), `call_capability` (a tool of an approved MCP server; it asks the person
  like any action that cannot be undone; the server is started with no environment from the config). The prompt holds one line:
  the roots and how many entries each has.
- **Visual analysis** (Settings): the loaded model's own vision, an approved skill or MCP server from the vision/analysis branch, or
  none. `analyze_image(path, question, crop)` crops and enlarges a region for fine detail, limits the image's size, and says so
  when the model's vision is off instead of pretending.
- `skills.md` in the capabilities folder is generated from the index, for reading.

Not done: a second, embedding-based search pass, and a server that needs environment variables or a remote address
(these are listed but not started from here).

## Finding a missing capability online

When a capability is missing that usually exists as an MCP server or a skill, the agent can look for one online, show the best
candidates to the person and install the one they choose (`harness/discovery/`). The tool is `find_online_capability(need,
look_anyway)`; it exists in a conversation that has the capability index (`--capabilities`), and `find_capability` points to it when it
finds nothing. The base prompt says to try it before working around the gap with the shell.

**What the tool does.** It first looks at what is already here: a tool the agent has (for example `search_web`) or an entry of the index
whose name and description match most words of the need; if one does, that is the answer and nothing goes online (`look_anyway=true`
searches anyway, for a better one). Otherwise it asks the sources that are turned on, scores the candidates and, by default, puts the
best five (at most) to the person in the Chat as a question of kind `capability_choice`: for each, name, source, version, score, the
reasons in a line, the warnings, the command that would run and the keys it needs, with the buttons *Install N*, *Other: I will tell
you* (a sentence of up to 500 characters, sent as `other:<sentence>` and returned to the model as the person's words) and *Do not
install*. After a choice it installs and tells the model the id (`mcp:<name>` or `skill:<name>`) and that `call_capability` (with an
empty `tool` to list the server's tools) or `load_capability` uses it; it does not start the server itself. Without a way to ask
(no Chat), nothing is installed and the options come back as text. The tool is registered as reversible, so plan mode refuses it.
The model gives only the need: the tool has no argument that names a package, a command or an address.

**What goes onto the network.** Only GET requests, with the User-Agent `basalt-capability-search/1.0`, a 12 s timeout, a cap on the
bytes read (2 MB; 4 MB for an npm package record, 3 MB for PyPI), no cookie, no credential, and a redirect is followed only inside the
same host. The need (the words the model wrote, at most 300 characters; the tool asks for no secrets and no personal data, and the call is
shown in the Chat with its arguments) appears in the address of the searches:
the MCP registry `registry.modelcontextprotocol.io/v0.1/servers?search=<word>` (up to three words of the need, one request each), npm
`registry.npmjs.org/-/v1/search?text=<words> mcp`, and GitHub `api.github.com/search/repositories?q=<two words> topic:agent-skills` (and
`topic:claude-skills`). For each candidate it then asks by name: npm (`/<package>/<version>`, `/<package>`, and the downloads at
`api.npmjs.org`), PyPI (`pypi.org/pypi/<package>/json`), GitHub (`/repos/<owner>/<repo>`, at most eight per search; for skills
`/commits/<branch>` and `/git/trees/<commit>?recursive=1`) and `raw.githubusercontent.com` for `SKILL.md` and for the
`.claude-plugin/marketplace.json` of the marketplaces the person lists. The sources are turned on or off one by one in Settings; a turned
off source is not asked and its candidates are not offered (the npm and PyPI switches also stop installing from them). The optional
GitHub token is read from the vault (a secret entry named `github`, field `value`), sent only to the GitHub API address, and raises
the limit of 60 requests an hour for unidentified requests; when the limit is reached the answer says so and when it resets, and the
candidates come back without stars and activity (the warning says so) instead of failing.

**The score** (`harness/discovery/scoring.py`) is 0 to 100. It measures popularity and upkeep, **not safety**: a popular, well kept
package can still do harm, and numbers such as stars and downloads can be inflated. The parts, with their default weights: weekly
downloads (20, logarithmic, the whole share at 100,000), age of the last release (20), releases in the last year (10), stars (10, log),
forks (5, log), open issues against the project's size (5), a declared licence (5), a publisher the registry verified (10; for a name
`io.github.<owner>/...` it must match the repository's owner), age (5) and ratings (10, where a source has them; none does today). A
fact the source does not give is left out and the others are weighed among themselves; a fact that is absent (no repository, no
licence) counts as zero; an archived or deprecated package scores 0. The card shows the part each played. The warnings, in plain words:
not updated for more than a year, few downloads, no repository, a name almost equal to a very well known one (a fixed list), keys or
settings it asks for, install scripts (not run), it is a program that runs on this PC, a remote service, a skill whose folder holds
scripts, archived, deprecated, no licence, GitHub not asked. The weights, the minimum score and the sources are in Settings,
Capabilities, *Search online*, and in `discovery.json`.

**What is downloaded, and what is never run.**
- *npm*: `npm install --prefix <user data>/capabilities/mcp/<name> <package>@<exact version> --ignore-scripts --no-audit --no-fund`.
  No script of any package runs at install (a package that needs one may not work). The `integrity` npm wrote in the lock file is
  compared with the one the registry published for that release; a different hash deletes the folder. The server is started from the
  installed program in `node_modules/.bin`, never through `npx -y`.
- *PyPI*: the release is downloaded as a wheel only (`pip download --no-deps --only-binary=:all:`), its sha256 is compared with the ones
  PyPI lists for the release, then it is installed with `uv pip install --no-build` (if `uv` is on the PC) or `pip install
  --only-binary=:all:` into its own folder, so no build script runs. Its dependencies come from the index without a hash check of
  their own. Without pip the hash is not checked and the record says so. A package with no wheel is not installed.
- *A remote server*: nothing is downloaded; the address is recorded. A remote server that needs a key in a header is not offered
  (Basalt signs in to remote servers with OAuth, in Settings).
- *A skill*: only its folder, from the commit that was shown (up to 100 files and 5 MB), file by file; each file's git hash is
  compared with the one in the commit's tree. A skill's text is instructions for the model; the scripts in its folder are files, which
  the model could later run only through the shell tool under the usual permissions. From a plugin marketplace only skill folders are
  taken, not the plugin's commands, hooks or servers.
- Candidates that would need an argument Basalt cannot fill, another kind of package (OCI, NuGet, MCPB) or a missing `npm`, `uv` or
  `pip` are not offered, and the answer names what is missing.

**Registration.** The entry is written to `installed.json` as installed by Basalt (origin `basalt`) and approved with the hash of
exactly what was shown: the start command or the address, or the text of `SKILL.md`. It shows in Settings, Capabilities, can be turned
off or moved like any other, and `find_capability` finds it at once. Using it is still a `call_capability`, which asks like any
action that cannot be undone.

**Keys.** The key a server asks for is a secret entry in the person's vault named like the variable (`TAVILY_API_KEY`), field `value`.
When the server starts, that one variable is read from the vault, handed to the new process and forgotten: it is not written to any
file and the model never sees it (and the output is masked like any vault value). A required key that is not in the vault stops the
start with a message naming the entry to create.

**Installing without asking.** *Install the best one by itself when its score is above the minimum* is off by default. Turning it
on shows the highlighted risk label and asks twice; from then on the first candidate whose score reaches the minimum is installed at
once, and the result in the Chat says that this was a standing instruction.

**Where the files are** (the capabilities folder of the user data folder): `discovery.json` (the settings; `base_urls` there points the
sources at other addresses and is not written by the page), `installed.json` (one record per install: source, version, score, command,
integrity, the variables and their vault entries), `mcp/<name>/`, `skills/<name>/`, `approved.json`. **Removing**: Settings,
Capabilities, *Search online*, *Remove* (it asks for a second press); it deletes the folder, the record and the approval. Doing it by hand
is the same three things. Both installers are told to install into that folder; their own download caches stay where npm and pip keep them.

Not done: no update of what was installed; no download counts for PyPI and no ratings from any source; the installed server's tools
are not listed by the install; the score does not read the code or any security database.

## Context patterns

Six ways to shape what the model reads, chosen in the Chat settings (each with a checkbox, a strategy and a description with
four parts: what it does, advantages, drawbacks, when to use it, and what it does to the server's cache). They apply to the Chat's
conversations with the agent and are kept in the preset under `context_patterns`; other programs that use the server pass only
`policy` and `threshold`. The catalog is `GET /api/context/patterns` and comes from `harness/context_patterns.py`.

Order: injection, rewriting, pruning, summary; versioning wraps them and a branch is an operation on a conversation.

| Pattern | Strategies | Default |
|---|---|---|
| Injection | fixed prefix, retrieval at the end, on request | on, at the end |
| Rewriting | fixed template (the request's words stay inside it, with the limits found in it) | off |
| Pruning | mask old results, strip noise, oldest first, by relevance (BM25) | on, mask old results |
| Summary | extract (no model), structured by the model, incremental | off |
| Branches | copy at a point (`POST /api/agent/sessions/<id>/branch`) | off |
| Versioning | snapshot per turn, hash-chained (`GET .../snapshots`, `.../diff?a=&b=`) | off |

What pruning or a summary takes out of the model's view is kept in the conversation's folder (`archive.jsonl`) so the person's
own record of it stays whole; a request the model saw in the template is shown as it was written. Strategies that need the
model (structured and incremental summaries) use the provider chosen for the compaction function and fall back to the extract
when it fails.

Going back: each request of the person in the conversations Chat has a "Back to here" button (two presses); the conversation returns to just after
that request, a copy of what came later is kept in its folder, and the files the agent changed are not touched (the Changes panel undoes them). Actions that cannot be undone are listed first and need an explicit yes (see *Branches, going back and comparing*).
A plan can be copied as a Mermaid diagram from the Plans view (`GET /api/plans/<id>/mermaid`).

Not done: a rewriting that the model proposes and the person approves, candidates written in parallel, named versions of
the system prompt, and the measure that picks the best strategy (it needs the real model).

## Branches, going back and comparing

A conversation's history is a list of messages, so the tree of branches is made from what the server records: a branch keeps
`branched_from` (the conversation and the request it left from) and a conversation that went back keeps a `rewinds` list (the request
it returned to, where it was, when, the file with the copy of what was taken off, and the actions that stayed done). Records written
before these fields existed have none and show as plain conversations.

- **Tree of branches.** `GET /api/agent/sessions/tree` places every conversation under the one it was branched from, in the order of the
  requests they leave from; one whose parent is gone is a root of its own. The Chat's **Branches** button opens a dialog with it as an
  indented list (`tree`, `treeitem`, `group`; one tab stop, arrows walk it, Right and Left open and close a group, Enter or a click opens
  the conversation). The line says the state, the request it was branched after and any going back, in words. The page has one dark
  theme, so contrast is checked on that.
- **What stays done.** Going back, from the conversation or the plan, does not undo the world. Before a conversation goes back,
  `GET .../<id>/back-impact?turn=N` lists the tool calls after that request: those that changed files in the conversation's copy
  (`reversible`: the Changes panel puts them back) and those that stay (`not_undone`: a command, a call, a payment, a browser action,
  an MCP tool, a tool whose effect is not known, a file outside the copy). A call the agent refused or that was cancelled is not
  counted. The class of each tool comes from what it declared when the conversation used it (kept in `tool_effects.json`), else from
  the table in `harness/rewind.py`. `POST .../<id>/back` refuses (`needs_yes`, 409) while any call stays, unless the body has
  `accept_irreversible: true`; the page shows the list and asks for one more press. The list is kept in the `rewinds` mark and the
  `went_back` event as still done: nothing pretends to have undone it.
- **A plan's steps.** While a conversation works on a plan's step (between `plan_step_start` and `plan_mark` to completed, failed or
  skipped), the manager writes each reversible or irreversible tool call that succeeded onto the step (`plan:note_effect`, in the step's
  `state_variables.effects`). `plan:rewind_impact` lists what a rewind from a step would reset and which of those actions stay; `plan:rewind`
  is refused, naming them, until it carries `accept_irreversible: true`, and the actions stay in the step's history entry as `still_done`.
  Steps done by hand, or by a conversation that was not watched, record nothing. The Plans page does not yet ask for the confirmation, so
  from there a rewind over a recorded irreversible action shows the refusal.
- **Run both and compare.** `POST .../<id>/compare` with `{turn, variants: [{text, mode?, max_turns?, ask_only?, end_turn_bias?, label?}, {...}]}`
  branches the conversation twice from that request and sends each branch its message, the first and then the second, never together
  (there is one GPU; two comparisons also wait for each other). Automatic mode is not offered. `GET /api/agent/sessions/compares/<id>`
  returns both answers, a diff of them and, when both branches worked in copies of the folder, the files each changed with a diff of
  each file between the two; without copies there is no file comparison. A side that asks something or is blocked is left for the
  person to answer from its branch. The list is kept across restarts (a comparison that was running is marked interrupted).
- **Files the agent made.** The same dialog lists them (`GET /api/outputs?session=<id>`) and previews Markdown, text, code, images and a
  page. A page runs in a frame with an empty `sandbox` (no scripts, forms, popups or origin) and a policy that allows no network
  and only `data:` images. A diagram (`.mmd`) is shown as its source: the app has no diagram renderer. Accepting or undoing a change
  block by block is the Changes view's job, and was already there.

Not done: the node canvas (§4.3) and its merge node, a cost estimate of re-reading the prefix when switching branch, and a diagram
renderer.

## The planner wakes the conversation

In a quiet moment (nothing running, nothing waiting, no open question) the session looks at the plans every 15 seconds
(`--watch-plans`). For what is new and needs the person (a change proposed by the planner and not by the Chat, a step the software
cannot do, a step that failed) it starts a turn with a note that begins `[planner]`. A note is announced once, even after a restart,
and never interrupts a task. It is **not** the person's voice: nothing in it can be quoted to confirm a change to a plan, and what
the model then does about a plan still asks the person first. It works while the conversation's process is alive (an idle
conversation sleeps and is not woken).

## The check on an answer

Before an answer is handed over, `harness/pipeline.py` measures it against the request. The request's *card* is made by rules,
with no model call: a number of words, characters, lines or list items, a language, JSON, a word to leave out; and, apart,
qualitative constraints (a tone, an audience, "no jargon"). Modes (`--pipeline`, default `full`):

- `off`: nothing is checked.
- `light`: what can be measured is measured; no extra model call is ever made.
- `full`: as `light`, and when the card has qualitative constraints and the measurable ones pass, a judge answers yes or no for
  each (at most three), through the provider chosen for it on the Providers page (the loaded model, or Jev).

A failed answer gets **one** second try: the agent is told the list of what was not respected and writes it again. If the second
answer still fails, the Chat says which points are unmet. A request with no constraint costs nothing, and the second answer is
measured but not judged again. A judge that cannot tell does not block an answer. The Chat shows each check as a note, with the
calls and seconds it took in the event (`verify`). The judge can be wrong either way and a second try can be worse, so
the mode is meant to be compared on the same tasks with `harness.eval` before it is relied on.

## Providers: which model each function uses

Basalt's own functions (the Chat, the agent's loop, the planner, the judges) can each use a different model: the one loaded in
this Basalt, another one on this PC, a cloud service, a command-line agent, or Jev for judging. The choice is made on the
Models page, sub-tab Providers; other programs that use this server still see only the loaded model.

- `harness/llmswitch/` is the vendored llmswitch package (see its `UPSTREAM.md`); `harness/llm/` is Basalt's layer over it.
- Keys and tokens are kept in the Windows credential manager (the registry file holds only a reference). The page can send
  a key but is never told it back.
- A function that handles personal data (`harness/llm/functions.py`) cannot be pointed at a provider outside this PC unless
  the person says yes for that function; taking the yes back puts the function on the loaded model again. The same check runs
  at every call, so a registry edited by hand does not get past it.
- A provider can have a cap of tokens a day; when it is used up the call goes on with the loaded model. A fall-back goes
  only to a route that is as local as, or more local than, the one that failed.
- Command-line agents answer text only: they are not offered to functions that need tools.
- **Jev** does not chat: it answers typed questions. `harness/llm/judge.py` asks it a *choice* question with a short excerpt
  (at most 1,500 characters), over https only. If it fails, is over its cap or has no key, the loaded model judges instead.
- The session's router judge (steer, queue or urgent for a message written while the agent works) and the planner's triage
  take their provider from here.

Routes: `GET /api/llm`; `POST /api/llm/providers`, `/providers/<id>`, `/providers/<id>/remove`, `/providers/<id>/test`,
`/assign`, `/consent`, `/import`. Changes are accepted only from this PC and from the server's own page.

**Reasoning levels.** A function or provider can say how much to reason with one of seven levels (or inherit). Each kind of provider
gets them in its own words (`reasoning_effort` for OpenAI-like servers, a budget for NIM and Claude), never two spellings at once. This
server takes all seven and maps them: `none` and `minimal` turn thinking off, `low` and `medium` stay, `high`, `xhigh` and `max` are the
highest level the model has.

**Loading Qwen for a function.** A route to this server may name one of the Loader's load profiles (`harness/llm/qwen_loader.py`):
a model already loaded is used as it is (a profile is never applied over it: a reload loses the conversation); one that is loading is
waited for; with nothing loaded the route's profile is loaded, and a server that loads by itself on the first request is left to do so
unless the route names a profile; if the Loader refuses (another program holds the GPU memory, a load is running) its words are the
answer. Nothing is ever unloaded from here.

**The Chat's policy for a full context** (stop, sliding window, compact) is the same thing as two context patterns: a conversation
started with `rolling` prunes oldest turns first and one started with `compact` writes a structured summary, at the share of the window
the threshold says. A pattern the person chose in the settings is not overridden.

Not done: reloading a model with another profile when a function wants one (it needs the engine to be idle, which the server does not report yet),
and the spending report per function.

## A conversation's own copy of the folder

Two conversations in one folder can step on each other's files, and an edit is otherwise applied as soon as it is made. In a folder under git
a conversation can work in a copy of its own (`harness/worktrees.py`): a git worktree with its own branch `basalt/<id>`, kept in the user data
folder, started from the folder's last commit plus the changes not yet committed to tracked files. What the conversation changes stays there.

- **When**: `worktree` when a conversation is made is `auto` (the default: a copy when another open conversation already works in that folder),
  `always` (the server accepts it; the Chat page no longer offers it, as it sends no project folder) or `off`. A folder that is not under git cannot be
  copied: with `always` that is an error, with `auto` the conversation works in the folder itself, and two of those never work at once
  (`folder_busy`: the second message waits until the first conversation's turn is over).
- **Reviewing**: the Chat shows "Works in its own copy of the folder" and a button that lists the changed files (added, changed, deleted) with
  each file's diff (`GET /api/agent/sessions/<id>/copy` and `/copy/diff?path=`).
- **Applying**: the checked files, or all, go to the person's folder as **staged** changes, never a commit (`POST .../copy/apply`). They are
  checked first: a file the person changed in a way that does not fit stops the whole apply and is named, and nothing is half done. What was
  applied is no longer a change of the copy; the rest stays held. Only while the agent is not working.
- **Throwing away** (`POST .../copy/discard`) closes the conversation and removes the folder and the branch, with what was not applied.
- A branch of a conversation in a copy gets a copy of its own, made from the person's folder as it is.

This is also how an edit is held back until it is approved: the conversation's commands see its own files as they are, and the person's folder
changes only when they apply.

## Signing in to remote MCP servers

A remote MCP server (an address in a `.mcp.json`, not a command) is found like the others and approved by the person after they look at its address
(the query part of an address is dropped: a key does not belong there). One that wants a sign-in answers 401, and Settings, under Capabilities,
shows **Sign in** next to it (`harness/mcp_oauth.py`, `serve/mcp_auth_api.py`). The flow is the one the MCP authorization specification asks of
a client: the server's resource metadata (RFC 9728) names the authorization server, whose metadata (RFC 8414 or OpenID discovery) gives the
endpoints; a client id is registered by itself (RFC 7591) when the server allows it; the person approves in their browser (authorization code, PKCE
with S256, a `state` that must come back, the server's address as `resource`, RFC 8707); the code becomes tokens. The answer comes back to
`/api/mcp/auth/callback` on this server, only from this PC, and only for a sign-in this server started.

- Tokens are in the credential store (the Windows credential manager), in pieces when long, never in a file in clear. A file keeps only where the
  endpoints are and the client id. A refresh token that the server refuses means signing in again; the client id is kept.
- Every address the discovery documents name must be https (plain http only when the MCP server itself is on this PC), and for a server that is not
  on this PC it must not be on a private network; metadata for another resource or issuer, or without S256, is refused.
- The agent calls an approved remote server through `call_capability` like a local one; the token goes only to that server, in the `Authorization`
  header of every request, and a token that ran out is renewed once before the call is given up. A server that is not signed in says so in the
  tool's answer.

Not done: servers that need a client id registered by hand (it says so; there is no place to type one yet), a sign-in from a different PC than the one
the server runs on, and the other transports' own sign-ins.

## A council for hard questions

`ask_council(question, context)` asks three advisers and a chair (`harness/council.py`). The advisers are the functions *Council member 1, 2, 3* on the
Providers page, the chair is *Council chair*, so each can be a different model, local or in the cloud; they get different jobs (answer, find what is wrong
or risky, propose another way). They answer at the same time, a member that fails is reported and the others go on, and the chair, told that the answers are
untrusted text, writes where they agree, where they differ and what to do (at most 250 words). It costs several model calls and is meant for a decision that is
worth it.

The tool exists only when the person pointed at least one of those functions somewhere (a provider, or "Basalt (this server)" for the loaded model in
several roles); with nothing chosen it is not offered, and what leaves this PC follows the rules of any other function (personal data to a cloud provider only
where the person said yes for that function, daily caps, fallback only to a route that is as local or more).

Whether it helps has not been measured: `python -m harness.eval --set council` runs six decisions about code (five with a flaw a reviewer finds, one sound);
run it without `--council` and with it, after setting a council up, and compare (a difference under 3 points is noise until it repeats).

## Language servers

When a language server is installed (pyright or pylsp for Python, typescript-language-server, clangd, rust-analyzer, gopls), a conversation
with tools gets three read-only tools: `diagnostics(path)` (the errors and warnings of a file, with line and column), `go_to_definition` and
`find_references` (a line and column counted from 1). The server is started on first use for the language of the file and ended with the
conversation (`harness/lsp.py`). Which program runs comes from `lsp.json` in the user data folder (`{"servers": {"python": ["pyright-langserver",
"--stdio"]}}`) or, without it, the first known server found on the PATH. A folder under work never names a command to run. With no server found
the tools are not offered. What the tools return is marked as untrusted text. Not done: diagnostics added to the result of an edit by themselves,
rename and code actions, and servers that need settings of their own.

## The web pages

The pages are what `BasaltWinGUI.exe` shows in its window. They are built for windows from 1024 pixels up to about 3600
wide, not for phones.

- **Interface size**: automatic on very wide windows (zoom 1.25 from 2400 pixels up to 2 from 5000), or a fixed
  percentage in Settings.
- **Buttons** are 30 px tall with 13 px type (`--btn-h` and `--btn-fs` in `app.css`), so one setting changes them all; the pages are for
  wide windows, so they are not sized for touch.
- **Sources**: after a link or an address in an answer, and for each site a `search_web` or `browse_url` step used, a small button opens the
  page in the system's browser (`target="_blank"`; the app hands a new window to the default browser). Only `http` and `https` addresses
  without a user name get one.
- **Tabs**: Chat, Agent, Plans, Monitor, Models, Hardware, LOGS, Wallet, Settings; **Alt+1…9** open them. **Ctrl+K** opens a palette that
  reaches every view and the common actions and settings.
- A notice under the top bar says when no model is loaded and opens Models. Settings has an index of its sections and
  holds the cloud-filter consent. The Monitor shows the agent's tasks and edits: under the requests, or in a column of
  their own on windows from 1700 pixels.
- **Reviewing the agent's edits**: the agent applies an edit when it makes it. In the Monitor each edit lists its blocks
  with *Keep* and *Undo* (and *Keep all*, *Undo all*); undoing asks to be pressed twice. A block is undone only while the
  file on disk is exactly what the agent wrote, so nothing you did since is overwritten; if you changed the file, or
  a later edit touched it, the panel says so and does nothing. Only the latest 30 edits can be undone, only for text
  files up to 200 000 characters, and only from the PC the server runs on.
- **Agent**: the way to run the agent from the app. Choose the folder and write the task; the mode sets how much it may do
  on its own: *Plan only*, *Read only*, *Ask first* (it edits files in the folder and asks before it runs a command) or
  *Automatic* (it runs commands without asking; needs a tick that says so, and belongs in a folder you can lose). A question
  from the agent (a command to run, an MCP tool) appears at the top of the right column and on the tab as a count, with
  *Approve* and *Deny*; one nobody answers within 15 minutes, or whose run ended, counts as a refusal. Under *More options*:
  the most steps, and rules for commands it may run without asking. The page shows the run's status and a readable log (one
  line for each tool, then its answer) and a button to the Monitor, where its edits can be reviewed; *Stop* ends the
  agent and everything it started. *Go back* lists the checkpoints of the folder, with what the agent did after each, and puts the
  files back to one of them (press twice); actions that stay done, such as a command it ran, are listed and the button warns. The
  state before the rewind is itself a checkpoint, so going back can be undone. One run at a time. It works only from the PC the server runs on, with the API key if the
  server has one. The agent is the same `python -m harness` as on the command line, started by the server with
  `--progress --result-file --ask-server`; the log and the result file are in the data folder (`journal/runs`).
- **Plans**: the graph (drawn with status as icon and word as well as colour), an ordered list that works by keyboard,
  proposed changes shown on the graph before they are applied, approve and reject, tasks handed to you, a box to judge events.

`node tools/web_audit.mjs --url http://127.0.0.1:7232 --interact --strict` drives Edge (the engine of WebView2) through every
view at 1024, 1920 and 3600 pixels and fails on page overflow, console errors, a body the zoom does not fit, controls
under 24 pixels, or a broken shortcut or palette; it saves screenshots. Static checks of translations, contrast and markup
run with `python -m pytest serve/tests/test_web_static.py`.

## Evaluation

```powershell
python -m harness.eval --set core --k 5 --endpoint http://127.0.0.1:7232 --gpu-power-limit-w 450 --out report.md
python -m harness.eval --set toolcalls --k 5
python -m harness.eval --set knowledge --k 3 --strategies none,rag,cag
```

Each trial is a fresh agent run on a fresh workspace with its own seed, checked on what happened (files, commands, the
answer, the tools called). A crash, a timeout or a server error is a failed trial with its reason, and a task that cannot even start is a
failed task; neither ends the run. `tool_called` and `tool_args` count only calls that ran, not calls the permissions refused. An answer
check compares normalized text (case, Unicode form, curly quotes and dashes, Markdown emphasis and code marks, runs of spaces), takes
`any_of` for alternative wordings, and with `"word": true` matches a number or word only as a whole (`24` is not in `124`); a check with
a missing part is refused when the set loads. The report gives the harness release and
commit, the model, the hardware, the GPU power limit (or says it was not recorded), pass@k and pass^k, a 95% interval, tool-call
validity, cost per trial and the prefix cache hit rate. These are development A/Bs, not headline numbers; a difference under about 3
points is noise until repeated. The task sets are small starting points; add private tasks of your own. Running other
harnesses against the same endpoint is not automated.

`python -m harness.eval.alternate` measures what it costs when two conversations take turns on one model (the time to the first token and the share of the
prompt read from the cache, taking turns against one after the other). It decides whether a queue that groups requests by conversation is worth building.

## Replay of a recorded session

`harness/replay.py` runs a recorded conversation again with a model that answers with the recorded answers, one after the other,
and reports the first place where the tool calls differ: another tool, other arguments, another decision (executed, denied,
invalid, blocked, loop), a call missing or added, and with `compare_results` a call that failed or succeeded where the recording
did the opposite. It tests the harness (loop, permission rules, tools), not the model. `export_trace(messages, turns)` writes the
trace as JSONL (a header line, then one line per message; each tool result carries the decision), with credential-looking values
and any extra values you pass masked (`mask_text`; a trace written for sharing is masked by default). `load_trace` also reads a
conversation's `history.json` and a plain file of messages, where the decision is read from the result's text. `replay(trace)` uses
stand-ins that return the recorded results (so nothing is run); pass your own registry to run the real tools, preferably in a copy of
the folder, and `confirm_from_trace(trace)` to answer questions as the person did. `python -m harness.replay trace.jsonl --mode auto`
does the first and prints the divergence. Permissions are decided by the rules you replay under, so a replay under another level is
the way to see what a change in the rules would have done to a past session.

## Running code

`run_code` (`harness/tools/execute.py`) runs a snippet in a separate process: Python with the interpreter of Basalt's own
environment in isolated mode (`-I`), and JavaScript or TypeScript when `deno` is on the PATH (all three languages) or
JavaScript when only `node` is. Each run gets a new working folder under `.basalt/run/` in the workspace, the workspace's path
in `BASALT_WORKSPACE`, and the same scrubbed environment as the shell. On Windows it runs in a job object (2 GiB of memory, 64
processes); a timeout (30 s by default, 600 s at most) or a cancel of the run kills the whole tree. Output is cut at 50,000
bytes per stream. Files the code leaves in its working folder are listed in the result and kept for a week; a run that makes
none leaves nothing behind. Edits the code makes to workspace text files are recorded for review like a shell command's.

It is an irreversible action for the permission layer (it can write anywhere the user can), so it asks at the default level,
is refused in plan mode, and can be allowed or denied with a rule named `run_code`. **There is no network or file isolation
beyond the working folder** (open decision D5): Python and node run with the user's rights. Only deno narrows things (read
access to the working folder and the workspace, write access to the working folder, no network); that path was built from
Deno's documented flags and not run here, because deno is not installed on the development PC. The memory and process caps
are Windows-only. There is no retry loop of its own: a program that fails is reported with its traceback, and the loop
detector stops identical repeats.

## Subagents

`spawn_subagent` (`harness/swarm/subagent.py`) hands a task to a second conversation and returns its last message as a
summary. The subagent has its own messages and only the tools named in `tools` (by default the read-only ones; never
`spawn_subagent` itself), up to `max_turns` turns (8 by default, 20 at most). It shares the parent's model client, permission
manager, hooks, checkpoints, secrets and cancel flag, so its calls are decided by the same rules and questions as the parent's
(plan mode and deny rules included, and reading outside content taints the parent too), and its calls land in the same audit
ledger between `subagent_start` and `subagent_end` records. A summary that follows outside content is fenced as untrusted
data. Subagents run one after another, never at once. The older `SwarmOrchestrator` and its `swarm.json` only divide a token
budget among roles and run a mock; nothing uses it and `spawn_subagent` does not.

## Routines

`harness/scheduler.py` keeps recurring tasks for the agent in the user data folder (`routines.json`, results in
`routine_runs.json`, the last 200). A routine is a prompt, a folder, a schedule, a mode and allow rules. Schedules are
`every 30m`, `every 2h`, `every 1d` (at least a minute) or fixed local times: `daily 08:30`, `weekdays 08:30`, `weekends 10:00`,
`mon,wed,fri 18:15`; there is no cron syntax. A routine is created **disabled** and its mode is read-only unless the person
chooses otherwise (`auto` needs the same confirmation as a conversation). Only one routine runs at a time and only while no
other conversation is busy or asking; a due routine waits (an hour at most, then the result is `skipped`). A routine that is
running is not interrupted when the person starts talking. A routine runs as a conversation through the server's
`SessionManager`, which is closed when it ends; if the agent needs an answer nobody can give, the result is `asked` with the
question, and nothing is decided for the person. Overdue interval routines run once when the program starts; a fixed time that
passed while it was off is skipped.

The API is `GET /api/routines` (routines, what runs, the latest results) and `GET /api/routines/runs?id=&limit=`, and the writes
`POST /api/routines` (create), `POST /api/routines/<id>/update|enable|disable|run|remove` and `POST /api/routines/cancel`, behind
the same control-secret gate, loopback check and own-page check as the other writes. The scheduler is made when the
conversation manager is first made (when the Chat first asks for its conversations) or when the routines API is first used, not
when the server starts, so routines do not run in a server that nobody has opened a page on. There is no page for routines yet.

## Other agents: ACP client and A2A

`delegate_to_acp_agent` (`harness/acp_client.py`) lets Basalt drive another Agent Client Protocol agent (version 1, stdio):
`initialize`, `session/new`, `session/prompt`, the agent's `session/update` notifications (message text and a list of its tool
calls come back), its `session/request_permission` and `session/cancel` when the run is cancelled. The permission requests
are put to the person through the same permission manager as Basalt's own tools, as `acp_permission:<agent>:<title>`: the
person's answer becomes `allow_once` or `reject_once`, and with nobody to ask the request is rejected. The client declares no
`fs` or `terminal` capability, so the other agent works on the folder itself (its edits are recorded like a shell command's),
and it does no authentication. The tool exists only for agents written in `acp_agents.json` in the user data folder
(`{"agents": {"name": {"command": ["program", "arg"], "env": ["VARIABLE_TO_PASS"], "description": "..."}}}`); the model can name
an agent but never a command. The agent starts with the scrubbed environment plus the variables listed in `env`. Its answer is
untrusted text. It is tested against a fake agent and against Basalt's own ACP agent over real pipes, not against a third-party
agent.

`ask_a2a_agent` (`harness/a2a.py`) is a client for Agent2Agent 1.x over the HTTP+JSON binding: it reads the Agent Card from
`/.well-known/agent-card.json`, takes the first `HTTP+JSON` interface of version 1, sends one text message with `POST
/message:send` and, if the task is not finished, polls `GET /tasks/{id}` (and cancels it with `POST /tasks/{id}:cancel` on a
cancel or after the wait). The agents are listed in `a2a_agents.json` (`{"agents": {"name": {"url": "...", "token_env":
"VARIABLE_WITH_A_BEARER_TOKEN"}}}`). A task that asks for input or authentication is reported as it stands; streaming, push
notifications, files and the extended card are not done. It was written from the specification at a2a-protocol.org (version
1.0.0) and tested against a local fake, never against a real agent; the exact error bodies are read leniently. **Basalt does not
serve an Agent Card or receive A2A tasks**: a card must list an endpoint that accepts tasks, and receiving a task means running
the agent for a remote caller, which needs a decision about who may call it. Both tools are irreversible and untrusted.

## Energy of a task

`harness/energy.py` samples the GPUs' power once a second (from the server's telemetry) while a task runs and reports the power
limit, the average and peak power and the energy in watt-hours (the samples added up over time). A routine's result carries it
as `energy`. It is the energy of the whole board(s) while the task ran, including anything else using the GPU, and the figure
is an estimate; without readable power it is left out. Runs started from the Agent page and the evaluation reports do not
carry it yet.

## Tests and what is not done

`python -m pytest tests` (harness) and `python -m pytest serve/tests` (server and pages). Tests that would place a real call
are skipped. Seven server tests (`test_config`, `test_lang_and_running`, `test_metrics`) fail on Windows with or without
these changes.

An approval from the page asks the server for an answer, and any process on this PC that can reach the server can post
one, so it matters that an agent which can run arbitrary code (an allow rule such as `python *`, or Automatic mode) can approve
its own later requests: such a rule already grants that power, and only the isolation below would take it away.

Not done: file and network isolation for commands (the job object limits resources only); `fs/*` and `session/load` in ACP;
reading bank accounts through an authorized aggregator, and a test of Google Pay or an embedded Stripe form on a real site; the compaction of an overlong chat still inserts its summary mid-context, which re-reads what follows.
