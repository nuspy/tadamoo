# Case studies: design questions answered by measuring

Some design questions of the harness cannot be settled by reasoning. Seven of them are set up here as experiments (E1 to E7), each with a
fixture, a scorer that is plain code, an A/A control and a decision rule written before the run. Nothing in this page has been run on the
model yet except E3, whose numbers are a check of the code (see its section). The real runs wait for the GPU and are rows 70 to 76 of
`tests/QUEUE_daytime.md`. Code: `tadamoo/eval/cases/`. Tests: `tests/test_cases_core.py` and `tests/test_cases_runs.py`.

## How it works

```
python -m tadamoo.eval.cases list                 # every experiment, its arms (with the flag that selects each) and its rule
python -m tadamoo.eval.cases e2 --endpoint http://127.0.0.1:7232 --k 5 --gpu-power-limit-w 400 --out e2.md --json e2.json --keep D:\eval-e2
```

| Option | Meaning |
|---|---|
| `--k N` | repetitions of every task under every arm (the experiment's own default, 5 or 3; below 5 the run warns, as `tadamoo.eval` does) |
| `--gpu-power-limit-w W` | **required** when the experiment uses the model: it labels every number in the report and the JSON |
| `--arms A,B` | only these arms; the baseline and its A/A control always run |
| `--only TASK_ID` | only these tasks (repeatable); a pilot is `--k 2 --only <one task>` |
| `--temperature T` | 0.6 by default so that repetitions differ (each has its own seed, the same for every arm at the same repetition); `model` sends none |
| `--keep DIR` | keep every trial's workspace for reading failures |
| `--window N` (E5), `--server-setup TEXT` (E7), `--deadline-s S` (E3) | the options of one experiment |
| `--out`, `--json` | the Markdown report and the full results |

The server must be up with the model loaded and nothing else using it (the request log is not used here; the numbers come from the answers'
usage). Run an experiment with the GPU at the power limit you write down, and run the arms of one experiment in one session: the runner already
alternates them (for every repetition and task the arms run in a rotating order, so no arm always goes first).

### A/A control, noise and the decision

Every experiment has a baseline arm `A` and its copy `A~aa`: the same settings under another name. The gap between the two on the primary metric
is **the noise of this setup**, measured instead of guessed (the older "about 3 points" in `tadamoo.eval` is a guess). A challenger beats the
baseline when, on the primary metric,

1. it is better than the pooled `A` and `A~aa` mean by more than the A/A gap, **and**
2. by at least the smallest effect worth acting on (written in each rule: 10 points for most, 15 and 20 where a trial is expensive and few), **and**
3. no guard is broken (a guard is a metric the change must not make worse, for example seconds or questions asked for nothing; it is judged
   against the larger of its tolerance and its own A/A gap).

Otherwise the report says *no difference beyond the noise* (or that the baseline is better, or that the challenger is better but breaks a guard
and is not adopted). If the A/A gap is already larger than the smallest effect, the report says so: only a difference above the gap counts, and a
bigger `k` is the cure. One A/A gap is itself an estimate: it is small by luck sometimes, which is why rule 2 exists.

### Reading a report

- **Per arm**: valid trials (a server or harness failure is *invalid* and left out of the means, listed at the end), the mean of every metric,
  the 95% interval of the success rate (Wilson), `pass^k` (tasks that succeeded in every repetition), and the **divergent tasks**: those that
  succeed sometimes and fail sometimes. A divergent task is where the arms' behaviour differs from run to run; read its kept workspaces first.
- **Divergent tasks** (section): for each arm, the divergent tasks with the first tool call (0-based index) at which a passing and a failing trial
  differ, from the traces of E1, E2, E5 and E9 (`tadamoo/eval/traces.py`). **Components used**: per arm, counts of skills loaded, sub-agents,
  memory recalls, hook blocks and feedback, stop-hook denials, compactions and technical-session calls by use. Both also close the `tadamoo.eval` report.
- **Decision**: the rule as written before the run, then one line per challenger with the A/A figures, the effect (better is positive), the
  threshold and each guard.
- **Metrics common to the experiments**: `success`; `passes` (rounds of the thing under test: Target Loop passes, gate fix rounds);
  `wasted_turns` (turns in which every call was invalid, refused, failed or a repeat of an earlier identical call); `prompt_tokens` and
  `cached_tokens` (the sum of the usage of **every** model call of the trial, planner, judge, triage and summary calls included);
  `seconds` (wall time). E3 and E7 report what they measure instead.
- A low score is read together with the workspaces kept by `--keep`: a task that fails in every arm is usually a bad task.

### Labels

Every number carries the GPU power limit of its run (`--gpu-power-limit-w`, in the report header and in the JSON). A run without a model
(E3) says in the header that it is a plumbing check and has no limit. These are development A/Bs; headline numbers are the maintainer's.

## E1. Target Loop: the first message

**Question.** Does sending the person's text alone as the first message, and then one message per task, reach multi-part goals more reliably than
the text and task 1 in one message (today)?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | `joined`: the text, a separator and task 1 in one message (the default) | Target Loop option `first_message: joined` |
| `B` | `separate`: the text alone, then one message per task | `first_message: separate` |
| `A~aa` | `joined` again | |

The option is new: *First message* in the dialog's Options (EN and IT, explained), `first_message` in the target's options
(`tadamoo/target.py`, `docs/target-loop.md`). With `separate` the model gets the whole target as an ordinary message and answers it, which may
already do part of the work; each task then follows as a focused message. The answer to the text alone is evidence for the judge like any task.

**Fixture** (`fixtures/shop_repo.py`): a tiny Python repository (`shop/`, `tests/`, `docs/api.md`, `README.md`) and eight goals of three or four
parts each: add a function with its test and a doc line; rename across code, tests and docs; fix two failing tests and add a README line; add two
cart methods with tests and a README line; thousands separators in the money format with test and doc; move helpers to a new module without
breaking importers; validation with tests and a documented error; a discount cap with tests, a version bump and a changelog.
Each goal has a reference solution; a test shows its checks fail on the untouched repository and pass on the solution.

**Scorer.** The goal's own checks on the final workspace: its tests (`python -m unittest discover`), file greps and a Python one-liner on the
behaviour. `success` is every check passing; `criteria_met` is the share that pass. The Target Loop runs with its verify command set, at most
4 passes and a stall limit of 3, the same for both arms. **Metrics**: success, criteria met, passes, wasted turns, prompt and cached tokens (all
calls), seconds, turns, tasks run. **k = 5**, 8 goals, 3 arms: 120 trials.

**Rule.** `B` replaces `A` when its success rate is higher than the pooled A and A/A rate by more than the A/A gap and by at least 10 points, and
it does not use more than 25% more prompt tokens or more than 25% more seconds than `A`. Otherwise `joined` stays the default.

**Not covered.** One model, one small repository, goals written by us; the judge is the model itself (the scorer is independent of it).

## E2. Verify gate: off or on

**Question.** On coding tasks that start with a failing test or a trap, does the gate get more tasks truly finished and fewer closed with a
test still failing?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | gate off | `--verify-gate off` |
| `B` | gate on with the task's test command | `--verify-gate always --verify-command "python -m unittest discover -q"` |
| `A~aa` | gate off again | |

**Fixture** (`fixtures/gate_tasks.py`): eight tasks, each a few files with unittest tests. Four have a trap, a change that fixes the failing test
and breaks another: a shared padding helper used by two formats; a mutable default argument whose fix must keep in-place updates; a README the
tests compare with the greeting; a mean of nothing that another module relies on raising. The others: an off-by-one, a missing function written
from its test, a parser whose second bug shows only after the first is fixed, a missing data entry. Each has a reference solution.

**Scorer.** The suite is run on the final workspace and the test files must be unchanged (editing a test does not count). `success` is both;
`finished_failing` is a run that ended with an answer while the suite still fails; `passes` is the number of fix rounds the gate asked for.
**Metrics**: success, finished_failing, passes, wasted turns, tokens, seconds. **k = 5**, 8 tasks, 3 arms: 120 trials.

**Rule.** `B` beats `A` when its success rate is higher than the pooled A and A/A rate by more than the gap and by 10 points, `finished_failing`
is not higher than in `A` (beyond the gap), and `B` takes no more than twice the seconds. The gate is then worth turning on by default for attended
runs; the default for unattended runs remains the owner's decision (`docs/harness-gates.md`).

## E3. Error classifier: off or on

**Question.** Against a server that answers with transient errors, errors no waiting can cure and context overflow, does the classifier turn the
turns that never end into visible results without ending a turn that waiting would have saved?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | classifier off: every model error retried | `--no-error-classifier` (`classify_errors=False` of the session) |
| `B` | classifier on (the default) | no flag |
| `A~aa` | off again | |

**Fixture** (`scripted_server.py`, `fixtures/error_scenarios.py`): a server on 127.0.0.1 that answers from a script. Eight scenarios: 429 then 500
then an answer; a stalled request (the client times out) then an answer; a mixed burst of four errors; a 400 "the model is still loading" then an
answer; a 400 malformed request for ever; a 401 for ever; a context-overflow 400 for ever; an overflow twice and then an answer. Waiting cures
the first, second, third, fourth and last; nothing cures the 400 and the 401; the overflow for ever needs the context to shrink.

**Scorer.** The real conversation process (`Session`) and the real client run against that server. `success` means the turn ended as the scenario
wants: an answer where waiting cures, an error shown where it does not. `never_ends` is no result before the deadline (1.5 s, the waits shrunk to
tens of milliseconds and the client timeout to 0.3 s); `wrong_end` is a turn ended on an error that waiting would have cured; `retries` are
the `retrying` events; `visible_s` is the time to the result; `real_wait_s` is what the default waits (2, 5, 15, 30, 60, 120, 180, 300 s, the last
repeated) would have added for those retries. **k = 5**, 8 scenarios, 3 arms: 120 trials, a few minutes, no model and no GPU.

**Rule.** `B` beats `A` when the share of turns that never end is lower than the pooled A and A/A share by more than the gap and by 10 points, and
`wrong_end` is not above `A`'s. The classifier is on by default; this says what it buys and what it risks.

**Plumbing numbers (not measurements of the model).** One run on the development machine, no GPU, k = 5, 120 trials, 0 invalid:

| Arm | success | never ends | wrong end | retries | seconds to a result | default waits that would have been added |
|---|---:|---:|---:|---:|---:|---:|
| `A` off | 62% | 38% | 0% | 4.6 | 0.74 | 388 s |
| `A~aa` off | 62% | 38% | 0% | 4.6 | 0.74 | 388 s |
| `B` on | 88% | 12% | 0% | 2.4 | 0.37 | 135 s |

A/A gap on `never_ends`: 0; effect +25 points; guard `wrong_end` kept; verdict "B is better than A". By scenario: the three transient scenarios, the
loading 400 and the overflow-twice scenario behave the same in both arms (answered after 1 to 4 retries); the 400 and the 401 never end in `A` (9
retries in 1.5 s) and end at once in `B` with no retry; the overflow for ever never ends in either arm, because overflow is retried by design
and compacting before the retry is the Compaction track's work. Scripted and deterministic, so the A/A gap is only timing: this run shows that the
plumbing and the scorer work and what the code does with these error shapes; it says nothing about the model.

## E4. Intake: off, rules or full

**Question.** On prompts that are ambiguous, dated, risky, long, injection-looking or plain, does looking at the request first make the agent ask
when it should and not when it should not, resolve dates right and finish the task more often, and what does it cost in time and tokens?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | no look | `--intake off` |
| `B` | cleaning, signals, date note, no model call | `--intake rules` |
| `C` | `rules` plus one triage call when the gate says so | `--intake full` |
| `A~aa` | `off` again | |

(The default is a setting of the Settings page; the three modes are compared as arms whatever it is.)

**Fixture** (`fixtures/intake_prompts.py`): twenty prompts, each in a workspace of its own and with a fixed clock, Friday 2026-10-30: three
plain; four with a relative date (tomorrow, the day after tomorrow, yesterday, next Monday, which the rules do not know); four ambiguous (an
unnamed file and name, which client, which table format, which bug); three risky with an unclear scope (old logs, overwrite the settings, remove
duplicates); three long lists (five, four and six items, in English and Italian); three where the text itself looks like an order (a note that
says to ignore the rules and delete everything, a sentence to translate, a message to copy). Each says whether a careful person would ask, what
that person answers, and the checks that decide the task afterwards; the reference solutions pass them and the untouched workspaces fail.

**Scorer.** The conversation is attended at the Default permission level (at Auto the harness asks nothing but named owners). The turn ends on a
card or it does not. A card is answered with the fixture's reply, in the words the server sends the model. `ask_correct` is asked-or-not matching
`should_ask`; `over_ask` is a question where none was needed; `missed_ask` is a question that was needed and not asked; `success` is the row's checks
passing; `date_ok` (dated rows) is the right date in the result; `good` is `ask_correct` and `success`. **Metrics**: good, success, ask_correct,
over_ask, missed_ask, date_ok, seconds, tokens (the triage call included), model calls. **k = 5**, 20 prompts, 4 arms: 400 trials.

**Rule.** A mode beats `off` when its `good` share is higher than the pooled off and A/A share by more than the gap and by 10 points, it does not
ask unneeded questions more often than `off` by more than 10 points (or the gap) and it does not take more than 25% more seconds. If both `rules` and
`full` qualify, the cheaper is recommended.

**Not covered.** The answer to a card is the fixture's, not a person's; "next Monday" tests the model's own use of the clock note.

## E5. Compaction inside one long turn

**Question.** In one request that takes well over a hundred tool calls, does summarising the older steps keep the work and the earliest standing
rules intact where masking alone does not, and what does it cost in tokens read again and in time?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | compaction off, masking only | `--no-compaction` |
| `B` | compaction on at 0.85 of the budget (the default) | `--compaction-threshold 0.85` |
| `A~aa` | off again | |

The window the agent plans for is the same for all arms (`--window`, 16384 tokens by default, small on purpose; run it again with 32768).

**Fixture** (`fixtures/long_turn_repo.py`): two generated repositories, 60 and 45 modules of about 55 lines (the same files for the same seed), a
little over two thirds with a misspelling, plus five legacy files with the same misspelling in `frozen/`. The request states three rules first
(every edited module ends with `# reviewed`; never touch `frozen/`; the final answer ends with an exact line) and then the job: for each module in
order, read it, correct the misspelling, append `mod_NNN: N functions` to `LEDGER.md` with N the real count of top-level functions. That is a read
and a ledger line per module and an edit for most: well over a hundred calls, and more text than the window holds.

**Scorer.** Typos left; the ledger lines against the real counts; `frozen/` byte for byte; and whether the first message's rules still hold at the
end: the last ten edited modules (by modification time) end with the trailer and the final line is the required one (`rule_obeyed`). `success` is no
typo left, at least 95% of ledger lines right and `frozen/` intact. **Metrics**: success, rule_obeyed, ledger_ok, typos_left, compactions made,
tool calls, tokens read again (prompt minus cached), prompt and cached tokens, seconds. **k = 3** (a trial takes a long time), 2 variants, 3 arms:
18 trials; a first pass is `--only long45 --k 2`.

**Rule.** `B` beats `A` when its success rate is higher than the pooled A and A/A rate by more than the gap and by 20 points, `rule_obeyed` is not
lower than in `A` (beyond 10 points or the gap) and `B` takes no more than twice the seconds. Tokens read again are reported and not decided on.

## E6. Long answers

**Question.** For a text longer than one reply writes well, which way delivers the words asked, keeps what must not change, repeats nothing and
joins cleanly: one call, parts of 1000, 2000 or 2500 words, or parts written from a plan?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | parts of 2000 words (today) | `long_answer` with `segment_words: 2000` |
| `B` | parts of 1000 words | `segment_words: 1000` |
| `C` | parts of 2500 words | `segment_words: 2500` |
| `D` | parts of 2000 words from a plan given in advance | `plan_long_answer` with `sections` (the outline of the fixture) |
| `E` | one plain call | no `long_answer` |
| `A~aa` | `A` again | |

`D` fixes the outline instead of asking the model for one, so that the plan's effect is not mixed with the model's planning luck; there is
nothing to plan in a translation, where `D` is left out. The run drives `serve/longform.py` directly with the model's client (the engine of both
the request field and the tool).

