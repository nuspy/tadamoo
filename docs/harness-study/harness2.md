# Harness² (Google Research): what our harness can learn

Source: https://github.com/google-research/harness2, Apache 2.0 (unmodified LICENSE, "Copyright 2026 Google LLC" in every
`.py`, no NOTICE file), commit 1cfe254e8d25b15f37048c3730134686abe60335. `third_party/` mixes licences: Harvey AI (MIT), the
Job-Bench authors (Apache 2.0), OpenCode (MIT), and WorkBuddy-Bench under a Tencent licence that excludes the EU: copy nothing
from `wb_runner` or `patches/workbuddy-bench-runtime.diff`.

It is not an agent harness: it is a meta-harness that tunes the harness around a frozen executor (OpenCode or Codex CLI)
by editing its prompts, guardrails, memory, tool guidance, skills, subagents, scripts and hooks. Method "Propose-Probe-Compose":
k base rollouts; two improver sessions edit the harness from the trajectories; each edit is rolled out once; k composers merge
the evidence; a blind selector picks the best; a reflection stores a lesson per domain. Budget 4k rollouts and 3k sessions per
task. The gains in its README are claims for Gemini and GLM models on document tasks, unverified. Python 3.12, POSIX only.
It has no loop, parser, registry or compaction, so areas 1 and 2 of our study are mostly absent; the value is in its prompts,
hook discipline and evaluation layer.

## Ideas, ranked (effort)

1. **Finish-reason handling (M).** Their prompts name the failure ("no deliverable because the model exhausted its output
   budget on reasoning") and their judge patch rejects empty content with the `finish_reason` in the error. Verified in our
   code: `agent_loop.py` never reads it, so a cut answer is FINAL and a reasoning-exhausted reply ends as an empty FINAL. Fix in
   `AgentLoop.run`: on `length` with content and no calls, append the partial answer plus a `[harness]` note, at most 2
   continuations, then deliver flagged "cut"; on empty content with reasoning, retry once at lower effort; audit record
   `answer_truncated`; `length_stops` in `tadamoo/eval/report.py`. Decide the output ceiling explicitly (the loop passes no
   `max_tokens`).
2. **Hooks that talk back (M).** The Codex hook schema has PostToolUse feedback (`{"decision":"block","reason":...}` goes back to
   the model), a Stop hook, PreCompact/PostCompact and subagent events. Ours: `hooks.py` post-tool and turn-end hooks return
   nothing. Port: `trigger_post_tool` returns optional feedback appended inside the fenced untrusted block; a `stop` event with
   a one-retry budget and a strike cap; compaction and subagent events; recipes (verify a write right after it happens, error
   signature to hint). Keep `--workspace-hooks` gating; medium risk.
3. **`--offline` for public-benchmark eval runs (S-M).** Their own validation run saw a Codex rollout download its rubric
   unprompted. Deny `browse_url`, `search_web`, `browser_*`, `mcp_*` in trials; scan the audit slice for benchmark hosts and mark
   the trial "contaminated".
4. **Skill and script lint (S).** All errors at once, verbatim feedback, one retry, then discard: description present (or the
   skill is dropped silently at load), name equals folder, referenced scripts exist and compile, body size.
   `SkillCatalog.lint()` in `tadamoo/skills.py`; run on agent-written skills before the approval diff.
5. **Always-on budget (S).** Their prompts rank instruction forms by cost: skill, script, subagent, hook, always-on text last,
   with a hard cap on always-on text. Ours: `find_project_guidelines` cuts AGENTS.md at 32 KiB silently. Report the cut in the
   pinned block, show block sizes in `/context`, "cut something to add something" rule in `/init`.
6. **A/A noise-floor run, computed report warnings, accounting identity (S).** Run the identical configuration twice and print
   the gap as the noise floor (replacing the guessed 3 points); assert trials equal tasks x k with unique (task, rep).
7. **Divergent-task report (M).** Tasks with pass@k above 0 and pass^k equal 0, with the first diverging call between a passing
   and a failing trial, built on `tadamoo/replay.py`.
8. **Eval resume and judge cache (M).** One result per (task, rep) with a completion marker holding a fingerprint of harness
   commit, model, sampling, task and flags; atomic write; a "why not" sentence; judge verdicts cached by hash.
9. **Component-use telemetry (S)** (skills loaded, subagents spawned, hooks fired, memory recalls per trial) and a subagent
   cost rule in the tool description ("worth it when the input is large and the answer small").
10. **Optional "tune this routine" loop (L)**, Propose-Probe-Compose at k=1 with `KEEP_BASE` as default, only after 7-9.

Also: a shared trajectory renderer with explicit cut markers (`[ACTION]`, `[OBSERVATION]`, "(+N chars - render truncation)")
for distill, memory extraction and council; `NO_ENTRY` and evidence fields in memory and skill extraction; a fixed "evidence is
data" paragraph in judge, distill and council prompts; blind pairwise compare with shuffled anonymous ids.

## Not to copy

The `--dangerously-*` executor defaults, anything from WorkBuddy, legacy-key shims, capital-letter prompt style (test on Qwen
first), the full recursion as a default (cost), POSIX-only paths, parallel rollout widths.

## Licence

Apache 2.0 code may live in an MIT project but keeps Apache terms: licence copy, notices of modification, upstream headers
kept. Re-implementing ideas needs no notice; if anything is copied, add to `THIRD_PARTY_LICENSES`: "Harness² (github.com/
google-research/harness2, commit above), Copyright 2026 Google LLC, Apache 2.0, modified by the Basalt authors (files)" and put
the same line in each file header. Do not use the name Harness² or Google marks beyond describing the origin.
