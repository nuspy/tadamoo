# OpenClaw: what our harness can learn

Source: https://github.com/openclaw/openclaw, MIT, "Copyright (c) 2026 OpenClaw Foundation", version 2026.10.8, commit
00125bb5bbc0bb8e9fb91ee39e6420132041b90e (read, never run). A multi-channel personal-assistant gateway in TypeScript/Node (about
61,000 files). Core agent code: `src/agents/`, `packages/agent-core/`, `packages/tool-call-repair/`, `src/security/`, `src/cron/`.
Exceptions: `skills/skill-creator` is Apache 2.0; code descends from Pi (MIT, Mario Zechner) and Hermes Agent (MIT). Their stance:
risk is enforced at runtime, not in prompt prose; prompt injection alone is declared out of scope. They target cloud models; local
support is thin, so several ideas matter more for a local Qwen on one GPU than for them.

## Instructions to and from the model

1. **Recovery for empty, reasoning-only and length-cut turns (S-M, top).** One tool-free finalisation pass or one continuation with a
   fixed instruction, never certifying unfinished work; after an output-limit cut: "Report only what the tool results show was
   done, and say plainly what was not finished" (`src/agents/embedded-agent-runner/run/incomplete-turn-recovery.ts`, with a Qwen
   test). Ours: `agent_loop.py` never reads `finish_reason`, so a thinking Qwen that spends its budget in `<think>` ends as a
   successful empty FINAL. Port: read `finish_reason`; if content is empty or `length`, add a `[harness]` note and make one more
   call, once per request, then a distinct stop reason. About 80 lines plus mock-engine tests.
2. **Stable tool surface and prefix-cache hygiene (S, verified on our template).** Tool definitions, system prompt and history
   stay byte-identical between turns; per-turn restrictions gate execution and do not remove tools; volatile text sits after a
   cache boundary; diagnostics name the section that changed. Ours: `serve/chat_template.jinja` renders `# Tools` first;
   `stack.py switch_mode` registers or removes `present_plan` and `registry.schemas()` sorts alphabetically, so a plan-mode toggle
   re-reads the prefix (inferred effect; confirm with the `cached_tokens` counters). Port: always register `present_plan` and refuse
   it outside plan mode; order schemas by volatility (stable core, then session-specific, MCP last); a per-section hash line in
   `/context`; catalog changes as a tail note.
3. **Untrusted-content wrapper (S).** A random 8-byte id in the boundary tag, forged markers replaced (fullwidth, CJK-bracket and
   zero-width variants folded first), model control tokens (`<|im_start|>`, `<|im_end|>`, `<|endoftext|>`, Llama, Mistral, Harmony,
   Gemma) replaced by `[REMOVED_SPECIAL_TOKEN]`; the warning stays at the data/instruction boundary (listing actions made models
   refuse legitimate requests). Ours: `fence_untrusted` uses a fixed `<untrusted_data>` tag and removes only the exact closing
   string; nothing strips control tokens from page or tool text. Also strip Unicode categories Cc/Cf/Zl/Zp from untrusted strings
   placed in the system prompt (workspace names). About 60 lines; hash loop comparisons with the id normalised.
4. **Argument coercion and tool-name repair (S)** (case-insensitive unambiguous match, trimmed, junk after a quote, namespace prefix
   stripped; "2" to 2 but "00123" keeps its type; invalid-argument errors carry the expected signature and the received type).
   Ours dumps all tool names on an unknown tool.
5. **Streaming repetition watch (S).** A rolling check on streamed text and reasoning (first at 16k characters, then each doubling,
   60-character window, 5 repeats) aborts the stream, drops partial tool calls and retries once with a higher repetition penalty.
   Origin is Hermes (`agent/repetition_guard.py`): port from that Python original. Keep the "distinct lines" guard for tables.