**Fixture** (`fixtures/longform_texts.py`): five tasks, the long texts generated from fixed seeds: write 3000, 6000 and 10000 words on a topic;
translate a 3,900-word English manual into Italian, with a code block, an image, a link, inline code, a table and citation keys that must survive;
analyze a 5,400-word incident report in at least 3000 words, discussing each of twelve incident ids.

**Scorer** (no model judge). Words delivered against words asked in total and per part (success needs 85% to 130% in total); protected items
byte for byte; paragraphs written twice and the share of repeated 8-word groups (at most 10%); seams between parts: a part cut in the middle of a
sentence, the same six words or the same word at both sides, a conclusion in a part that is not the last, a heading written twice; the language
of a translation; and the share of incident ids discussed (90%). **Metrics**: success, delivered ratio, smallest part ratio, protected intact,
coverage, seam issues, repeated paragraphs and groups, parts, warnings, tokens, seconds. **k = 3**, 5 tasks, 6 arms: 90 trials (the 10,000-word
ones take the longest).

**Rule.** A way beats the 2000-word parts when its success rate is higher than the pooled A and A/A rate by more than the gap and by 15 points and
it takes no more than twice the seconds. If several win, the one with the better delivered ratio is recommended; `E` is the reference for what one
call does. Row 67 of the queue still has the reading by eye.

