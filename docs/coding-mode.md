# Coding mode

What the Agent tab adds for working on code in a folder: a **role** for each conversation, the **project checks** a folder seems to have,
and a **safe verification** class of permissions. Code: `tadamoo/roles.py`, `tadamoo/project_profile.py`, `tadamoo/permissions.py`
(`safe_verification`), `serve/coding_prefs.py`, the wiring in `tadamoo/stack.py`, `tadamoo/agent_loop.py`, `tadamoo/session.py` and
`serve/agent_sessions.py`, and the page in `serve/web/coding.js` and `coding.css`. The design comes from
[harness-study/vibe-coding.md](harness-study/vibe-coding.md). The file tools (`edit_file`, `search_code`, `tree`) and the verify gate are
described in [harness-file-tools.md](harness-file-tools.md) and [harness-gates.md](harness-gates.md).

## Roles

A role says what the agent is for. It is a short block of instructions, a set of tools it may use, and what it may write. It is chosen for a
conversation (the selector in the Agent tab, `--role`, the `role` field when a conversation is created) and can change at any time.

| role | tools | writes | instructions in short |
|---|---|---|---|
| `build` (the coding role, shown as Developer) | read, search, `edit_file`, `write_file`, `shell_command`, memory, skills, LSP | anywhere in the folder | look first, change in small steps, run the project's checks, report what they showed |
| `plan` | read tools, `edit_file`, `write_file` | only `.tadamoo/plans/*.md` | no changes to the project; at most three questions; a plan file, a checklist, a summary |
| `ask` | read tools | nothing | answer about the project with file and line for each claim |
| `debug` | the same as `build` | anywhere in the folder | run the failure, list at most four causes, narrow to one or two, confirm, fix, run again |
| `orchestrate` | read tools, `spawn_subagent` | nothing | hand each part to a subagent with the file and the check, then verify what comes back |
| `general` | every tool | anywhere in the folder | none: a conversation as it was before roles |

