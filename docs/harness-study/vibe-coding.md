# A vibe-coding mode for the Agent tab

Proposal from the study of Goose and Roo Code. The roles, the project profile, the safe-verification class and the Agent-tab selector are built (see [../coding-mode.md](../coding-mode.md)); the rest of this page is still the study. The user describes what they want in a folder; the agent plans, edits
files, runs the project's checks and iterates. Our models are local open-weight models, so tool-call reliability and prompt
length decide; Goose's own benchmark shows a 30B-class model at 1.1% on Terminal-Bench 2 against 50-57% for a frontier model, so
every idea is measured with `tadamoo.eval` before it ships.

## Sources

- **Goose** (https://github.com/aaif-goose/goose, formerly block/goose): Apache 2.0, "Copyright 2024 Block, Inc.", no NOTICE,
  commit 775b3958c394ae2747cfb01791d3a45ac579139a. Rust workspace; the loop is a state machine of ordered operations over the
  persisted conversation; tools are MCP plus in-process "platform" tools; recipes are parameterised task templates.
- **Roo Code** (https://github.com/RooCodeInc/Roo-Code): Apache 2.0, "Copyright 2025 Roo Code, Inc.", no NOTICE, commit
  b867ec9145750d0ae1ff7f02d35406e9bf2a0b16 (v3.53.0; the extension was shut down on 15 May, so this is a frozen reference).
  VS Code extension; native tool calling only; modes are role text plus tool groups plus an edit-file regex.

Nothing is copied: ideas are re-implemented in Python. If text or code is copied later, add an entry with the commit hash to
`THIRD_PARTY_LICENSES` (the Apache text is already there under the Infernix entry), mark the file as modified, and never use
the Goose or Roo names or logos.

## What we already have and they do not

Rule-based permissions with the Rule of Two, per-file checkpoints with rewind, hash-chained audit, branches, a git worktree per
conversation, the Changes panel with Keep/Undo per block, plan mode, `repo_map`, memory, skills, MCP, ACP, steering, an eval
harness with SWE-bench and Terminal-Bench loaders, engine-side grammar for tool calls.

## Verified gaps in our code

`edit_file` fails with only "Target string not found" and takes one edit per call (`tadamoo/tools/filesystem.py`); no text-search
tool; at `default` level every `pytest` asks because `shell_command` is IRREVERSIBLE (`tadamoo/permissions.py`); post-tool and
turn-end hooks cannot change the outcome; nothing can stop the agent from finishing before the tests pass (the only end check
is the constraint card in `AgentLoop._check_answer`); no todo tool; a coding conversation gets the whole registry (planner,
browser, vault, phone); no whole-tree snapshot (a script that rewrites a tree is not covered).

## The mode

Two axes: the permission mode that exists (Plan only, Read only, Ask first, Automatic) and a new **role** that decides what the
agent is for and which tools it sees (name is the owner's decision).

- **Roles** (`harness/modes.py`, `--role`): Ask (read tools only), Plan (read tools, `todo_write`, `ask_user`, `present_plan`, and
  writes only under `.tadamoo/plans/*.md`), Build (read, edit, shell, todo), Debug (Build plus a hypothesis protocol: list causes,
  narrow to 1-2, add logging, confirm, then fix; the gate re-runs the failing command), Orchestrate (todo, ask, `spawn_subagent(role,
  task)`, read tools; no edit or shell). Tools outside the role are neither advertised nor callable by name; the edit-glob limit
  is checked at call time and names the allowed globs. A workspace roles file may only narrow tools, never add shell or hooks.
  Role text under about 600 tokens, byte-stable within a role for the prefix cache.
- **Tool profile for local models** (about nine tools): `tree` (with line counts, gitignore-aware), `repo_map`, `search_code`
  (pure Python regex over text files, gitignore-aware, `path:line: text` with context, capped, works on Windows without `rg`),
  `read_file` (line numbers on; an indentation mode that returns the block around `anchor_line`), `edit_file`, `write_file`,
  `run_tests`, `shell_command`, `todo_write`, `diagnostics` when a language server exists. Measure the token cost of the full
  registry against this profile with `/context`.
- **Edit format.** Keep plain `target`/`replacement` fields (our grammar enforces them). Add `edits: [...]` applied atomically,
  `replace_all` or an expected count, LF-normalised matching with a cascade (exact, per-line rstrip, per-line strip with
  re-indent, unicode-normalised) applied only when exactly one candidate matches and the result names the matched lines; on no
  match return the closest window with line numbers and the real lines ("did you mean"); on several, list their line numbers.
  Roo's fuzzy threshold is 1.0, so it never applies a wrong guess: do the same. Reject a whole-file write over an existing
  file that contains "... rest unchanged ..." markers or is under half the old length unless `overwrite`. Not primary: unified
  diff (line numbers and hunk counts are a common failure for weaker models; inference) and Roo's SEARCH/REPLACE marker format
  (needs exact marker lines; Roo spends about 140 lines of error text on malformed markers). A SEARCH/REPLACE text fallback in
  `parse_calls` is phase 2, behind an eval.
- **After each edit:** syntax check in process (`ast.parse`, JSON, TOML, `node --check` when found), then new LSP diagnostics
  appended to the result; a per-file failure counter (third failure: re-read the file, prefer `write_file`).
- **Project profile** in `<folder>/.tadamoo/project.json`: detected test, lint and run commands (`pyproject.toml` or `pytest.ini`
  gives `python -m pytest -x -q`; `package.json` scripts; `Cargo.toml`; `CMakeLists.txt` asks). The person approves the exact
  strings once; they become a "safe verification" permission class together with a short read-only list (`git status`, `git
  diff`, `git log`, `ls`, `dir`), matched on the whole command after the segment splitter, never as a prefix. A profile
  changed by a cloned repo asks again.
- **Test-and-fix gate** (`harness/verify.py`, called where `AgentLoop` accepts a final answer): nothing edited since the last pass
  means finish; otherwise run each verification command as an ordinary tool call (permissions, audit, checkpoint apply). Pass:
  record the checkpoint as "last green". Fail: a tail user message with the command, exit code and the trimmed output (last 60
  lines plus every error line), up to 3 rounds, stop earlier if the same test ids fail twice, a wall-time cap, stop reason
  `verify_failed`. "No tests collected", a missing module or command not found go to the person, not back as code bugs.
  Goose's recipe `retry` resets the conversation and loses the failure output: do not copy that part; its Stop hook, a deny that
  becomes a hidden user message with a block cap, is the pattern.
- **todo_write:** the model rewrites the whole checklist; shown in a pane and repeated in the tail note each turn; survives
  `/compact`; a turn-budget note after half the turns.
- **Rollback:** per-block Keep/Undo and per-turn checkpoints exist; add "Back to last passing state" and a whole-tree shadow-git
  snapshot per request (separate git dir, `core.worktree` set to the workspace, exclude list, a size guard, never `clean -x`)
  for "Undo this request"; the own-copy worktree becomes the default for Automatic in a clean git folder.
- **Project map:** one compact `repo_map` (about 600-1000 tokens, focused on the request) as a tail note at the first turn;
  subfolder AGENTS.md appended once when the agent first touches that folder (untrusted workspace text); `@import` expansion
  limited to depth 3, 64 operations, 1 MiB, never outside the repo or into git metadata. Later: an FTS5 `code_search` (our server
  has no text-embedding endpoint; Roo's index needs an embedder plus Qdrant).
- **Subagents:** a `role` argument, a `context` argument, `resume`, named agents from `.claude/agents/*.md`, no nesting by
  default; sequential on one GPU.
- **Skills with arguments** (`$ARGUMENTS`, `$1`, `$name`) and frontmatter `verify:`, `max_turns:`, `tools:` are our "recipes".

## User flow

Add a folder; the profile chips appear and the person confirms the commands. A role selector sits next to the permission label
(default Build; "Plan first" for an empty folder). Plan asks at most three questions as buttons and shows the todo list; a yes
continues in Build. During work: the step stream, the Changes panel, the checklist, a status chip per turn ("Edited 3 files,
tests passed (pytest, 41 passed)" or "tests failing after 3 attempts" with the last output). Finish: summary, Keep all, Undo all,
Undo this request. A suggestion button, never a silent switch, offers Debug after a bug report.

## Build order (about 1,400 lines of code and 800 of tests, 1-1.5 weeks)

1. `search_code` and `tree`, the coding tool profile, the `edit_file` upgrade and its error package.
2. Project profile detection, the safe-verification permission class, `run_tests`.
3. `VerifyGate` with events, stop reason and exit code.
4. `todo_write` and its pane; turn-budget note.
5. Roles, `--role`, the selector, status chip, "back to last passing state".
Phase 2: SEARCH/REPLACE fallback, subfolder AGENTS.md, skill arguments, named subagents, compaction fallbacks, shadow snapshots,
`code_search`.

Risks: a slow local model times out the gate or loops (round, wall-time and same-failure caps); the verification command is code
the project controls (the person approves the exact string; shown in the chip); tolerant matching on the wrong block (unique
match only, matched lines returned); Windows quoting (call the interpreter with an argument list); hiding tools the person
expects (list in `/tools`, switch to the full set); the role changes the cache prefix.

Tests (no GPU, scripted mock engine): edit cases (no match, several, delete, CRLF, atomic multi-edit, tolerant unique and
ambiguous), the gate (pass, fail then pass, three failures, same failure twice, no edit means no run, permission refused,
cancel), `search_code` bounds and gitignore, the permission class, profile detection, the static page checks. Evaluation: a
`tadamoo.eval --set vibe` of 8-10 small tasks in temp folders at pass^k with k=5, comparing edit formats and gate on/off, every
number labelled with the GPU power limit; real-model runs go to `tests/QUEUE_daytime.md`.