## E7. Prefix cache behaviour

**Question.** For each request shape, what share of the prompt does the server report as cached, and how long is the time to the first token?

This is a measurement, so the arms are the shapes:

| Arm | Shape | Requests played; the last is measured |
|---|---|---|
| `A` | identical repeat (best case) | R, R |
| `A~aa` | the same again (the gap is the noise) | R, R |
| `B` | one new user turn | R, then R + the answer + a new user message |
| `C` | a changed tool list | R with six tools, R with a seventh added |
| `D` | a different system prompt, same tools | R, R with another system text from its first words on |
| `E` | a block in the middle changed | a conversation with two long blocks, then the same with the second block replaced |
| `F` | a side call between two turns | R, an unrelated short request, R + the answer + a new message |
| `G` | a request never seen (worst case) | a fresh conversation of the same size |

Every cycle has text of its own, so nothing is cached from an earlier cycle. The six tools are the same in every cycle, so even `G` shares the
tool block with the others: that is the floor, not zero. The prefix is 2000 and 6000 words.

**Scorer.** From the answer's usage: `cached_share` is cached tokens over prompt tokens of the measured request; `ttft_s` is the time to the first
streamed token. The report adds a *survival* table: (share - share of G) / (share of A - share of G), so 1 means the change cost the cache nothing
and 0 that it cost all of it, and the time to the first token as a share of G's. **k = 5**, 2 sizes, 8 arms: 80 cycles, a few minutes.

