# Hermes Agent: what our harness can learn

Source: https://github.com/NousResearch/hermes-agent, MIT, "Copyright (c) 2025 Nous Research", commit
ef75bc708867e3884cb40a91363be6dc6fddc034 (read from source, never run). A personal agent: one core behind a CLI, TUI,
desktop app and a messaging gateway; memory, agent-written skills, subagents, cron, terminal, browser. Our harness is already
ahead on Rule of Two, vault, hash-chained audit, per-file checkpoints, plan mode, hooks, MCP, ACP/A2A, planner; the gaps are
in the robustness of the model-facing loop for a small local model and in a few tools. Items are ours to rewrite, not copy.

## Instructions to and from the model

- **Truncated, empty and degenerate replies (biggest gap).** Our `agent_loop.py` never reads `finish_reason`. Hermes
  (`agent/turn_truncation.py`, `agent/repetition_guard.py`, `agent/turn_empty_response.py`): on `length` it refuses to continue
  when the visible text is empty after a think block or dominated by repetition (a 60-character window repeated 5+ times
  covering half of a 400+ character fragment), otherwise appends the fragment plus a nudge and continues at most 4 times,
  stops when the prompt filled the window (fewer than 512 free tokens), re-asks a tool call cut mid-argument with a doubled
  `max_tokens` (never runs incomplete arguments), and turns reasoning off for a thinking-only truncation. A ladder recovers
  "no visible text after think". Port: read `finish_reason` and `usage` in `AgentLoop.run`, new `tadamoo/output_guard.py`
  (repetition test, continue/retry/stop state machine), nudge as a `note: true` message. M.
- **Tool-call repair.** Three layers: JSON repair by passes (control characters, trailing commas, string-aware closing of
  brackets, unrecoverable text never guessed; `agent/message_sanitization.py`), schema-guided coercion ("42" to 42, "true" to
  True, JSON string to array/object, bare scalar to one-element list; keep the original when ambiguous;
  `tools/arg_coercion.py`), and tool-name repair (case, separators, CamelCase, `_tool` suffix, junk after a quote, then
  `difflib` at 0.7; `agent/agent_runtime_helpers.py`). A mixed batch runs the valid calls and errors only the invalid ones;
  strikes advance only when no call in the turn is valid. Port: `tadamoo/tools/repair.py`, called in `parse_calls` and
  `ToolRegistry.validate` before the permission layer; record `repaired: true` in the audit. S.
