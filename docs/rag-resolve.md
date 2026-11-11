# Which archives apply: the resolver

One place answers "which knowledge archives may this function use now": `resolve(store, context)` in `tadamoo/rag/resolve.py`. The agent harness, the
search route, plan steps, routines and the voice functions all take its verdict; none keeps a list of its own. It reads only each archive's `meta.json`.

## Context

A dict. `function` is required: `agent`, `plan_node`, `routine`, `meeting`, `copilot`, `call`, `avatar`. The rest is what the function has.

| Key | Meaning |
|---|---|
| `session`, `plan` + `node`, `routine`, `meeting` | the ids of the record the function runs for; the server looks them up (`sources`) |
| `project` | a project key: its own archives apply |
| `attached` | archive ids added to the project's |
| `routed_to_cloud` | the caller's word that the model call leaves this PC; it can only make the rule stricter |
| `audience` | `me` or `others`; it can only make the rule stricter. `meeting` and `call` are always `others`, `copilot` is always `me` |
| `granted` | archive ids the person agreed just now to have said to others; it never lifts a `deny` |

The server does not trust the caller: where the model runs is worked out from the providers registry (`routes_outside`: the function's fallback chain
has a provider that is not this server; a swap counts; an unreadable registry counts as outside) and OR-ed with `routed_to_cloud`. For `agent`,
`plan_node` and `routine` that is the `executor` route; the voice functions use the loaded model, so they are not outside. With a `session` that has a
record, the record's workspace and `knowledge` replace the caller's `project` and `attached`.

## Answer

`Resolution` (`to_dict()` is what `GET/POST /api/rag/resolve` returns, this PC only):

- `archives`: each `{id, name, project, project_name, description, documents, cloud_ok, others_ok, via, verdict}`. `name` is the archive's title, what the person
  recognises. `via` is `project`, `attached`, or the function that declared it. `verdict`: `allow`, or `ask_dialog` / `ask_voice` when the audience
  is others and the archive's `others_ok` says to ask first, in a dialog or by voice. Whoever speaks, writes or shows the content to others asks, then
  asks again with `granted`.
- `blocked`: `{id, name, project, project_name, codes, reason, verdict: "deny"}`. `codes` holds `cloud` (route outside, `cloud_ok` off), `others`
  (audience others, `others_ok` is `deny`), `missing` (the archive is gone). `reason` is a plain sentence.
- `ids` (verdict `allow`), `ask_dialog`, `ask_voice`, plus `function`, `project`, `routed_to_cloud`, `audience`.

`describe(resolution)` writes the names (and what is left out) in one line, never ids.

## `cloud_ok` and `others_ok`

`cloud_ok` (default off): the passages may go to a model outside this PC. `others_ok` (default `ask_dialog`): the content may be said, written or shown to
other people: `allow`, `deny`, `ask_dialog` (a dialog at that moment) or `ask_voice` (asked aloud). Both are set in the archive's settings, through
`POST /api/rag/archives/<id>/update`. The private copilot window needs no consent. Archives are not confidential: no Rule-of-Two change.

## The route can change after passages are in the history

`ProjectRecall` (`tadamoo/rag/recall.py`) remembers each passage it sent from an archive that is not cleared for cloud models (`withhold.py`). When
the conversation's route leaves this PC, `agent.outgoing` replaces them in what is sent (a line that repeats a run of eight words of one counts) with
`[passage withdrawn: its archive is not cleared for cloud models]`; the kept history is unchanged, so going back to the local model keeps its cached
prefix. A council role outside the PC reads its question and context through the same filter. A paraphrase by the model is not caught.
Compaction clears `ProjectRecall.sent`, so passages lost to it can be sent again. A harness process searching through the server only checks that
its archives exist (their meta) and sends a `context` so that the server applies the rule too.