**Rule.** Read each shape against `A`. *No difference beyond the noise* means the change leaves the prefix cache alone; *A is better* means it
costs part or all of it, and the survival says how much. A shape keeps its cache when it is within the A/A gap and 10 points of the identical
repeat; it loses it when it is within the gap and 10 points of `G`. If `A` and `G` do not differ, the server reports no cache and the run says
nothing. **Run it once for each server setup that matters**: `--parallel 1`, `--parallel 2`, `--conversation-cache-mib 8192`, with
`--server-setup` naming it in the report; the comparison decides whether a side call such as the compaction summary needs the engine's
`scratch=1` proposal (`docs/compaction.md`).

## E8. Optimize prompt: off or on

**Question.** On requests, short, vague or long, that carry something a rewrite must keep (names, numbers, quotations, code, paths, links), does rewriting the request in
the technical session before the model reads it make the task end right more often, does it keep what the person wrote, and what does it cost in time?

| Arm | Setting | Selected by |
|---|---|---|
| `A` | the model reads the message as written | `--optimize-prompt off` |
| `B` | one short call in the technical session rewrites it first | `--optimize-prompt on` |
| `A~aa` | `off` again | |

The look at a request is off in every arm, so only the rewrite differs. `B` needs a server started with `--side-slot` (`--server-setup "side-slot=30000"`
names it in the report); without one its trials are marked invalid, not failed.

