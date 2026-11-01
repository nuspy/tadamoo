# Harness study

What our agent harness (`harness/`) can learn from other open-source harnesses (Hermes, OpenClaw, Harness², Goose, Roo Code), and two designs that follow from it. The
studies read the sources and compared them with `harness/` and `docs/harness.md`; nothing was run. Everything is a proposal
until built and measured with `python -m harness.eval`, with every number labelled with the GPU power limit.

- [long-answers.md](long-answers.md): why final texts come out shorter than asked, cheap fixes, and a segmented long-answer mode.
- [hermes.md](hermes.md): Hermes Agent (MIT).
- [openclaw.md](openclaw.md): OpenClaw (MIT).
- [harness2.md](harness2.md): Harness² from Google Research (Apache 2.0), a meta-harness that tunes the harness around an executor.
- [intake.md](intake.md): what should run between the user's text and the first tool call (cleaning, triage, ask-or-act, decomposition, re-planning).
- [clarification-survey.md](clarification-survey.md) and [ask-user-v2.md](ask-user-v2.md): how agents ask clarifying questions and suggest the next prompt, and the design of `ask_user` v2 with single, multiple and free answers.
- [guidance-texts/](guidance-texts/): inventory of every text the harness writes to the model, evidence on how Qwen-class models read guidance, findings, ranked candidate rewrites and the experiments G1-G6 and M1 that would test them (the weight of the tool JSON is the main finding).
- [awareness.md](awareness.md): the agent's knowledge of itself, its target and its limits, lessons kept in a file and reused, proactive course checks, option cards.
- [vibe-coding.md](vibe-coding.md): a coding mode for the Agent tab, from Goose (Apache 2.0) and Roo Code (Apache 2.0).

## What comes out first, across all studies

1. **Read `finish_reason` in `AgentLoop`.** Verified in our code, found by three studies: a cut reply is delivered as FINAL and a
   reply that spent its budget on thinking ends as an empty FINAL. Continue on `length` (bounded), retry once at lower
   reasoning effort on an empty reply, a visible "cut" flag, a repetition guard. Also stop forcing temperature 0 on every turn.
2. **A completion gate.** Nothing can stop the agent from finishing before the checks pass: a Stop hook (deny becomes a hidden
   user message, capped) and a verify step with the project's own commands, three rounds at most.
3. **A better `edit_file`:** several edits per call, a unique-match tolerance cascade, "did you mean" with line numbers on
   failure, read-before-write.
4. **Tool-call repair:** JSON repair, schema-guided coercion, tool-name repair, mixed batches.
5. **Hooks that talk back** (post-tool feedback, stop, compaction and subagent events) and a stronger loop guard (result hash,
   cycles, progress reset, identical-result stubs).
6. **Honest evaluation:** an A/A noise floor, divergent-task reports, resume with a fingerprint, `--offline` for public
   benchmarks.
7. **Prefix-cache hygiene and compaction inside one long turn** (OpenClaw): a stable tool surface (a plan-mode toggle re-reads
   the prefix today), and a summariser that works on a single long autonomous turn and keeps the newest text.
8. **Long final texts in segments** with protected content (images, links, code, tables, diagrams) and deterministic checks.

## Licence notes

No code has been copied. Each study lists what is copyable and the attribution to add to `THIRD_PARTY_LICENSES` if some is.
Do not use anything from the WorkBuddy part of Harness² (its licence excludes the EU), and rewrite rather than copy the pieces
that Hermes itself ported from other projects. Publishing and licence statements are the owner's decisions.

## Decisions for the owner

Whether a deterministic model error may end a turn visibly (today every model error is retried forever); whether verify-on-stop
and gate commands are on by default for unattended runs; whether the agent may write skills by itself; whether an untrusted
project AGENTS.md is blocked, warned or asked about; the name of the new coding "role"; the quantisation question for short
answers (IQ4_XS and KV q8_0).
