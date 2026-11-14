# Inference settings: per conversation, per plan, and the machine default

The settings of the Chat panel (thinking level, sampling, the most a reply may write, steps per answer, end-of-turn bias, where the context is
masked and folded, stop strings, system prompt, structured output, context patterns) are not global. Each conversation keeps its own copy, saved
with it; each plan has its own; and there is one machine default that every new conversation and every plan starts from.

## Where each setting lives

| setting | kept in | used by |
|---|---|---|
| everything in the panel | the conversation's record, `inference` (`agent_sessions.json`) | the panel; the fields below are read from it |
| thinking level (`effort`) | record `effort` | `--effort` of the conversation's process |
| "When the context is full": policy and percentage | record `context_policy`, `context_threshold`, `compaction`, `compaction_threshold` | masking from the percentage; with **Compact** also the fold of older steps from the same percentage (`--compaction-threshold`, never below 50 or above 98); other policies: `--no-compaction` |
| temperature, most a reply may write | record `temperature`, `max_tokens` | `--temperature`, `--max-tokens` |
| other sampling (top_p, top_k, min_p, penalties, seed, reasoning budget, stop strings) | record `sampling` | `--sampling`, added to every model call |
| steps per answer, end-of-turn bias | record `max_turns`, `end_turn_bias` | `--max-turns`, `--end-turn-bias` |
| context patterns | record `context_patterns` | `--context-patterns`, read when the process starts |

System prompt, structured output, automatic continuations and the maximum answer tokens belong to the model-only Chat (`serve/web/chat.js`); a
conversation with tools keeps them in its record but does not send them. The model-only Chat has no record: its settings wait in the browser
until the page opens a conversation, and "New conversation" takes the machine default again.

`serve/inference.py` reads the panel's settings as these fields (`fields`), checks them (`normalize`) and gives the machine default. A new
conversation is made with `inference_settings` (the panel's values) or, with none, the machine default; a plain argument (`effort`,
`max_turns`, `context_policy`, `context_threshold`, `end_turn_bias`) overrides one value for that conversation. A record made before the
settings were kept with it gets them from its own fields when the manager loads it (`inference.from_record`).

## The machine default

The inference preset **Machine Default** of `presets.json` (the one the configuration wizard also writes). Settings > General > Inference
settings opens the same panel on it. With no preset the built-in values apply: compact at 80 %, the model's own sampling. Changing it does not
touch a conversation or a plan that already has its own copy; a plan whose settings were never edited has none and follows the default.

## Changing a conversation's settings

The panel is bound to the open conversation. A change is saved a moment later with `POST /api/agent/sessions/<id>/inference`
(`{settings: {...}}`, the whole panel); the running process hears only what changed, from its next model call (thinking level, steps, bias,
temperature, reply limit, sampling, and where the context is folded); the context patterns apply from the next start. `/effort` and the header's
Thinking list change the same setting. A wrong value is refused with the reason and nothing changes.

## Plans

- **The plan's settings**: "Inference settings" at the top of the plan's editor opens the same panel (without what only a conversation has:
  permissions, checkpoints, pictures, the Billion context link). They are kept in the plan's profile (`profile.inference`, saved with
  `plan:update_meta`). The conversation that carries the plan out, and the one that recovers a failed step, start from them
  (`SessionRoutineRunner`, `PlanService.inference_of`); with none, from the machine default.
- **Thinking per step**: an action step may have its own level (`effort`: `none`, `low`, `medium`, `high`; empty follows the plan), set in the step's
  detail. When the conversation starts the step (`plan_step_start`) its process is told that level, and the plan's level again when the step is
  marked done, failed or skipped. The conversation's own record keeps the plan's level.
- **All steps at once**: the list "Set all steps to…" at the top of the editor sends `plan:set_reasoning` (`{level}`): every action step that is not
  completed takes it; empty makes each follow the plan again.

## The summary call

The compaction's summary call is asked with thinking off (`client_for("compaction", ..., level="none")`): a route that thinks by default used the
whole reply limit on thinking and the summary was cut off twice, so the notes were made without the model. The reason a summary was not written
(`summary_error`, `summary_cuts`) is kept in the conversation's audit with the compaction. When the budget changes (the server's window or the
provider's limit), the size the last compaction left is forgotten, so the next check starts from the threshold alone.

## Mobile

The panel is a page of the web UI; it is one column from 360 px, every control is 44 px or taller and nothing needs hover. The Android app makes
its conversations through the same route without settings, so they start from the machine default; it has no panel of its own (only the
thinking level in the new-conversation sheet, which overrides the default for that conversation).