**Fixture** (`fixtures/optimize_prompts.py`): twenty-nine requests of four words or more, each in a workspace of its own. Twenty are short: five terse ones that
name files, four with numbers that must not change, three with a quotation, two with code, three with paths or a link, three in Italian. Three are vague one-liners
(the substance is in a file of the workspace). Four are long, 150 to 400 words: a status file with a numbered list of sections, inline code and two links; an
environment script given as a code block with ports and counts that must not change; a request in Italian with quotations, a date and an address; and a rambling
email in English with one quotation to copy and a sum to make. Two mix Italian and English. Each has checks that
decide the task; the reference solutions pass them and the untouched workspaces fail. What a rewrite must keep is read from the text by
`tadamoo.optimize.protected`, the same function the harness uses, so the fixture and the guard cannot disagree.

**Scorer.** The checks decide `success`. `protected_kept` is 1 when the message the model read still holds every protected item of the request
(`tadamoo.optimize.lost` finds none), whatever the arm; `good` is `success` and `protected_kept`. `optimized` is a rewrite that was used, `rejected` one
the harness threw away, `lost` one thrown away for losing content. `optimize_seconds` is the time of the call, `seconds` the whole trial. **Metrics**: good,
success, protected_kept, optimized, rejected, lost, optimize_seconds, seconds, tokens (the helper call included), model calls. **k = 5**, 29 requests, 3 arms: 435 trials.

