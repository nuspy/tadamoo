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
```

| option | meaning |
|---|---|
| `--permission-mode plan\|read_only\|default\|auto` | `plan` changes and runs nothing; `default` allows edits in the workspace and asks for the rest |
| `--allow PATTERN`, `--deny PATTERN` | rules such as `shell_command:pytest*`; a deny always wins |
| `--secret-env NAME[=host,host]` | lets the agent write `{{secret:NAME}}`; with hosts, the value can only be sent to them |
| `--reference DIR`, `--reference-strategy auto\|cag\|rag\|none` | reference documents: fixed block, searched passages, or by size |
| `--wiki`, `--wiki-project` | wikibridge tools (`BASALT_WIKIBRIDGE_URL`, `BASALT_WIKIBRIDGE_KEY`) |
| `--skills DIR` | folders with `SKILL.md` skills |
| `--phone`, `--on-behalf-of NAME` | phone tools (`CALLAPICALL_URL`, `CALLAPICALL_TOKEN`); every call asks you |
| `--interactive` | ask on the terminal before risky actions |
| `--max-tokens-budget`, `--timeout-seconds`, `--max-turns` | stop after this much |

The run and its file edits appear in the Monitor (tasks and changes) while it works, unless `--no-report`; the reporting
never changes the run, and stops after two failed attempts. An edit made by a shell command (`sed -i`, a script that rewrites
a file) is recorded too: the workspace's text files are compared before and after each command, so it can be reviewed block
by block like the file tools' edits. Binary files, files over 200 000 characters, `.git`, `node_modules` and similar folders
are left out, as are the changes of a command that writes more than 50 files; a workspace of more than 5 000 files or
30 MB of text is not watched.

Exit codes: 0 finished, 1 error, 2 bad usage, 3 turn limit, 4 budget, 5 stuck in a loop, 130 cancelled. With `--json` the
result has the stop reason, the changes, token and cache counts and the path of the audit log.

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

### The Chat and the planner

A conversation has the planner's tools (`harness/planner_tools.py`), unless it is started `ask_only`: `plan_list` and
`plan_get` read; `plan_create`, `plan_propose_change`, `plan_mark` and `plan_archive` change; `ask_user` asks a question with
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
sensitive data (secrets), irreversible actions ask again.

What this is **not**: an OS sandbox. A command runs with your files and network rights, with a scrubbed environment, no
stdin, a time limit and capped output. On Windows each command also runs in its own job object, created before the command
executes anything: 4 GiB of memory and 64 processes for all of it together, and everything it started is killed when it ends
or times out (a server a command leaves running does not survive it). The deny list stops the obvious system-changing tools
wherever they appear as a command word. File and network isolation (a dedicated Windows user, firewall rules, a VM for
untrusted code) is not provided; until it is, `auto` should only be used in a folder you can lose. Hooks, rules and
the secrets check read what a command *says*: a command that builds its address at run time needs a firewall.

Checkpoints are stored outside the workspace; a rewind takes a safety snapshot of the current state first. To go back:
`python -m harness.rewind --workspace D:\proj` lists the checkpoints with what the agent did after each, and `--to ID`
restores the files (`--keep-new` leaves files created since). Restoring files does not undo a command the agent ran, a call
it placed or an MCP tool, so a rewind over any of those lists them and needs `--yes`; the rewind is written in the audit log
and its safety snapshot can be rewound to as well. The audit log
(`audit.jsonl`) is tamper-evident (a hash chain, a head file, secrets masked), not tamper-proof.

## Secrets, payments and phone calls

- **Secrets**: the model writes `{{secret:NAME}}`; the shell tool fills it in at the last moment and masks the value in
  everything that comes back. A secret without hosts is for local commands only; one with hosts is refused if the command
  names any other host or a network tool without a URL.
- **Payments without confirmation (level 4)** are specified in the design document (§4.8.4). The broker (`payments_broker.py`)
  keeps a state file signed with the device key and a hash-chained ledger and re-reads both on every request, so a revocation
  made in another process applies at once and any file that fails verification stops all payments. Amounts are exact cents;
  there is a cap per payment and a cumulative cap; a waiver expires. Granting one needs the dictated declaration (checked in
  Italian and English against the chosen amount), the release and acceptance steps with the name typed again, a payment
  system limited to delegated credentials, and an AES-256-GCM evidence dossier (needs the `cryptography` package, see
  `harness/requirements.txt`; without it no waiver is given). The agent can only *ask* (`request_payment`); nothing it can
  call grants or widens a waiver. **No payment connector exists**: an authorization is a recorded decision, nothing is charged.
  Decisions still yours: whether the declaration's amount is the per-payment or the cumulative cap (it is the per-payment
  cap, confirmed), the default 30-day expiry, and a lawyer's review of the three dialogs.
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
  placeholder; long outputs keep their head, tail and **every error line**; JSON is trimmed and HTML reduced to text. The
  system block is byte-stable so the engine's prefix checkpoint is reused; retrieved memories go after the goal as untrusted data.
- `AGENTS.md` (else `CLAUDE.md`, else `GEMINI.md`), searched from the workspace up to the repository root, is part of the prompt.
- Memory is SQLite full-text search per workspace; identifiers such as `sm_120` are searchable.
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
  whatever they claim; `MCPHub` exposes two tools (search and call) so the prompt stays short, and can hold servers of both eras.
  **Not implemented**: OAuth, and `subscriptions/listen`. Two details were inferred, not read: that `requestState` sits in
  the retried request's `params`, and the shape of the `notifications/cancelled` sent on a stdio timeout.
- The ACP agent follows protocol version 1 over stdio: `initialize`, `session/new`, `session/prompt`, `session/cancel`,
  `session/set_mode`, `session/update` (message and thought text, tool calls, edits as `diff` content) and
  `session/request_permission` toward the editor. It edits files itself rather than through the editor's `fs/*`, offers no
  `session/load` and no authentication, and reads text only.

## The planner

`harness/planner/` holds the plan graph, the plan card, the event filter (System 1), re-planning by the local model (System 2)
and the bridge the page talks to.

- A plan is a graph of action, sensor, decision and merge nodes with shared values. Decisions route by simple conditions
  (`len(guests) >= 2`; never code); merges wait for their inputs; a task the software cannot do is offered to you with an
  alternative and, if you decline, becomes yours and blocks what depends on it. You can rewind a step.
- Incoming events wait in a buffer (10 events or 5 minutes after the first) and are judged by the filter. **The cloud filter
  (TypeSafe Jev) is off.** It needs your consent in Settings *and* a key in the server's `TYPESAFE_API_KEY`; only the plan
  fields you tick are sent, with people's names replaced by labels; the answer says whether it came from the cloud, from
  the local keyword filter or from a failure. When the filter says wake, the local model proposes two or three alternatives
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
- API: `GET/POST /api/plans` (`GET /api/plans?archived=1` lists the archived ones), `GET /api/plans/<id>`,
  `POST /api/plans/<id>/events`, `POST /api/plans/<id>/triage`, `POST /api/plans/<id>/archive` and `/restore`,
  `GET /api/plans/<id>/events?after=N` and `/stream` (server-sent events), `GET/POST /api/planner/settings`.

## The web pages

The pages are what `BasaltWinGUI.exe` shows in its window. They are built for windows from 1024 pixels up to about 3600
wide, not for phones.

- **Interface size**: automatic on very wide windows (zoom 1.25 from 2400 pixels up to 2 from 5000), or a fixed
  percentage in Settings.
- **Tabs**: Chat, Agent, Plans, Monitor, Models, Hardware, LOGS, Settings; **Alt+1…8** open them. **Ctrl+K** opens a palette that
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
answer, the tools called). A crash or a server error is a failed trial with its reason. The report gives the harness
commit, the model, the hardware, the GPU power limit (or says it was not recorded), pass@k and pass^k, a 95% interval, tool-call
validity, cost per trial and the prefix cache hit rate. These are development A/Bs, not headline numbers; a difference under about 3
points is noise until repeated. The task sets are small starting points; add private tasks of your own. Running other
harnesses against the same endpoint is not automated.

## Tests and what is not done

`python -m pytest tests` (harness) and `python -m pytest serve/tests` (server and pages). Tests that would place a real call
are skipped. Seven server tests (`test_config`, `test_lang_and_running`, `test_metrics`) fail on Windows with or without
these changes.

An approval from the page asks the server for an answer, and any process on this PC that can reach the server can post
one, so it matters that an agent which can run arbitrary code (an allow rule such as `python *`, or Automatic mode) can approve
its own later requests: such a rule already grants that power, and only the isolation below would take it away.

Not done: file and network isolation for commands (the job object limits resources only); OAuth for MCP; `fs/*` and `session/load` in ACP;
holding an edit back until it is approved (it is applied first and can then be undone block by block); a payment
connector; the compaction of an overlong chat still inserts its summary mid-context, which re-reads what follows.