- **Instruction blocks by model family** (`agent/prompt_builder.py`): tool-use enforcement ("never end your turn with a promise
  of future action", applied to qwen), execution discipline (verify before done, read back after external writes, mandatory
  tools for arithmetic and file facts), task completion ("never substitute fabricated output; reporting a blocker honestly is
  better"), and a function that deletes any line naming a tool absent in the session. Their own A/B shows such blocks can move
  nothing for some models, so gate by evidence. Port: `tadamoo/prompt_blocks.py` in our words, selected by `valid_tool_names`;
  decide with `python -m tadamoo.eval --set core --set toolcalls --k 5`. S.
- **Prompt assembly.** Stable tier, then context (project files, then anything worktree-dependent), then volatile (skills index,
  memory snapshot, time line, cwd last): project files before worktree text lets worktree sessions share a prefix. Skill-index
  changes are delivered once as a note on the next user message. A prompt-surface snapshot test (render the exact system prompt
  and tools[] to committed fixtures; the test fails when stale) shows a one-line change that rewrites the prompt. Subfolder
  AGENTS.md is appended to the tool result the first time the agent touches that folder (`agent/subdirectory_hints.py`).
- **Injection scanning of instruction files** (`tools/threat_patterns.py`): NFKC normalisation, invisible/bidi unicode, hidden
  HTML, role hijack, exfil patterns; applied to AGENTS.md, memory writes, skill installs, cron prompts. Ours has none for
  AGENTS.md, skills or web text (the known CSS-hidden-text gap). Port: `harness/threat_patterns.py`; ask the person instead of
  silently blocking a project file. S-M.
- **Structured output:** a delegated subagent takes an `output_schema` and gets exactly one retry with the validation errors.
- **Compression:** a fixed summary template (Goal, Constraints, Progress done/in progress/blocked, Key Decisions, Relevant
  Files, Next Steps, Critical Context) updated rather than redone; tail protected by token budget with tool call/result groups
  never split; the last unanswered tool round and the active todo list are never dropped; a pruned skill leaves a
  "reload with" marker; token count anchored on the server's reported `prompt_tokens` plus an estimate of only the new
  messages. Do not copy micro-compaction (rewrites the prefix) or prepare-ahead compaction (competes for the one GPU).
- **Tool result budget:** besides a per-result cap, a per-turn aggregate cap with the largest results spilled to disk first
  (`tools/tool_result_storage.py`). S.

## Tooling

- **Edit tool:** an ordered chain exact, line-trimmed, whitespace-normalised, indentation-flexible, escape-normalised,
  unicode-normalised, block-anchor; similarity strategies never under `replace_all`; ambiguous matches list the first 5
  locations; `is_already_applied` turns a re-sent edit into a no-op; failures show numbered "did you mean" snippets with a
  visible-whitespace diff; an empty or identical `old_string` gets "do not re-send this call unchanged". Read-before-write: a
  whole-file write over a file never read, or changed since, is refused; a repeated identical read answers "unchanged" and is
  blocked on the third. Port: `tadamoo/tools/fuzzy_edit.py` and read stamps in `FileSandbox`. M.
- **Background processes** (`tools/terminal_tool.py`, `tools/process_registry*.py`): `background=true`, `notify_on_complete`,
  `watch_patterns`, a `process_manage` tool (poll, log, wait, kill, write), a 200 KB rolling buffer; a foreground command over
  the cap is promoted to background; after two empty polls the model is told to end its turn and wait for the completion note.
  Ours has foreground shell only (10 minutes). M-L.
- **Programmatic tool calling** (`tools/code_execution_tool.py`): a script calls the allowed tools through RPC stubs (cap 50
  calls, only stdout returns); collapses a 10-step chain into one turn. Ours: personal skills already do this through
  `tadamoo/personal_skills/hosting.py`; expose the same stub module to `run_code`. M.
- **Schemas:** descriptions must not name tools of other toolsets; `tools/schema_sanitizer.py` rewrites schemas that the
  llama.cpp grammar converter rejects and a classifier strips `pattern`/`format` and retries. Check our `ToolRegistry.schemas()`
  against our grammar path.
- **Subagents:** the child gets goal, context, workspace and the project AGENTS.md, and a completion instruction (lead with
  outcomes, bullets, do not replay the process); depth cap, per-turn caps.
- **Skills:** description at most 60 characters; fixed section order (When to Use, Prerequisites, How to Run, Quick Reference,
  Procedure, Pitfalls, Verification). The review prompt that lets the agent write skills is good guidance: class-level skills,
  not session diaries; a pitfall is a rule plus one clause of why; never store negative claims about tools; user corrections
  are first-class signals. Skill usage telemetry and stale/archive (never delete).
- **Session search:** FTS5 over all past sessions, deduplicated by lineage, no model call. Ours has no tool to search earlier
  conversations. M.

## Control of workflows

- **Loop guard** (`agent/tool_guardrails.py`): identical (tool, args, result hash) streak (notice at 3, stop at 5); cycles of
  2-4 calls; failure counts that reset after any successful mutating call; failure-tolerant tools never halt on varied red
  commands; identical long results replaced by a stub referencing the first; per-turn caps (50 web searches, 50 subagents);
  pollers exempt; hard stops by default only on unattended runs; refusals carry a marker so they never feed the failure streak.
  The cycle algorithm is a port of another project: rewrite. Replaces our `LoopDetector`. M.
- **Error classifier** (`agent/error_classifier.py`): context overflow means compress then retry; role-alternation errors mean
  merge adjacent messages and retry; grammar-pattern errors mean strip and retry; format errors abort; deterministic errors are
  never retried unchanged. Ours retries every model error forever with growing waits: a 400 that fails again loops. Needs an
  owner decision against the "never blocked" rule. M.
- **Verify before stopping** (`agent/verification_stop.py`): after code edits with no passing check recorded, keep the answer as
  interim and nudge once or twice (what changed, what the last check printed, which command to run); prose-only edits never
  trigger it; opt-in in Hermes. Goals with quality gates: shell commands that must pass before a judge may say done. S-M.
- **Budget warning at 90% of max turns plus one grace call to summarise**, so a run that hits `--max-turns` ends with a report.
- **Cron:** a pre-run script whose stdout is injected, a script-only job (zero model cost), `context_from` chaining, an idle
  watchdog instead of a wall-clock cut, `next_run_at` advanced before dispatch so a crash cannot run a slot twice; an ESTOP
  file that pauses new work without killing running work. All S in `tadamoo/scheduler.py`.
- **Approvals:** the hardline floor and user deny globs run before any bypass; matching on deobfuscated command variants; a
  denial circuit breaker after N denials per session (S).

## Not to copy

The facade-plus-sibling-files layout, provider and billing machinery, LLM "smart approval" as an auto-approve gate (a model
reading attacker-influenced text, plus a second call on one GPU), micro-compaction, parallel tool/subagent execution, the V4A
patch dialect, telemetry, YOLO bypasses, and their guidance texts verbatim (tuned for GPT/Codex, naming their own tools).

## Licence

MIT: copying needs the copyright and permission notice kept. Small self-contained pieces are copyable (JSON repair,
`is_repetition_dominated`, `boosted_output_cap`, fuzzy-match helpers, `parse_context_limit_from_error`, the think scrubber).
Rewrite instead of copying: `_normalize_json_strings_for_schema` (ported from cline), the cycle detection (ported from
oh-my-pi), `PARALLEL_TOOL_CALL_GUIDANCE` (from cline), the Google guidance (from OpenCode), `agent/estop.py` (ported from gastown,
MIT: keep that notice if used). If code is adapted, add a Hermes entry (MIT text, commit above) to `THIRD_PARTY_LICENSES` and a
one-line header in the file.

## Owner decisions raised

Whether a deterministic model error may end a turn visibly; whether verify-on-stop and quality gates are on by default for
unattended runs; whether the agent may write skills by itself and use idle GPU time for a reviewer pass; whether untrusted
project files are blocked, warned or asked about.