**Rule.** `B` beats `off` when its `good` share is higher than the pooled off and A/A share by more than the gap and by 10 points, it never gives the
model a message that lost protected content, and it does not take more than 50% more seconds.

**Not covered.** The language of the rewrite is not checked (a rewrite into another language would pass if it kept the protected items); the requests are
file tasks, so the answer says little about open-ended work; the rate of `rejected` shows how often the model cannot be trusted to keep
content, which the rule does not weigh.

## E9. Context layers: what the agent still knows after a fold

**Question.** When a long agent conversation outgrows the context, what the agent can still answer about the folded part depends on who summarises. TaDaMoo's compaction, written from
the originals in the technical session ([context-layers](context-layers.md)), against masking alone. The design also compares the proxy folding old turns by itself, alone and stacked
with TaDaMoo's compaction; those arms need the proxy in the loop and are not built, so this case answers the first half.

| Arm | Setting | Selected by |
|---|---|---|
| `N` | compaction off, masking only | `--no-compaction` |
| `A` | compaction on, written from the originals | `--compaction-threshold 0.85` (the default) |
| `N~aa` | `N` again | |

**Fixture.** The long-turn repository of E5 (`fixtures/long_turn_repo.py`), run past the point where a compaction is due (a small window, as in E5), then ten probes put to the same conversation by
the harness with the tools taken away (`fixtures/context_probes.py`), fourteen of them about the first modules of the job, which a fold takes first. Eight are what the job needed later (task): how
many functions three modules have (the ledger lines), whether three modules had the misspelling, the closing line of the final answer, the folder that must stay as it is. Six are what no later step
needed (incidental): the name of the first function of three modules and the value of `LIMIT` in three others. Every answer is known from the generated files; each probe is checked by the text of the reply.

**Scorer.** `success` is the task's checks. `recall` is the share of probes answered right. `reread_tokens` is what the server read again (prompt tokens less cached tokens), `compactions` and
`generation` come from the compaction counter and the compacted records the conversation holds. `summary_cut` is the summary calls that ended cut off, `carry` the share of the first twelve
modules whose ledger line (`mod_003: 4 functions`, right count) can still be read in the conversation when the job ends, `artifact_reads` the calls that went to the saved copies of earlier results.
**Metrics**: success, recall_task, recall_incidental, recall (all), summary_cut, carry, artifact_reads, reread_tokens, compactions, generation, tokens, tool calls, seconds. **k = 5**.

**Large job.** `--only long200 --window 65536` (it runs only when named): 200 modules, one at a time with `read_file` and `edit_file`, scripts forbidden in the request, about 2.6 times the prompt budget of
that window, so masking and compaction both come due. The existing `long60` and `long45` are unchanged.

