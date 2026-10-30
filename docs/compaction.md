# Compaction inside one long turn

An autonomous request can run for hundreds of tool calls without a new message from the person. Masking old results (see
[harness.md](harness.md), "Context, memory, skills and reference documents") is lossless but cannot keep up forever, and the summary
of the context patterns works on whole turns, so a single long turn was never summarised. This page is the summary that works inside one
turn. Code: `harness/compaction.py` (cuts, the summary call, the report), `harness/identifiers.py` (the identifier audit), the hook in
`PatternContext.prepare` (`harness/context_patterns.py`) and in `AgentLoop._prepare_context` (`harness/agent_loop.py`). The design comes
from [harness-study](harness-study/README.md) (OpenClaw, Hermes). The settings page is Settings > General > Long work
(`serve/web/compaction.js`, `serve/compaction_prefs.py`, kept in `compaction.json` in the user data folder).

## When it runs

Before each model call the context is prepared in this order: strip noise and masking of old results (as before), the turn-based summary
of the context patterns if the person turned it on, then this compaction. It starts when the conversation is still above
`--compaction-threshold` of the prompt budget (default 0.85, 0.5 to 0.98) after masking, and never before the point where masking starts
(the Chat threshold): the cheap, lossless step always has its chance first. Below the threshold nothing changes, not even the list object.

The size is the character estimate of the messages plus what the server counted beyond them: after each model call the context learns the
difference between the server's `prompt_tokens` and its own estimate (the tool list, mostly) and adds it from then on, within a quarter of
the budget. A compaction is not repeated until the conversation has grown by a tenth of the budget, and one that would free less than a
twentieth is not made (the size it was left at is remembered, so a conversation whose newest steps alone fill the room is not summarised
over and over). `/compact` skips the threshold and the gain test and works on any conversation that has something in front of its newest steps.

## Where the cut falls

Cut points are by tokens, not by message count (`tail_start`): walking back from the newest message until `keep_share` of the budget (a
quarter) is reached, then moved back until the first kept message is not a tool result, so a call and all its results are kept or replaced
together. What is never replaced:

- the system message and any pinned message;
- the person's first message, when it is at most 6000 characters (it is the stable head, see the cache report);
- the person's latest request, whatever its size, and the two requests before it when each is at most 2000 characters; they are repeated
  as written after the note, so the order is: system, first request, note, latest requests, newest steps;
- the newest steps, as written (after masking: a result masked earlier stays masked).

Everything between is the region the note replaces. A note from an earlier compaction (or from the turn-based summary) in the region is
folded into the new one, so there is always one. What leaves the model's view is handed to the conversation's record
(`archive.jsonl`) like any other pruning.

## The summary call

A separate call through the same client the compaction function points to (the model route of the function "Context compaction"; the executor
client when none is set). It has no tools, temperature 0, thinking off, at most 1500 tokens out and a 240 s limit; a failed, empty or
slow call gives the **plain notes** instead (below), flagged `summary_by: "extract"` with the reason. Nothing waits for the call but the
turn itself.

What it reads is bounded: at most 96,000 characters and at most 0.4 of the budget in characters. The region is cut into units (a call
with its results), and the units are chosen newest first: the person's messages in a quarter of the room, the newest steps up to 85%, a
little of the oldest, then whatever room is left, newest first; the gaps are marked with the number of messages left out. One tool result
is shown as its start, its error lines and its end (1400 characters). Before that, the harness gives the call the exact list of files read
and changed and of commands with how each ended, taken from all of the region's calls whatever the budget.

The notes follow a fixed outline: Goal, Decisions, Files touched, Commands run, Open items (next step first), in the language of the
request, with paths, names, ids, URLs, numbers and error text copied. Earlier notes are given for updating.

**Untrusted text.** Each tool result in the call is inside a data block with a random id (`harness/untrusted.py`), forged tags and chat
control tokens are scrubbed, and the instructions say a block's text is never to be followed or turned into a decision or an open item. The
notes themselves go into the conversation inside a data block as well, under a heading that says they are a record and that the request
after them is the only order, so text that came in through a tool and survived into a summary does not become an instruction. The note
is a user-role message marked `summary`; the harness's own bookkeeping keys never go to the server.