The read tools are `read_file`, `list_directory`, `tree`, `search_code`, `repo_map`, `repo_diagram`, `diagnostics`, `go_to_definition`,
`find_references`, `recall` and `load_skill`. `ask_user`, `todo_write` and `present_plan` are allowed in every role, so the checklist and
the way out of the Plan only permission mode always work. The browser, the web tools, the planner, the phone, the vault and MCP tools are
not in a coding role; `general` has them. The coding role has one name, the constant `CODING_ROLE` in `tadamoo/roles.py` (its id is `build`; the page shows it as
"Developer" in both languages, the string `cd.role.build`, and it is the Agent tab's default role).

### How a role is applied, and the cache

The tool list sent to the model is the same in every role, in the same order, and the system prompt does not change: a switch never
rewrites the cached start of the prompt (`tests/test_coding_mode.py` checks it with the order of the tool list and the first messages).
A role works in two ways that are not part of that start:

- **A call outside the role is refused by name** (`AgentLoop._guarded`, before the permission layer), with the tools the role has: "The tool
  'edit_file' is not available in the ask role. Tools you can use: ...". For `plan`, a file tool is also checked at the call against the
  allowed globs, and the refusal names them. The audit records `role_denied`. Nothing is counted as a permission refusal.
- **A note ends each request and is not kept.** It starts with `[harness] Role: ...`, then the tools of the role, what it may write, and the
  commands the person approved (up to six). It is the same text for as long as the role, the tool list and the approved commands are, and it
  is at most 600 tokens. It is added to the request in `AgentLoop._ask` and is not in `agent.messages`, the history or the saved
  conversation. `general` adds nothing.

The tool list therefore still holds every tool, so the model may try one that the role refuses; the note and the refusal say what to use.
`/tools` marks the tools outside the role and `/status` shows the role.

## The project checks

`tadamoo/project_profile.py` reads a folder's files (it runs nothing) and suggests commands by kind:

| files | suggested |
|---|---|
| `pytest.ini`, `[tool.pytest]`, `setup.cfg`, `tox.ini`, or `pyproject.toml`/`setup.cfg` with a `tests` folder | `python -m pytest -x -q` |
| `[tool.ruff]`, `ruff.toml`; or flake8 configuration; `[tool.mypy]`, `mypy.ini` | `python -m ruff check .`; `python -m flake8`; `python -m mypy .` |
| `package.json` scripts `test`, `lint`, `typecheck` (or `type-check`, `tsc`), `build` | `npm test`, `npm run lint`, ... (`pnpm` or `yarn` when their lock file is there); the placeholder `no test specified` is ignored |
| `Cargo.toml` | `cargo test`, `cargo clippy`, `cargo build` |
| `go.mod` | `go test ./...`, `go vet ./...`, `go build ./...` |
| `Makefile` targets `test` or `check`, `lint`, `build` | `make test`, ... |
| `CMakeLists.txt` | no command: the person writes their own |

For a command that only starts a script of the project (an npm script, a Makefile target) the suggestion carries the script's text, and the
card shows it. Each suggestion has a digest of the command and that text.

### Accepting

The card "Project checks" in the Agent tab lists them and the person accepts them one by one, with two choices for each: **run without
asking** and **run before an answer ends** (the verify gate). Nothing is accepted by default and nothing from a project file approves
anything: the acceptance is saved in `coding.json` in the user data folder, keyed by the folder, with the command and its digest
(`serve/coding_prefs.py`); a file `.tadamoo/project.json` or anything else in the project is never read for it. Only a command the folder
suggests now can be accepted. An accepted command counts only while the folder still suggests the same command with the same digest: if the
project changes the script behind `npm test`, the card shows "Changed since you accepted it" and the command asks again, and a command the
folder no longer has is dropped.

The person can also type their own checks, one command per line (up to 24, 500 characters each). Those always count.

## Safe verification

`PermissionManager.safe_verification` (`tadamoo/permissions.py`) lets a `shell_command` run without a question when:

- the whole command equals one the person listed or accepted (spaces outside quotes do not matter), or
- every statement of it does (statements are split on `;`, `&`, `|`, `&&`, `||` and line ends outside quotes), or
- it is one of the read-only commands `git status`, `git status --short`, `git diff`, `git diff --stat`, `git log`, `git log --oneline`,
  `ls`, `dir`, and the role is one that runs commands (`build`, `debug`; not `general`, `plan`, `ask`, `orchestrate`).

It is never a prefix match: `python -m pytest -x -q tests/a.py` is not covered by `python -m pytest -x -q`, `pytest > out.txt` is not
covered by `pytest`, `ruff check . && rm x` is not covered, and a command built at run time (`$(...)`, a backtick, `${...}`, `%VAR%`) is
covered only if it is, whole, a listed command. The order in `_decide` is: plan mode (refuses what is not read-only), deny rules, the Auto
level, allow rules, **safe verification**, tools that always ask, the Rule of Two, the level. So a deny rule and plan mode win, and it
works at the read-only level too, as an allow rule does. The permission reason in the audit is "Permitted as safe verification". When
nobody can answer, the refusal lists the exact commands that can run.

### With the verify gate

The gate (Settings > General > Checks before an answer ends, off by default) runs the commands of the Settings list and the accepted
checks marked "run before an answer ends" for the folder. They reach a running conversation at once (`BuiltAgent.set_checks`). With
the gate off the ticked checks only wait; the card says so.

## The page

In the Agent tab (not in the Chat) the head of the page has a **Role** selector, and under it the **Project checks** card.

- The selector lists the roles from the server (`GET /api/coding`), shows each role's description as its tooltip, and for a new conversation
  remembers the last choice in this browser (the first time: the coding role). Changing it in an open conversation calls
  `POST /api/agent/sessions/<id>/role`; the conversation shows a note "Role is now ...".
- The card shows a summary line ("1 of 3 accepted") and a **Review** button. Opened, it lists each suggestion with its kind, command, file,
  script and state in words (not accepted, accepted, changed since accepted), the two checkboxes, a box for the person's own checks, the
  read-only commands, the state of the verify gate and **Save**. A folder with nothing in it offers the Plan role with a button; it never
  switches by itself.
- After a plan in the Plan role a button **Continue in Developer** switches the role and sends "Go ahead and carry out the plan." A message
  that looks like a fault report, sent in the coding role, offers **Switch to Debug**. Both are buttons: the role never changes without one.
- Buttons are 30 px high (the app's `--btn-h`), long commands wrap, the list scrolls inside the card, and nothing needs sideways scrolling
  from 1024 px up. All texts exist in English and Italian (`cd.*` in `serve/web/i18n.js`) and are put in the page as text.

## Interfaces

| | |
|---|---|
| options | `--role ROLE`, `--safe-command CMD` (repeatable), `--project-check CMD` (repeatable); the server starts a conversation's process with them |
| process commands | `{"cmd": "role", "role"}` and `{"cmd": "checks", "safe": [...], "checks": [...]}`; events `role` and `checks` |
| server | `GET /api/coding` (roles), `GET /api/coding?workspace=` (the card), `POST /api/coding` (`workspace`, `accepted: [{command, gate}]`, `own`; tells the running conversations of the folder), `POST /api/agent/sessions/<id>/role`; the `role` field of a new conversation. The two `/api/coding` routes answer only to the PC the server runs on |
| record | the conversation's `role` is in its record and survives a restart of its process; a record without one is `general` |

## Tests

`tests/test_coding_mode.py` (roles, notes, refusals, cache, safe verification, detection, acceptance, server and process wiring, the route),
`tests/test_coding_page.py` (the script run in node against a stand-in page, markup, strings, styles), and the cases added to
`serve/tests/test_agent_sessions.py` and `serve/tests/test_web_static.py`. They use the scripted mock model. What needs the real model is
in `tests/QUEUE_daytime.md` (rows 54 to 56).

## Not built

- A roles file in the workspace that narrows the tools of a role; `spawn_subagent` with a `role` argument.
- `run_tests` as a tool of its own (a listed command through `shell_command` does the same), `run` commands in the project checks, and
  the detection of a language server's diagnostics as a check.
- The status chip per turn ("Edited 3 files, tests passed"), "Back to last passing state", the whole-folder snapshot and "Undo this request".
- The SEARCH/REPLACE fallback, subfolder `AGENTS.md`, skill arguments, named subagents (phase 2 of the study).
- An evaluation of the roles with `tadamoo.eval --set vibe`; the numbers wait for the real model (queue row 54).