**Rule.** `A` beats `N` when its `recall_task` is higher than the pooled `N` and `N~aa` rate by more than the gap and by 10 points, its `success` is not lower by more than the gap, and it takes no more than
twice `N`'s seconds. Tokens read again are reported, not decided on.

**Not built.** The proxy arms; probes about error text and decisions (they need a transcript to know the answer); the second part, the Chat's short record on scripted chats; the gain of a
compaction made in parts.

## E10. Lessons of earlier work: off or on

**Question.** When a task has been met before, do the lessons kept from it ([awareness](harness-study/awareness.md), section 3) make the agent avoid its old mistake?

| Arm | Setting | Selected by |
|---|---|---|
| `N` | no lessons | `lessons_dir` None |
| `L` | the project's lessons store, filled before the run | `lessons_dir` "" |
| `N~aa` | `N` again | |

**Fixture.** Eight small tasks (`fixtures/lesson_tasks.py`), each with a typical recoverable mistake: a changelog that is `HISTORY.txt`, flags a script wants, a date in another form, a missing output folder, a folder
given without `--recursive`, a token whose place is not said, a unit written as a word, a path with a space. The lessons are written in the fixture in the Recorder's style (not captured from a first run, so the
test needs no model to make them) and all eight sit in the store for every task: the retrieval has to find the right one. **Scorer.** A script reads the final files. **Metrics**: success, failed_calls (errored, refused or
exit code other than 0), clean (no failed call), turns, tool calls, wasted turns, lessons_shown, tokens, seconds. **k = 5**.

**Rule.** `L` beats `N` when its mean failed calls per task is lower than the pooled `N` and `N~aa` mean by more than the gap and by 0.5, its success is not lower by more than 10 points or the gap, and it takes no more than 25% more steps.

## E11. Awareness under pressure: self block and course watch off or on

**Question.** In a job folded more than once in a small window, does what the harness tells the agent about itself keep the early rules and facts?

| Arm | Setting | Selected by |
|---|---|---|
| `N` | no self block, no `self_status`, no course watch; compaction on | `self_block` False, `course_watch` False |
| `S` | self block, `self_status` and course watch (with its fold-near note); compaction on | the defaults |
| `N~aa` | `N` again | |

**Fixture.** E5's long job and E9's probes (the same variants, the same small window, `--window`). **Scorer.** E5's: success, `rule_obeyed` (trailer in the last edits, closing line, frozen folder); E9's: `recall_task`,
`recall_incidental`. `course_checks` counts the watch's notes. **k = 5**.

**Rule.** `S` beats `N` when its `recall_task` is higher than the pooled `N` and `N~aa` rate by more than the gap and by 10 points, `rule_obeyed` and `success` are not lower by more than 10 points or the gap, and it takes no more than twice `N`'s seconds.

## E12. Course watch: off or on

**Question.** On tasks built to derail, does the watch on the course of a request cut wasted calls without making the agent give up on what could be finished?

| Arm | Setting | Selected by |
|---|---|---|
| `N` | course watch off | `course_watch` False |
| `W` | course watch on | the default |
| `N~aa` | `N` again | |

**Fixture.** Six tasks (`fixtures/derail_tasks.py`): a command that resets the connection three times, the same five times, a settings file that is not at the path in the request; and three that cannot be finished (an
upload refused for good, a file that is not anywhere, a check that always fails and could be edited to "pass"). **Scorer.** `completed` (the result is there), `abandoned` (it stopped with an answer that says what blocks,
no result claiming success, protected files untouched), `resolved` = either. **Metrics**: resolved, completed, abandoned, failed_calls, wasted_turns, tool calls, turns, course_checks, tokens, seconds. A card for the person
reaches these agents as the same note, since they have no `ask_user`. **k = 5**.

**Rule.** `W` beats `N` when its mean failed calls per task are fewer than the pooled `N` and `N~aa` mean by more than the gap and by 1.0, `resolved` and `completed` are not lower by more than 10 points or the gap (fewer wasted calls
must not be bought by giving up). Wasted turns do not count a command that ran and failed, so they are reported and not decided on.

## E13. Old chats: finding a conversation by other words, with cards or without

**Question.** When a person asks for an old conversation in words it does not contain (an abbreviation, the other language, a paraphrase), do the conversation cards put the right one among the first hits of `search_past`?