**Plain notes** (no model, or the call failed): the goal, the list of files and commands, and the first line of the last five things the
agent said. They are the same kind of record, fenced the same way.

## The identifier audit

After the summary is written, the identifiers (`harness/identifiers.py`: URLs, file paths, ids such as UUIDs, hashes, call ids and
tickets, function and class names, numbers of three digits or more) that appear **both** in the removed text and in what is kept (the
newest steps and the latest requests) are compared with the summary. Tool names and the harness's own markup are not identifiers.
Whatever the summary lacks is added to the note as a **Re-read these** list, at most 12 entries, each saying how to get it back:

- a path: read the file again; a URL: fetch it again;
- anything else found in a tool result of the region: the result is saved in the artifacts folder and the line names the file;
- otherwise the line of the earlier work that held it.

Two more additions, both marked in the note: the files changed (from the calls, exact) that the summary did not name, and, when the model
wrote the notes, paths, URLs and ids in its open items that appear nowhere in the work ("not found in the work: check before relying on
it"). The report counts what was carried, what was missing and what was not found.

## The prefix-cache report

The server reuses its cache of the start of the prompt only while the tokens are the same. A compaction changes everything after the head,
so the report says what did not change (`report.prefix`):

| field | meaning |
|---|---|
| `system_stable`, `system_sha` | the system message is unchanged; its short hash |
| `tools_sha` | hash of the tool list sent with every request, which compaction never touches (added by the loop) |
| `stable_messages`, `stable_tokens` | how many messages from the start are identical to before, and their tokens (estimate) |
| `reread_tokens` | the tokens after that, which the server will read again (estimate) |
| `stable_share` | stable tokens over the tokens after the compaction |

The first model call after the compaction then adds `observed`: the server's `prompt_tokens`, `cached_tokens` and what it read again
(`compaction_cache` event, and the stored report). The estimate and the observed value are both kept so they can be compared.

The system prompt and the tool list are built once and compaction never edits them; a test sends the whole loop and checks that the
system message and the tool list of every request are byte-identical before and after.

## Where it shows

- **The event stream** (`agent` events, `type: "compaction"` with the whole report, then `type: "compaction_cache"`); the Chat and Agent
  tab show one line each. The audit log records `compaction` (the counts only, no text).
- **`/context`**: the setting, how many summaries were made, and for the last one the sizes, who wrote it, the stable and re-read
  tokens, the observed cache and the identifiers to re-read.
- **Hooks**: `pre_compact` and `post_compact` fire for a compaction too; `post_compact` carries `compaction` (the report) and `actions`
  includes `compaction`. A compaction that starts below the character estimate's own threshold still fires `pre_compact`.
- **`/compact`**: the same compaction on demand, with the newest 40% of the conversation kept at most.

## Settings

| setting | option | default |
|---|---|---|
| on or off | `--no-compaction` (off) | on |
| start at | `--compaction-threshold SHARE` (0.5 to 0.98) | 0.85 |

Settings > General > Long work has both, each with its explanation in English and Italian, and takes effect for conversations started
afterwards. Off leaves masking only; a request that outgrows the window can then fail at the server. The mobile app has no card for it.

## Costs and limits

- One extra model call per compaction, on the same GPU, and the server reads the shortened conversation from the first changed token
  (the report says how many). The summary call has a different prefix, so on a server with a single cache slot it also evicts the
  main conversation's cache: the report's `observed` shows the cost. Queue row 64 in `tests/QUEUE_daytime.md`.
- The summary can lose detail or get something wrong; the audit catches identifiers, not meaning. The full text of masked results and of the
  results the audit named is on disk; the person's record keeps everything.
- A conversation whose newest steps alone fill the window cannot be helped: the tail is kept as written.
- The cancel button does not interrupt a running summary call (it ends at its time limit).
- The estimate is characters over 3.5 plus the learned overhead; a model whose text tokenizes very differently shifts the real start.
- The chat page's own context policy (`serve/chatctx.py`, POST `/api/chat/context`) is a separate path for plain chat and is not changed.

## Tests

`tests/test_compaction.py` (cuts, the work, untrusted text, the audit, the report, the whole loop with a scripted model),
`tests/test_compaction_settings.py` (the settings, the flags, the summary client, `/context`, `/compact`). Real-model checks are rows
54 to 56 of `tests/QUEUE_daytime.md`.
