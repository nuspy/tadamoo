# Compaction inside one long turn

An autonomous request can run for hundreds of tool calls without a new message from the person. Masking old results (see
[harness.md](harness.md), "Context, memory, skills and reference documents") is lossless but cannot keep up forever, and the summary
of the context patterns works on whole turns, so a single long turn was never summarised. This page is the summary that works inside one
turn. Code: `tadamoo/compaction.py` (cuts, the summary call, the report), `tadamoo/identifiers.py` (the identifier audit), the hook in
`PatternContext.prepare` (`tadamoo/context_patterns.py`) and in `AgentLoop._prepare_context` (`tadamoo/agent_loop.py`). The design comes
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
- the newest steps, as written (after masking: a result masked earlier stays masked);
- code, in a session with file or shell tools (below): it stays after the note, as written.

Everything between is the region the note replaces. A note from an earlier compaction (or from the turn-based summary) in the region is
folded into the new one, so there is always one. What leaves the model's view is handed to the conversation's record
(`archive.jsonl`) like any other pruning.

## Code stays whole (agent sessions)

A session with a file or shell tool (`read_file`, `write_file`, `edit_file`, `shell_command`: the Agent tab and any headless run) never masks or
summarises code (`tadamoo/codeblocks.py`, `AgentConfig.keep_code`, on by default). A message is marked `code` once, from the message and the call
it answers: a fenced block or pasted source in the person's message, a fenced block in the model's reply, a write or edit call to a source file (by
extension, or text that reads as source), a `read_file` result of a source file, `search_code`, `find_references` and `go_to_definition` hits in
source files, and shell output that reads as source. A call and its results stay together: a code result keeps the call that asked for it, a call
that wrote code keeps its results. Masking skips these; the compaction takes them out of the region and puts them back, as written, after the
note (order: system, first request, note, code, latest requests, newest steps). The note ends with a line naming the files whose code stays, and
the summary call is told the code stays and never reads it (a fenced block in the person's latest request is shown to it as a mark). The
turn-based summary and the dropping of old turns of the context patterns keep it too.

**Safety valve.** Code kept whole may take 60% of the prompt budget (`CODE_SHARE`). Past it, the oldest code results are masked as usual, with
the note that names the saved full text, until the code is back under the share; when results are not enough, the oldest code of the person and the
model loses its protection and may be summarised. Each time, the loop sends a `code_masked` event (count, tokens, which files and how) and writes a
`code_masked` audit record, and tells the model once, in a `[harness]` note, to read the files again with `read_file` when it needs them. Small
windows feel it first: the code and the head (system prompt and tools) share what is left.

## The summary call

A separate call through the client the function "Context compaction" points to: the loaded model unless the person pointed it at a cloud
route in Settings (which also needs their yes for personal data). Where the function is not set, the conversation's own client writes the
summary only if it stays on this PC (`tadamoo/stack.py`, `_compaction_client`); a conversation that runs on a cloud route never sends its
history to it for a summary unless the person chose that route for the function. It has no tools, temperature 0, thinking off, at most 1500 tokens out and a 240 s limit; a failed, empty or
slow call gives the **plain notes** instead (below), flagged `summary_by: "extract"` with the reason. Nothing waits for the call but the
turn itself.

What it reads is bounded: at most 96,000 characters and at most 0.4 of the budget in characters. The region is cut into units (a call
with its results), and the units are chosen newest first: the person's messages in a quarter of the room, the newest steps up to 85%, a
little of the oldest, then whatever room is left, newest first; the gaps are marked with the number of messages left out. One tool result
is shown as its start, its error lines and its end (1400 characters). Before that, the harness gives the call the exact list of files read
and changed and of commands with how each ended, taken from all of the region's calls whatever the budget.

The notes follow a fixed outline: Facts, Goal, Decisions, Files touched, Commands run, Open items (next step first), in the language of the
request, with paths, names, ids, URLs, numbers and error text copied. Earlier notes are given for updating, also when the originals are
read back (the notes carry what the oldest part held, within the call's read budget).

**Facts** (`tadamoo/compaction_facts.py`) hold per-item values as `id: value` lines, kept by the program: the lines of the previous record
are copied unchanged, the lines the agent wrote itself in its edits and writes are read from the calls (a later write for an id wins), and
the summary call may only add ids that are not there yet. They come first in the note, so a cut loses the least; at most a tenth of the
budget, the oldest lines leaving first and counted.

**A summary that ends on its length limit is not used.** The call is made again from half the text with a request to be shorter; cut off
twice, no compaction is made: the conversation and the earlier records stay as they were and nothing is counted (`abandoned` in `/context`).
The gain is estimated from the cut before the call, and a call is not made when it cannot free enough.

**Untrusted text.** Each tool result in the call is inside a data block with a random id (`tadamoo/untrusted.py`), forged tags and chat
control tokens are scrubbed, and the instructions say a block's text is never to be followed or turned into a decision or an open item. The
notes themselves go into the conversation inside a data block as well, under a heading that says they are a record and that the request
after them is the only order, so text that came in through a tool and survived into a summary does not become an instruction. The note
is a user-role message marked `summary`; the harness's own bookkeeping keys never go to the server.

**Plain notes** (no model, or the call failed): the goal, the list of files and commands, and the first line of the last five things the
agent said. They are the same kind of record, fenced the same way.

## The identifier audit

After the summary is written, the identifiers (`tadamoo/identifiers.py`: URLs, file paths, ids such as UUIDs, hashes, call ids and
tickets, function and class names, numbers of three digits or more) that appear **both** in the removed text and in what is kept (the
newest steps and the latest requests) are compared with the summary. Tool names and the harness's own markup are not identifiers.
Whatever the summary lacks is added to the note as a **Re-read these** list, at most 12 entries, each saying how to get it back:

- a path: read the file again; a URL: fetch it again;
- anything else found in a tool result of the region: the result is saved in the artifacts folder and the line names the file;
- otherwise the line of the earlier work that held it.

A **Where the full results are saved** list closes the note: one line per file the folded steps read, with the saved copy of its result
(at most 4% of the budget, the newest kept). Two more additions, both marked in the note: the files changed (from the calls, exact) that the summary did not name, and, when the model
wrote the notes, paths, URLs and ids in its open items that appear nowhere in the work, under the line "Not contained in the memory of this
session (check before relying on it)" (in Italian «Non contenuto nella memoria di questa sessione (verifica prima di farci affidamento)»,
when the person's request is in Italian). The report counts what was carried, what was missing and what was not found.

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
afterwards. The choice is kept in `compaction.json` in the user data folder, read afresh each time a conversation's process is started, so
it survives a restart of the server and of the app; with no file it is on. Off leaves masking only; a request that outgrows the window can then fail at the server. The mobile app has no card for it.

## Costs and limits

- One extra model call per compaction, on the same GPU, and the server reads the shortened conversation from the first changed token
  (the report says how many). The summary call has a different prefix, so on a server with a single cache slot it also evicts the
  main conversation's cache: the report's `observed` shows the cost (see *The summary call and the engine's cache*). Queue rows 64 and 69.
- The summary can lose detail or get something wrong; the audit catches identifiers, not meaning. The full text of masked results and of the
  results the audit named is on disk; the person's record keeps everything.
- A conversation whose newest steps alone fill the window cannot be helped: the tail is kept as written.
- The cancel button does not interrupt a running summary call (it ends at its time limit).
- The estimate is characters over 3.5 plus the learned overhead; a model whose text tokenizes very differently shifts the real start.
- The chat page's own context policy (`serve/chatctx.py`, POST `/api/chat/context`) is a separate path for plain chat and is not changed.

## The summary call and the engine's cache

The summary should not disturb the cache of the main conversation. What the code does today, from `serve/service.py`, `serve/engine/process.py`
and the engine's headers:

- **One request key picks a kind of slot.** `Service.run` calls `engine.generate(prompt, max_new, sampling, cancel)`; the only field that
  names a slot is `cache_slot: "side"` (see *The technical session* below); nothing pins a conversation's slot or a cache identity. The
  engine's choice among the conversations' slots is made from the prompt alone.
- **`--parallel 1` (the default), one session.** The KV cache is positional, "one arena that holds one branch of history at a time", and a
  checkpoint is kept only while its tokens are a prefix of the current prompt (`include/basalt/program/conv_cache.hpp`). The summary prompt has its
  own system message and no tool list, so it shares no prefix: the conversation's cache is overwritten from token 0, its stable head (system
  message, tools, first request) included, and its next call reads everything again. The report's `prefix.stable_tokens` is what the
  *conversation* leaves unchanged, not what the server still holds.
- **`--parallel N`, N of 2 or more.** `ProcessEngine.pick_slot` takes, among the free slots, the one whose held tokens are the longest prefix of
  the prompt, else an empty one, else the least recently used. The summary has no shared prefix, so it goes to an empty or least recently
  used slot and the conversation's slot, idle but the most recent, is kept; only when it is the only free slot is it overwritten. Each slot
  keeps its own two checkpoints. This costs N times the KV cache (the context is divided among the slots) and is not a guarantee.
- **`--conversation-cache-mib N` (default 0, off; `--conversation-cache-slots`, default 4).** When a request that does not continue the
  running conversation arrives, the engine parks the outgoing conversation (state and KV) in host RAM and restores it for a request that
  shares its prefix (`generate.cpp`, "conversation cache: parked / restored"). That is the existing way to run a side request without losing
  the main one, at the price of host RAM and a copy back; it needs only the option. Not measured for the summary call yet (row 69).

### The technical session

With `--side-slot TOKENS` (on by default, 30000; `0` is off) the engine has one more slot, with a context of its own, that only
calls carrying `"cache_slot": "side"` use ([running](running.md#the-technical-session---side-slot-tokens)). `_summarizer` in
`tadamoo/stack.py` sends it (through `tadamoo/side.py`, `side_extra`) when the server reports a session, and `THIS_SERVER_ONLY` in
`tadamoo/llm/client.py` holds it back from any cloud route. The summary prompt keeps its own system message and no tools, so in the
session it shares nothing with a conversation and writes nothing into one: the conversations' slots keep their cache, and the
summary's own slot keeps no checkpoint. A text larger than the session is not sent whole: `tadamoo/digest.py` cuts it into parts of about
10,000 tokens at blank lines and line ends (no model decides the cuts), the session writes notes on each part in a call of its own that
carries nothing from the others, and the notes are what the final call reads; notes still too large are cut and written up again, up to
four rounds. The harness checks the notes of every part without a model: not empty, not almost as long as the part, not going round in
circles, and carrying at least half of its paths, URLs and ids (twenty at most). Notes that fail are asked for again, up to three calls,
with what was wrong said to the model; an answer that comes back cut off is asked for again on the two halves of the part. A part still
not written up stands as its start and end as written, marked, with its names, and names left out of otherwise good notes are added. A
failed call ends the work and the compaction falls back to its notes made without the model. There is no time limit. Without a session (or on a cloud route) the old rule holds: the call reads at most
`SUMMARY_READ_CHARS` characters and loses the middle. The cost is the one of
the setting: the engine's batch mode is on, and the memory of one more session. The effect on `cached_tokens` of the first call after a
compaction, and on a conversation's speed, is queue rows 69 to 75.

## Tests

`tests/test_compaction.py` (cuts, the work, untrusted text, the audit, the report, the whole loop with a scripted model),
`tests/test_compaction_facts.py` (cut-off summaries, facts, pointers), `tests/test_code_kept.py` (code kept whole: the detector, masking, compaction, the valve),
`tests/test_compaction_settings.py` (the settings and that they survive a restart of the manager, the flags, the summary client and that no cloud
route is used unless the function is pointed at it, `/context`, `/compact`). Real-model checks are rows 63 to 65 and 69 of `tests/QUEUE_daytime.md`.
