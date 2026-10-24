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
never changes the run, and stops after two failed attempts.

Exit codes: 0 finished, 1 error, 2 bad usage, 3 turn limit, 4 budget, 5 stuck in a loop, 130 cancelled. With `--json` the
result has the stop reason, the changes, token and cache counts and the path of the audit log.

Data (audit log, checkpoints, memory, approvals) lives in the user data folder, one folder per workspace, never in the
repository; the only thing written in the workspace is `.basalt/artifacts` (full outputs that were shortened).

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

Checkpoints are stored outside the workspace; a rewind takes a safety snapshot of the current state first. The audit log
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
- API: `GET/POST /api/plans`, `GET /api/plans/<id>`, `POST /api/plans/<id>/events`, `POST /api/plans/<id>/triage`,
  `GET /api/plans/<id>/events?after=N` and `/stream` (server-sent events), `GET/POST /api/planner/settings`.

## The web pages

The pages are what `BasaltWinGUI.exe` shows in its window. They are built for windows from 1024 pixels up to about 3600
wide, not for phones.

- **Interface size**: automatic on very wide windows (zoom 1.25 from 2400 pixels up to 2 from 5000), or a fixed
  percentage in Settings.
- **Tabs**: Chat, Plans, Monitor, Models, Hardware, LOGS, Settings; **Alt+1…7** open them. **Ctrl+K** opens a palette that
  reaches every view and the common actions and settings.
- A notice under the top bar says when no model is loaded and opens Models. Settings has an index of its sections and
  holds the cloud-filter consent. The Monitor shows the agent's tasks and edits: under the requests, or in a column of
  their own on windows from 1700 pixels.
- **Reviewing the agent's edits**: the agent applies an edit when it makes it. In the Monitor each edit lists its blocks
  with *Keep* and *Undo* (and *Keep all*, *Undo all*); undoing asks to be pressed twice. A block is undone only while the
  file on disk is exactly what the agent wrote, so nothing you did since is overwritten; if you changed the file, or
  a later edit touched it, the panel says so and does nothing. Only the latest 30 edits can be undone, only for text
  files up to 200 000 characters, and only from the PC the server runs on.
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

Not done: file and network isolation for commands (the job object limits resources only); OAuth for MCP; `fs/*` and `session/load` in ACP;
holding an edit back until it is approved (it is applied first and can then be undone block by block); a payment
connector; the compaction of an overlong chat still inserts its summary mid-context, which re-reads what follows.