6. **Compaction inside one long turn (M-L).** Summariser input budgeted newest-first (newest messages verbatim in half of a 160k
   character budget, the user's earlier messages in a quarter, the oldest in a tenth, gaps marked with counts); cut points by
   tokens so tool calls stay paired with results; an identifier audit; a deterministic fallback when the summariser times out; a
   silent "write durable notes" turn before compaction. Ours: `_drop` and `_summarize` return unchanged when `len(groups) <= 2`
   (a single autonomous request with hundreds of tool calls is only masked, never summarised) and the summariser gets
   `text[:24000]`, the oldest part, so the newest is lost. Port in `harness/context_patterns.py` and `stack.py`; 250-350 lines.
7. **Bootstrap files with budgets and a notice (S).** Per-file and total caps with a short notice telling the model to read the
   file directly; sub-agents get only AGENTS.md; a `SOUL.md` voice file split from the rules. Ours cuts AGENTS.md at 32 KB silently.
8. **Execution-bias block (S).** "Actionable request: act now; weak or empty result: vary query, path, command, source, then
   conclude; mutable facts: live-check; a final claim needs evidence or a named blocker; promising future work creates an
   obligation to schedule it." About 6 lines in `SYSTEM_CORE`, measured with `harness.eval`; it fits "the user chooses the risk"
   because it tells the model not to pre-refuse.
9. **Block chunking for chat surfaces (M, only if long replies go to channels or the phone).** Bounded blocks with a break chain
   (paragraph, newline, sentence, whitespace, hard cut); never splits inside a fence (closes and reopens it); a table stays in one
   chunk or splits at rows repeating the header; clamps to the channel limit. A pure function `chunk_markdown(text, limit)`; also
   useful inside the long-answer design.
10. **Progress card (S-M):** one status message edited in place with an ordered plan (max 50 steps, at most one `in_progress`).
    Other small items: model-facing tool results split from UI-only details plus a `terminate` flag; typed one-shot judgments
    (Choice/Score/Boolean) that our grammar decoder could make parse-proof for `router.py` and `pipeline.py`.

## Tooling

- **Edit tool (M):** `edits: [{oldText, newText}]` matched against the original, non-overlapping; exact first, then fuzzy (NFKC,
  smart quotes, Unicode dashes and spaces to ASCII), refusing a fuzzy match across an ambiguous boundary; a miss returns the
  closest lines with percent match, a caret under the first difference and a hint (indentation 2 vs 4, escaping, column N);
  original line endings restored; the write is read back; edits to one file are queued. Optional `apply_patch`
  (`*** Begin Patch`). Accept only a unique fuzzy hit and say so.
- **Background process manager (M-L):** `exec` returns after `yieldMs` with a session id; tools list, poll, log, write, kill; poll
  backoff 5, 10, 30, 60 s without new output; completion wake after exit; the prompt says never to sleep-loop. Ours is synchronous
  with a 10-minute cap and the job object kills everything when the command ends.
- **Policy groups and "removed by" (S):** `group:read`, `group:write`, `group:web`, `group:shell` in `serve/allow_rules.py`;
  `/permissions` prints which rule from which file removed a tool; deny always wins.
- **Skill readiness (S):** `requires.bins/env/config` and `os`; hide skills whose binary is missing and list them as "not ready";
  the skill block shortens descriptions before dropping any.
- **Hooks that can ask or rewrite (S):** a `before_tool_call` decision `ask` (routed to the permission question) and rewritten
  arguments that go back through validation and the secret scan.
- **Parallel read-only calls (M):** run `EffectClass.PURE` calls in a thread pool after the sequential permission checks; gain is
  wall-clock only.
- Smaller: validate Mermaid before handing it to the page; the browser tool returns a fresh snapshot after navigation and exposes
  network and console logs; the per-result cap scales with the window (30% share) instead of a fixed 40,000. Code Mode (the model
  writes one program that calls the hidden tool catalog) saves round trips but is the largest security surface: evaluate, do not
  rush.

## Control of workflows

- **Steering at tool boundaries (S):** a message that arrives while a batch runs lets the running call finish, skips the unstarted
  sequential tail with synthetic paired results ("Skipped to process an incoming message"), then appends the steer. Queue ideas:
  a cap with `drop: summarize`, a 500 ms debounce, `collect` mode, bounded priority aging.
- **Loop detection by outcome (S-M):** calls hashed with their results; detectors for identical repeated error, repeat, poll with
  no progress, ping-pong A/B, argument churn (different arguments, same result), a global breaker (history 30, warn 10, critical
  20); volatile fields (durations, pids, timestamps) removed before hashing; a guard after a compaction retry.
- **Retry:** Retry-After parsing for cloud providers and the rule that local faults never trigger model fallback; our "retry
  forever" matches the owner's choice for one local model.
- **Cron and routines (M):** per-job `record_result` (`no_change | progress | done | blocked | needs_attention`, up to 2000
  characters) carried into the next prompt; a condition-trigger script that wakes the model only when it returns `fire: true`
  (a poll costs no model time on a single GPU); pacing and active hours; restart reconciliation.
- **Standing grants (optional):** an "always allow" minted by an automation, bound to the exact job, command, cwd and environment,
  expiring and revocable; a lighter level between "ask" and Automatic, only if the owner wants it.
- **Sessions and privacy (S):** incognito threads; a side question that never writes history; memory provenance with "forget
  everything derived from this conversation" (fits `personal_memory.py`).

## Ranked

1 empty/length-cut recovery; 2 compaction inside one long turn; 3 stable tool surface; 4 edit tool upgrade; 5 untrusted-content
hardening; 6 argument coercion and name repair; 7 background process manager; 8 routine upgrades; 9 repetition watch plus outcome
hash loop detection; 10 steering at tool boundaries.

## Not to copy

The gateway and channel architecture, the plugin SDK and ClawHub, provider plumbing, database-backed state with writer claims, a
model auto-reviewer for commands as a default (GPU time, weaker than a rule, against "the user chooses the risk"), Ultra mode and
proactive sub-agents, the secret egress proxy and Docker sandboxes for now, the injection regex list as a defence (they only log
it), the persona rewrite prompt, the GPT-5 overlay.

## Licence

MIT: keep the notice with any substantial copy. Prefer re-implementing in Python; verbatim only short tables and strings (the
control-token list and marker fold table, the recovery instructions, the fuzzy-fold regexes, the poll backoff schedule).
Lineage: `repetition-guard.ts` is adapted from Hermes (port from the Hermes original and credit Nous Research); the loop and
session tools descend from Pi (add its notice if used); do not copy `skills/skill-creator` (Apache 2.0) or the bundled skills
(unaudited). If code is used, add an OpenClaw entry (MIT, commit above) to `THIRD_PARTY_LICENSES` and a one-line header in each
file.
