# Cold tools (a shorter prompt)

The definitions of the tools are most of the prompt's fixed part. With `--cold-tools` (an option of the agent, of `python -m tadamoo.eval` and,
later, of the conversation's settings) the rare tools are listed with their first sentence and the skeleton of their schema (names and types of the parameters, no descriptions), and the model asks `tool_help`
for the rest before it uses one. Off by default; it is kept only if the A/B below says so.

- **Cold:** the Word, Excel, PowerPoint and PDF writers, `repo_diagram` and the task tools (`tadamoo/cold_tools.py`, `COLD`). A bare agent lists
  51,362 characters of tools; with the switch 34,292 (33% less).
- **The list never changes during a conversation** (a changed list is read again). The whole definition comes back as the result of a
  `tool_help` call, at the end, where it costs the cache nothing.
- **Why a skeleton and not a free object:** the grammar of a tool call writes only the parameters its schema names. Measured on the real model:
  with `{"type": "object", "additionalProperties": true}` it wrote `{}` eleven times in a row; with the names and types it wrote the call right.
- **A call is still checked** against the real schema; a wrong call to a cold tool is refused with the reminder to ask `tool_help`. The grammar
  no longer forces what is inside a parameter (the edits of `docx_edit`, for one).

## The A/B

The same tasks twice, `--cold-tools` the only difference, `--k 5` or more, one reasoning level, one model, the power limit written down:

```
python -m tadamoo.eval --set documents --k 5 --endpoint http://127.0.0.1:7232 --out a.md --json a.json
python -m tadamoo.eval --set documents --k 5 --endpoint http://127.0.0.1:7232 --cold-tools --out b.md --json b.json
```

Also `--set core` (the cold arm must not hurt ordinary tasks). Rule written before the run: B wins if the tasks passed do not drop beyond the
noise (the difference of two runs of A) and the prompt's tokens fall by at least 30%. Read besides: extra turns, calls refused by the registry,
and whether the model asked `tool_help` before the first call of a cold tool.

## Result of the first A/B (24 November 2026)

The real model (Qwen3.8-Flash-Next Uncensored IQ4_XS), 600 W, temperature 0.6, k = 5, the same tasks and seeds in both arms, a limit of 400,000 tokens
per trial, a bare agent (no browser, computer or outside tools in the list).

| Set | Passed A | Passed B | Prompt tokens read per trial (all turns) | Turns per trial | Seconds per trial |
|---|---|---|---|---|---|
| `documents` (4 tasks) | 20 of 20 | 20 of 20 | 90,386 -> 80,545 (-11%) | 5.8 -> 6.9 | 24-39 -> 29-45 |
| `core` (10 tasks) | 50 of 50 | 50 of 50 | 51,111 -> 36,251 (-29%) | 3.4 -> 3.4 | the same |

- B asked `tool_help` before the first use of every cold tool in 19 of 20 trials (in the other one it called the tool first, and the call was accepted) and made no call the registry refused.
- Read it with its limits: one model, 14 tasks, five repetitions, and A already passes everything, so this can show that B does not lose, not that
  it gains in quality. Where the cold tools are used the saving is small (-11%) because each trial pays one more turn for `tool_help` and the PDF
  and PowerPoint tasks took 45 s instead of 31-39 s; where they are not used (`core`) the prompt is 29% shorter at the same time.
- By the rule written before the run (prompt tokens down at least 30%) B misses by a point on `core`. It is kept as an option, off by default.
  A prompt with the browser, the computer and outside servers listed is longer and has more to make cold: that is the next measure.
