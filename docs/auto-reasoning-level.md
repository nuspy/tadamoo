# Automatic reasoning level

How the harness could choose, step by step, how much the model thinks. This is a design and a measurement plan; nothing here is built yet.

## What a level is

The chat template turns `reasoning_effort` into three different prompts:

- **high** (`xhigh`, the default) puts a sentence at the very start of the system message asking for careful thinking, checked assumptions and alternatives.
- **low** puts a different sentence there, asking for brief thinking.
- **medium** puts no sentence.
- **none** (`enable_thinking` false) puts no sentence either and ends the prompt with an empty `<think>` block.

So the level is an instruction that shapes *how* the model reasons (depth, checking, alternatives), not only how long it reasons.

## The thinking has no cap

The harness never limits how long the model thinks: no token budget, no forced end of the thinking. Reasoning needs its own room, and a thought closed early can give a worse result than no thinking at all. The only lever is the level, which changes how the model reasons from the start and lets it finish. The server's `reasoning_budget_tokens` stays at its default of none and the harness does not set it.

## The cache

The prefix cache is valid only as far as the prompt is unchanged.

- **none and medium** share the whole prompt except its last tokens: switching between them keeps the cache.
- **low and high** change the first line of the prompt: switching to or from them invalidates the whole cache, and the context is processed again.

Two ways to switch levels without losing the cache: move the level sentence from the head of the system message to a trailing note that is not kept (the harness already has such notes for roles), or switch only between none and medium. Whether the model follows a trailing level sentence as well as the head one is not known and has to be measured.

## A level is not a capability

For this model more reasoning does not mean better results. Thinking longer costs time inside a task limit, can drift into unchecked assumptions, and tasks driven by feedback from the terminal can reward trying and looking over thinking. So:

- Do not assume the results are monotonic in the level. A task that fails at none may pass at high, and the other way round.
- Measure, for each task and level, the pass rate over several trials with the time and tokens used. The best level is the one with the highest pass rate; among equal rates, the cheapest.
- Name the setting by what it does and keep it tied to the model, its quantisation and its sampling. A table made for one model is not valid for another.

## Calibration

Run the same tasks at none, low, medium and high, with several trials each, and keep:

1. pass rate, seconds and tokens per task and level;
2. the number of steps and tool calls, to see whether low levels win by acting more;
3. the failures that no level fixes, analysed apart: they are missing knowledge, tools or environment, not missing reasoning.

A cheaper first pass runs one trial at none and sends only the failures to the next level; the tasks near the edge then get three trials with "pass" meaning at least two of three. This ladder finds the lowest level that passes but does not show whether a higher level would have done better, so run the full matrix on a sample first to learn whether the results are monotonic.

Keep the failed-attempts limit and the other settings equal across levels and write them in the report.

## Choosing the level for each step

The unit is the step, not the whole prompt: planning and surprises need more thought than a routine command. Pass or fail belongs to the whole task, so there are no per-step labels. Compare whole policies on the same tasks instead: fixed none, fixed medium, fixed high, and an adaptive one that picks the level of each step. The adaptive policy wins if it passes at least as often for less cost.

The deciders, from the cheapest:

1. **Signals, no model.** The last result has an error, failed attempts went up, the step is the first one, the step is routine, the task was retried. The harness already counts failed attempts. After repeated failures the level may change in either direction: a different approach is worth more than insisting.
2. **The technical session**: the main model at no reasoning, a small context (the goal and the last result), a one-word answer.
3. **A small separate model**, only if the first two are not enough. Basalt-Plus runs only Qwen3.8-Flash-Next, so a small model needs another runtime and is a new dependency.

A cloud service is never used in a benchmark. For normal use it needs the person's consent, because the prompt leaves the PC.

## Rules to extract

From the groups of tasks and steps that each level solves, write down the features that separate them as general rules, such as the kind of step, the evidence of an error, the size of the unknown, the need to check a value. The rules are derived from the benchmark, so a score obtained with them on the same tasks is labelled "calibrated on this benchmark"; a held-out set or another benchmark gives an unlabelled one.

## Open questions

- Does a trailing level note work as well as the head sentence?
- Is the technical session enough as a decider, or does a small model add accuracy?
- Which signals separate the steps that need more thought, and do they carry over to other benchmarks?