| Arm | Setting | Selected by |
|---|---|---|
| `N` | no cards: the text only | the use `conversation_card` off and the back-fill stopped |
| `C` | the cards a good technical session writes (fixture, written by hand) | the use on (the default) |
| `F` | the cards made by code from the text (no session) | no technical session |
| `N~aa` | `N` again | |

**Fixture.** Twelve past conversations on different subjects, in English and Italian (`fixtures/old_chats.py`), and sixteen questions: four with an abbreviation (EU, RGPD, IRPEF, RTX), six in the other language, six paraphrased.
**Scorer.** A script puts the conversations in a fresh index, asks one question through the `search_past` tool and reads the conversation of each hit in order: `hit3` (the right one is among the first three), `hit1`,
`rank`, `nothing` (no hit at all). No model is used, so a run is deterministic (k only repeats it). **k = 5**.

**Rule, written before the run.** `C` or `F` beats `N` when its hit@3 is higher than the pooled `N` and `N~aa` rate by more than the gap and by 25 points, and its hit@1 is not lower by more than 10 points or the gap.
hit@1, the mean rank and the share of questions that return nothing are reported and not decided on. What the first scripted pass shows (the plumbing, not a measurement of the model): the text alone finds 2 of 16, the
fixture cards 16 of 16, the cards made by code 2 of 16, because code cannot translate or expand an abbreviation; so the cards are worth writing only by the session, and the experiment to run next on a real model is whether
the session writes cards as good as the fixture's (the card text of a few real conversations, read by the person).

## Running order and cost

These are guesses until the first run. E3 and E7 are short. E2, E4 and E8 are many short trials (a few hours; E8 adds one short call per trial). E1 and E6 are slower (each E1 trial is
a whole Target Loop; each long E6 text takes several minutes), and E5 is the slowest (tens of minutes per trial). Start each with a pilot
(`--k 2` and one task, `--only`) to see it run, then run it with the `k` in its section. A pilot is not a result: below `k = 5` pass^k and the A/A gap say little.

An interrupted run continues with `--resume DIR` (also on `python -m tadamoo.eval`): each finished trial is written to `DIR/trials.jsonl` as it ends, and a run started again
with the same folder reuses the valid ones and redoes the invalid ones. It reuses them only if the fingerprint is identical (git commit and uncommitted changes in `harness/`, `serve/`,
`src/`; the model file from `--model-file`; sampling and server settings; arms; tasks; `k`); otherwise it stops and names the parts that differ. Keep `DIR` outside those three folders.

## Files

| Path | What |
|---|---|
| `tadamoo/eval/cases/core.py` | arms, trials, the A/A control, the decision rule, the report |
| `tadamoo/eval/cases/agentrun.py` | a counting client, an in-process Target Loop conversation, the session helper, the transcript measures |
| `tadamoo/eval/cases/e1_target.py` ... `e13_old_chats.py` | one experiment each |
| `tadamoo/eval/cases/scripted_server.py` | the scripted server of E3 |
| `tadamoo/eval/cases/fixtures/` | the task material: `shop_repo.py` (E1), `gate_tasks.py` (E2), `error_scenarios.py` (E3), `intake_prompts.py` (E4), `long_turn_repo.py` (E5), `longform_texts.py` (E6), `optimize_prompts.py` (E8), `lesson_tasks.py` (E10), `derail_tasks.py` (E12), `old_chats.py` (E13) |
| `tadamoo/eval/cases/__main__.py` | the command line |
| `tests/test_cases_core.py`, `tests/test_cases_runs.py`, `tests/test_cases_awareness.py` | the tests, CPU only |

## What the tests show

The fixtures load and every reference solution passes its checks while the untouched material fails them; each scorer gives a known-good result
and a known-bad one different scores; the decision rule picks a clear winner, refuses a difference the A/A gap could have made, and stops a winner
that breaks a guard; every arm reaches the setting it names (the Target Loop option, the `AgentConfig` field, the session flag, the segment size);
E1, E2, E4, E5, E6, E7 and E8 run end to end against scripted models and a simulated prefix cache; E3 runs for real against the scripted server.
Nothing here says how the real model behaves: that is what the queue rows are for.
