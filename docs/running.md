# Running the server

The long-form notes behind the settings in [configuration](configuration/index.md): concurrent requests, the KV cache, memory floors and image input. Diagnostic flags: `bin/basalt --help`. What the server prints and the stats page: [console](console.md); the engine's `PROBE` commands: [probe](probe.md#probe-rates-engine-command); lending VRAM beside the elastic KV: [VRAM arbiter](engine-vram-arbiter.md).
Settings: [configuration](configuration/index.md). Diagnostic flags: `bin/basalt --help`. Several models: [models and the router](configuration/models.md).

## Start-up

The page is up at once and the model loads behind it. The header reads `Loading` with the percent over a progress bar while weights load; choosing a model in the picker loads it, and the Unload button beside the model name unloads the loaded one until the next request; each reads `Loading…` or `Unloading…` until the server shows the change, and a refused one shows its error in the header. `/health` and `/metrics`
show the load: `load.percent` and `load.gib`, the GiB placed so far in one figure over every byte the model loads (a card, a second card, RAM), where it was one figure per target. A failed start-up load ends the server. Requests wait during a load and get a 503 if it fails.

## Concurrent requests: `--parallel N`

`--parallel N` (1..8, also `"parallel"` in the config's `server` section) serves N requests at once in the
engine's batch slots; the default 1 is the single-stream path. Further requests wait in arrival order (a later one
may go first only to resume a slot that holds its conversation, a few times at most). A slot is freed when its
request finishes, is cancelled or its client leaves. `/metrics` reports `live.slots` (`total`, `active`,
`queue_depth`). As in llama.cpp, `--max-context` is shared: each slot holds `--max-context / N` tokens (`--parallel 4
--max-context 262144` gives four slots of 65536; ask for `--max-context 1048576` for four of 262144). The solo
path, used while one request runs alone, has the same length, so a request that fits a slot never outgrows it, and
one that does not is refused with a 400 naming that length. `/slots`, `/props` and `/v1/models` report the context
each slot has. `--slot-full-context` (also `"slot-full-context"` in the config's `server` section) gives every slot
the whole `--max-context` instead, at N times the KV cache; the startup line says so. The slots' KV cache and one solo
session of the same length come out of the expert cache. Each slot keeps two conversation checkpoints, the end
of its system prompt and its last turn (each when at least `--prompt-cache-root` tokens lie before it), and a new
request reads only what follows the longest start it shares with any slot's checkpoint, decoding or idle: requests that
share a long system prompt read it once.

Every chat, messages and responses request must name the served model in `model`: the alias (or the model's name) that `/v1/models` lists, or the model file's name. An unknown or missing name is a 404 in the API's own error shape, and replies always carry the served name.

With `--max-context` above 32768 the KV cache is elastic (not with `--kv-resident`; a peer card is fine, and with
`--vram-elastic` a lend leaves the KV what it may still take): each session backs its first 32768 cells at startup
(the K/V pools, the indexer's block keys, the RoPE tables and the draft layer's K/V alike), and the rest of that VRAM
holds the expert cache's last slots until a request needs it. A request backs its prompt plus 16384 cells before the
prompt is read (and 16384 more whenever decode runs past that) by taking those slots back; their experts become CPU
misses (a long prompt's go to a peer card). The next request that needs less gives the cells back and the cache
refills the slots from RAM, so a short request decodes with the cache a 32k context would have. With `--parallel` a
slot whose share is above 4096 tokens backs 4096 cells at startup and grows by 4096. An idle conversation's KV stays
until requests beside it have run 15 s, then moves to RAM (outside `--conversation-cache-mib`, above the RAM floors;
else only to the disk tier holding it) when a request needs VRAM or the engine is idle; its next turn reads it back.
The startup line and each move say `elastic KV: ...` (a move out: `slot N's idle conversation ...`). The cache never
goes below the slots a prompt chunk needs: a request whose KV would take it there even after the other sessions and
idle conversations give back what they can is refused with that error, and a reply that grows past it ends with it (a
`--parallel` slot ends as at its length limit); the engine keeps running.

While other slots are decoding, a new prompt of up to 16384 tokens is read at once: the slots wait for it (a few
seconds at most), and it is read as in the single-stream path: the same chunks and streamed ring, the CPU pool's
share of its experts. Its first token so comes about as soon as on an idle engine. A longer prompt is read beside them:
the prompt path runs on its own thread, the slots' decode windows keep running (their kernels take the higher stream
priority), and the few steps that touch the draft layer or the output run between two windows, so a long prompt does
not stop the other requests. A much shorter request that arrives meanwhile goes first at the end of the long prompt's
current chunk; the rest of the long prompt is read after it, beside the slots, in the same chunks. The cache slots its
loan and its KV take hold, before they are taken, only experts the decoding slots route least: the ones they routed
lately trade places with less-routed experts of their layer, so the slots keep their experts in VRAM while the prompt
is read. A prompt that arrives while no other slot is decoding is read as in the single-stream path.

On one card, a batch window's layer with enough CPU misses sends a share of them over PCIe: CUDA0 copies them from
their RAM copies into VRAM staging and computes them while the CPU pool computes the rest, both reading RAM at once.
The startup line `batch windows: a layer with N+ CPU misses sends a share of them over PCIe to CUDA0` gives the
threshold. A request decoding alone runs the single-stream path and is not affected.

At startup the server prints one `[server] kv` line per device with all the session state placed there, for
example `12.5GiB cuda2 solo=262144 slots=2x262144 draft=0.5GiB`. With elastic KV (the default for a server whose
`--max-context` is above 32768, or with `--parallel` for slots above 4096 tokens) it reads `0.6/4.4GiB cuda0 elastic`:
the memory backed at startup, and what the cache can grow to at `--max-context`. Because that memory is taken from the
expert cache, the engine warns (`warning: the N batch slots' sessions ... take X GiB of CUDA0 ...`) when it leaves the
cache short of the expert set, or at under half of what it would hold without the slots, and names the remedies: a smaller `--max-context`, no `--slot-full-context`, fewer `--parallel` slots. If a slot's session
does not fit at all, the engine says which slot (`warning: --batch N: slot K's session does not fit`) and starts with
the slots that did fit; if fewer than two fit, `--batch is off` and the server runs one request at a time.

## The technical session: `--side-slot TOKENS`

`--side-slot TOKENS` (also `"side-slot"` in the config's `server` section, or the Loader's *Technical session* field; the default and
bare it are 30000, `0` is off) adds one slot to `--parallel`: a session of TOKENS tokens kept for the harness's short calls
(the triage of a request, next-prompt suggestions, the summary of older steps, the judges and the router, and the rewrite of the person's message
when Optimize prompt is on, which is available only while this session exists). A call that sends
`"cache_slot": "side"` (chat completions, messages and responses alike) runs there, with a prompt of its own and no big instruction
or tool prefix, so it neither waits behind a conversation's long answer nor overwrites a conversation's cache. The answer carries
`"cache_slot"`: `"side"`, or `"main"` when the ordinary slots served it. A request is served by them, and not refused, when the server has
no technical session or the prompt and answer do not fit its context; calls for the session wait for it in turn and never take a
conversation's slot, and no ordinary request ever takes the session's. `"cache_slot": "side_only"` is the same request refused (400, "technical session") instead of served by an ordinary slot when it does not fit; `"side_use": "name"` names what the call is for, and `/api/side-uses` counts the session's calls by that name.

The server turns `--parallel N --side-slot T` into the engine's `--batch N+1 --side-context T`: the conversations' slots keep
`--max-context / N`, the side slot holds `min(T, that)` and is the last slot, so when memory is short it is the slot dropped first
(the engine says so: `--side-context is off: the side slot did not fit`). With `--parallel 1` the batch is 2. The engine's batch
holds 8 slots, so `--parallel 8` becomes 7 conversations and the session (the server says so). `/slots`, `/props`, `/v1/models` and
`/metrics` report each slot's context and the session (`side_slot`).

What it costs. Memory: the KV cache and state of a session of TOKENS tokens, elastic like the other slots (it backs 4096 cells at
start and grows with use), and the draft layer's K/V of that size; the Loader's memory plan counts both. The session keeps no
conversation checkpoints and parks no conversation, so the RAM kept for them does not grow. The engine's batch windows need the K/V cache as resident INT8 without rotation: the server adds `--kv int8` by itself (and says so at start), and refuses `--parallel` or `--side-slot` together with another `--kv` format. What is given up: it turns the engine's
batch mode on, even with `--parallel 1`, so `end_turn_bias`, `logit_bias` and relaxed drafting are unavailable while it is on, and a
request alone no longer runs on the pure single-stream path. A call that arrives while a conversation decodes alone moves that
conversation into the batch windows until the call ends. It is on unless the setup cannot hold it: the server leaves it off, and says why
at start, when `--parallel` is 8, when the engine's own `--batch`, `--slots` or `--side-context` is set, or when `--kv` on the command
line is not `int8`; a `kv` other than `int8` in the config file gives way to `int8` (the server says so; `--side-slot 0` keeps the file's).
The Loader shows the same and counts the INT8 KV in its plan. The GUI and the harness ask `/v1/status` (`concurrency.side_slot`): with no
session the quick jobs run in the ordinary slots, Optimize prompt is greyed out and the session's size is shown, not editable, outside
the Loader. On a layer split over several GPUs a call's prompt is read in the main session, as every batch request's is, so the
isolation holds on one GPU.

- `server` holds the server's options (`python -m serve.server --help`), named as the flags without the dashes:
  strings or numbers, `true`/`false` for a switch (`lazy`, ...), a list for the repeatable ones
  (`allowed-hosts`, `allowed-origins`, `script`). `model` and `tokenizer` paths are relative to the file.
- `engine` holds engine flags (`bin/basalt --help`), the same way: a string or number, `true` for a switch, `false` to
  leave it off, a list for a flag with several values (`"control-vector-layer-range": [4, 44]`). Any flag the engine
  accepts works, on the command line as well (`--kv int8`, `--resident-experts`). A name the engine does not accept
  stops the server at startup, as does one in the wrong section.
- Precedence: command line > config file > built-in defaults.
- The API key is command line only (`--api-key`); a config file that sets it is refused.
- `"relaxed-drafting": 0.3` (or `true` for the default 0.5; `--relaxed-drafting [P]` on the command line) trades
  exactness for speed under sampling: a draft the model gives probability P or more is kept even when the model's own
  draw would have been another token. Lower P is faster and further from the model's true sampling distribution; the
  measured speed and cost per value are in [precision/features.md](precision/features.md#relaxed-drafting). Off by
  default: without it, speculative decoding returns exactly what plain sampling would. Greedy (temperature 0) requests,
  `logit_bias`, a request with a grammar (JSON `response_format`, tools), control tokens and everything inside a tool
  call stay exact. A request can set its own value with `"relaxed_drafting": P` (or `false` for exact). Single-stream
  decoding only; `--parallel` slots stay exact.
- The startup line `[server] engine ...` lists the settings that came from the file or the command line, then the
  grammar state; with none it says `defaults`, e.g. `grammar=off (from cli)`.

`--parallel N` (1 to 8) serves N requests at once in batch slots. Further requests wait in arrival order. `--max-context` is shared:
each slot holds `max-context / N` tokens (`--parallel 4 --max-context 262144` gives four slots of 65,536). A request longer than its slot
is refused with a 400 that names the length; the start log's `slots` line shows the context each slot gets. `--slot-full-context` gives every slot the whole `--max-context`, at N times the KV cache.

Each slot keeps two checkpoints (end of the system prompt, last turn), so requests that share a long system prompt read it once.

The engine tokenizes every prompt and matches it against what each slot holds. A reply the client sends back unchanged continues the
slot's own tokens even when the model wrote it in other pieces than the tokenizer cuts the same bytes into (not with images).

### Session affinity

With `--parallel`, a request that names its session goes to the slot that session's conversation sits in, so the next turn continues
the K/V already there. The session is the first of these headers that is set, then the request body's `session_id`: `x-llm-flow-id`,
`x-claude-code-session-id`, `x-session-id`, `x-session-affinity`, `x-grok-conv-id`, `x-grok-session-id`. A request with none picks a
slot by the longest shared prefix, then the least recently used. A session may hold several slots: a client may send one session id for a
session and its parallel subagents (Claude Code's `x-claude-code-session-id`, OpenCode's `X-Session-Id` and `x-session-affinity`, which
name the parent session), and each conversation keeps the slot its K/V is in. A request that continues none of the session's own slots
takes an unowned one first; with every slot owned, the session's own least recently used one comes before another session's.

### Prompts beside decoding slots

A prompt of up to 16,384 tokens is read at once, and the slots wait a few seconds. A longer one is read beside them, on its own thread,
while the slots keep decoding. A short request that arrives meanwhile goes first at the end of the current chunk.

## The console log

Every line starts with `[server]`. Values are `key=value`. Start-up prints a legend, a line per model, `ready`, `loaded`, `kv`, `slots`.
While a prompt is read, a slot prints `pp=<read>/<total>` with the rate. While requests generate, a `live` line appears every 2 s.
Each request ends with one `done` line:

```
[server] s1 done  pp=96/89c 0.1s  tg=1720 119.6t/s acc=65% dl=2.9  ttft=0.1s  end=stop  hit=89.7%  pcie=39.3%
```

- `pp=<prompt tokens>/<cached>c`: the rate counts newly read tokens only.
- `acc`: drafts accepted over drafts offered. `dl`: tokens per window, from the engine's own window count over the whole request (its solo and batch runs and every pass). With `--parallel` a window serves every busy slot in a fixed number of rows, so `dl` falls toward 1.0 as the slots fill. Both are absent when the request did not draft.
- `end`: `stop`, `length`, `cancelled`, `disconnect` or `error`.
- `hit`: expert cache hit rate. `pcie`: share of routed experts read over PCIe.

The engine's own `basalt ...` lines go to `~/.cache/basalt/<id>/engine.log`, one folder per model file (`<id>` is 16 hex digits
taken from the file); `[server] engine log: PATH` names it at start. A log of 10 MiB or more moves to `engine.log.1` at the
next start. An engine that dies of a fault or an unhandled exception writes a `basalt crash: error: ...` line (the fault, the stage
it was in) and the server's warning quotes it with the exit status. The folder also holds the conversation
cache. Nothing is unpacked from the model file: the server reads the tokenizer from its sections, and the engine and `basalt-vision`
read the vision projector inside it.

## The stats page

The server's address serves a page, updated every second from `/metrics`, `/slots` and `/api/logs`. The top of the page always shows:

- Decode and prefill: the live rate with a 60 s chart; when idle, the last request's decode and the last prompt read of at least 512
  new tokens (a cached follow-up has no meaningful read rate). The decode number's tooltip has the best decode and the conversation
  cache; the prefill number's has the last prompt, its prefix cache hit and the best prefill (the fastest read listed when the
  server has none yet).
- Experts: where the last request's expert lookups were served (VRAM, a peer card, CPU and RAM, the SSD), as a legend, and a map
  of 640 experts sampled evenly from every layer (`experts_map`): its colour is where the expert lives now, its brightness how often
  decode looked it up lately, an ember edge a lookup off the GPUs in the last second. A dense model shows its weights by bytes.
  Experts on the SSD turn the heading amber.
- With no model loaded, a slow field of basalt columns on its own (8 frames a second, paused while the tab is hidden, one still
  frame under reduced motion).

Under it, four tabs (keys `1` to `4`, `L` for the logs, or `#tab=req|slots|hw|logs` in the address):

- Requests: the queue, the recent requests, tokens per request, the last request, the best since the server started, the
  conversation cache (`conversation`: `parked`, `bytes`, `evictions`, `restored`, `restored_ms`) and the totals (`totals`).
- Slots: a card per slot with its current request, or for an idle slot its last one, its context and its drafting; past four
  slots, compact rows. With no model loaded the cards stay, empty.
- Hardware: each card's load, power, temperature, VRAM and PCIe traffic, and the CPU, RAM, disk reads, experts in VRAM, driver and
  uptime.
- Logs: the console lines, all or warnings only. The engine's long cache lines are shown short (`cache: restored 187k tokens in
  912 ms`); the log and `/api/logs` keep the raw line. The footer always shows the newest line and the request and token totals.

The engine's own figures, which the server asks for with `STATS` once the engine is ready:

- `/metrics` `experts_map`: `total` experts, and for each sampled one `tier` (0 VRAM, 1 peer card, 2 RAM, 3 file),
  `routed` (decode lookups a second, 0 once nothing runs) and `recent` (its lookups, fading by 0.85 a second).
- `/slots` per slot: `started` (epoch seconds), `path` (the endpoint) and `prompt_ms` (the read time) of the running request;
  for its current or last request `accept_pos` (drafts accepted over offered by draft position) and `accept_hist` (tokens per
  decode window, a sample a second over 60 s); for a model with routed experts `hit`: its windows' lookups split `vram`, `peer`
  (a bus copy or another card) and `cpu`, with `lookups_s`, `peer_s` and `ram_s` a second. A batch window several slots share
  counts its lookups for each of them; a model whose slots run through layer split stages counts none per slot.
- `/metrics` `engine`: `commit` (the build's), and `expert_slots`, the experts every card holds now (fewer while asleep).
- `/metrics` `hardware`: each card's `sm_clock` and `mem_clock` (MHz), `throttle` (what holds the clocks down, empty for nothing),
  `expert_slots` and `h2d_gbps` (the host-to-device link as the start measured it; Flash-Next measures it, the other models
  leave it out); `cpu_ccd` (the load of each group of cores that share an L3 cache), `ram_parts` (`experts` the engine holds in
  RAM, `page_cache`, `other`; Linux only) and `disk_hist` (the read rate's last 60 s).

The header word reads `Thinking`, `Answering` or `Tool call` (`live.phase`) while exactly one request runs. The conversation cache
figures come from the engine's `conversation cache:` lines and clear when the engine unloads, sleeps or restarts; a model that prints
no line when it parks (DeepSeek) shows them after its first restore, without evictions.

## Conversations on disk: `--conversation-cache-disk-mib`

Off at 0. With a budget, conversations of at least 8,192 tokens are written to `conversations` in the model's cache folder when they leave
RAM, a session, or at exit. Only the system prompt's and the last three turns' states are stored. Files are bound to the model and settings
they were made with, and checked by hash; a bad file is deleted. The folder is capped by the budget (least recently used first), and a
write always leaves 4 GiB of the volume free. After a restart, a prompt that starts with a stored conversation reads it back.

## Sleep: `--idle-sleep`

After the given seconds without requests, the engine frees its VRAM (the driver moves the GPU state to host memory). The process and its
RAM copy of the experts stay (a dense model has no cache to give back first: the driver moves all its GPU state, both cards' on two cards). The next request wakes it in seconds. Linux only. The server stays awake when the GPU state would not fit in
free RAM with 8 GiB to spare. `--idle-unload` ends the process instead.

## Lending VRAM: `POST /v1/vram`

With `--vram-elastic`, `POST /v1/vram` with `{"reserve_mib": N}` resizes the expert cache to keep N MiB free (`null`: the start's reserve).
A resize needs a gap between requests. Idle, it applies at once and answers `200` with the new figures. While a request runs it is
queued and answered `202 {"status": "pending"}`: it applies when the running requests have finished, and requests that arrive meanwhile
wait for it, so steady traffic cannot hold it off. A later POST replaces a queued one (`"replaced": true`). A malformed body is a
`400`. While the model sleeps, the reserve is kept and applied at the wake (`200 {"status": "asleep"}`), as when it is not loaded.
`GET /v1/status` shows `vram.pending` (the queued reserve, or `null`), `vram.applied` and `vram.error` (the engine refused
the queued one).

## Memory floors

Basalt-Plus refuses settings that would leave the machine without memory, and never shrinks what you asked for:

- `--vram-reserve-mib` below 256 is refused on Windows and WSL2 (128 on Linux): less free VRAM makes the driver page GPU memory.
- `--ram-reserve-mib` (default 2048) is the RAM left free for the rest of the system. The engine allocates its host buffers
  first, or holds their RAM when it page-locks them later; the expert tiers then take the free RAM less the reserve and less
  the driver's tracking of the pages they pin (`RAM floor ...` in the start log). Flash-Next's copy and top-up also leave 2 GiB beside the reserve
  for the host memory the engine takes after they read the free RAM (CUDA graphs, the first request), so the free RAM settles
  above the guard, not on it. When some experts stay in the file, the
  tiers also leave the OS file cache they are read through a floor: one prompt chunk's file reads and two layers' share of
  those experts, at least 4 GiB. Programs left idle beside a small reserve may be swapped out.
- While serving, the RAM guard holds the reserve: below it, it parks conversations to disk or drops them. A request waits up to 30 s
  for RAM to return. A running request stops with an error only when RAM is critical (under 1 GiB free, or below the reserve under
  memory pressure). `--ram-reserve-mib 0` turns the guard off.
- Experts on the SSD give one warning, the same for every model: `N experts on SSD (X GiB) · free X GiB RAM`. The console
  repeats it and the stats page adds an SSD tier and turns its Experts heading amber.
- The disk tier keeps its floor a GiB above the reserve.
- `--resident-budget-gib` that would leave less than the floor is refused with the largest safe value.

## Vision: `--vision`

Image input is one server option, `--vision off | dynamic | cpu | <GPU number>` (config: `"vision": 1` in the
`server` section). It is `off` by default. The encoder is Basalt-Plus's own and shares its kernels in every mode, so all
of them give bitwise identical embeddings.
`--vision off | dynamic | cpu | <GPU number>` (config: `"vision"` in `server`). The encoder is Basalt's own, and all modes give bitwise
identical embeddings. All of them read the projector in place from the model file's `vision/mmproj` section. The text can still
differ between modes: the sidecar's VRAM cost shrinks the expert cache, and which experts run on the GPU or the CPU shifts the
last bits of the logits, so a greedy answer can fork after some tokens (`dynamic` does the same with a different cache size).

| Mode | Encoder runs | VRAM cost | Fresh-image TTFT (IQ3_S, 400 W): 1k / 3k / 4k image tokens |
| --- | --- | --- | --- |
| `off` | no image input | none | |
| a GPU number | sidecar `bin/basalt-vision` on that card (the numbering `gpu` uses), always loaded | about 2.2 GiB of that card's expert cache | TTFT 0.77 s at 1k image tokens, 1.4 s at 3k, 1.8 s at 4k |
| `dynamic` | inside the engine, on the main GPU | none at rest (0.85 GiB of pinned RAM) | 0.77 s, 1.45 s, 1.8 s |
| `cpu` | sidecar on the CPU | none | about 6 s at 1k, 40 s at 3k, 70 s at 4k |

`dynamic` borrows the expert cache's last slots per image (any slot whose expert has a RAM copy), uploads the weights
from the pinned copy (about 30 ms), encodes on a side stream, and refills the same slots from RAM (about 70 ms), so the
cache is identical afterwards. Other `--parallel` slots pause for the encode. When a long conversation's elastic KV
holds that room, the sessions first give back the KV cells they do not hold (idle slots first) and take them back as
they grow. An image is refused only when the room cannot exist even then. An image already seen is served from the
encoder cache in tens of milliseconds in every mode. The sidecar's card is named, not derived from `gpu`: any card
works, whether or not it runs the engine, and the startup line names it.

```json
{ "server": { "gpu": "0,1", "vision": 1 } }
```

A GPU number uses the sidecar, which the main build also produces (`tools/vision` builds on its own too). The server
looks for it next to `bin/basalt` and only needs the named card to exist: the sm_120 rule is the engine's, not the
sidecar's. A sidecar is built for one card at configure time (`-DCMAKE_CUDA_ARCHITECTURES=<cc>`); on another it fails
at startup with CUDA's own error, which the server shows. Sidecars on non-Blackwell cards are community-tested.
`dynamic` needs no sidecar.

The image encoder is Basalt-Plus's own, written for this model's vision tower (no llama.cpp or ggml): bf16 tensor-core
products and flash attention with fp32 accumulation on the GPU, AVX-512/AVX2 on the CPU. It replaces llama.cpp's
mtmd encoder as a drop-in, is faster at every image size, and stays closer to an fp32 reference: mtmd's fp16 attention
drifts on large images. One image is capped at 64 megapixels; a header past it is refused before it is decoded.
| `off` | none | none | |
| GPU number | sidecar `bin/basalt-vision` on that card | about 2.2 GiB of its expert cache | 0.77 s / 1.4 s / 1.8 s |
| `dynamic` | inside the engine, on the main GPU | none at rest (0.85 GiB pinned RAM) | 0.77 s / 1.45 s / 1.8 s |
| `cpu` | sidecar on the CPU | none | about 6 s / 40 s / 70 s |

`dynamic` borrows the cache's last slots per image and refills them afterwards (about 70 ms), so the cache is identical after.
Hy4-flash reads images with `dynamic` only: its main-card expert slots go back for the request's pictures and fill again as after a
prompt. Which
images a request may name: [image sources](configuration/image-sources.md). An image may not sit in a system prompt.

## Video

With image input on, Qwen3.8-Flash-Next and Qwen3.8-27B read video (DeepSeek, Hy4-flash and Inkling 2 Mini do not). A user message names a video as it names an image, from
the same [sources](configuration/image-sources.md):

| API | Part |
| --- | --- |
| OpenAI chat | `{"type": "video_url", "video_url": {"url": "..."}}`; llama.cpp's `{"type": "input_video", "input_video": {"url": "..."}}` |
| Responses | `{"type": "input_video", "video_url": "..."}` |
| Anthropic | `{"type": "video", "source": {"type": "base64", "media_type": "video/mp4", "data": "..."}}`, or a `url` source |

Frames are sampled as the model's own processor samples them: 2 a second, 4 to 768, evenly spaced. A part may set `fps` (up to 60)
and `max_frames` (4 to 768). All frames are scaled to one size so the whole video fits `video-max-tokens` (default 8192 image
tokens), and each pair of frames is read as one picture with its time in the prompt. Audio and rotation metadata are not read: a
phone's portrait clip arrives sideways.

Decoding uses FFmpeg 6, 7, 8 or 9 from the machine ([platforms](platforms.md#ffmpeg-for-video)). At start the server prints
`video  on, FFmpeg 8 (...)`, or `video  off` with the reason, and then a video is refused with that reason. A video may be 1 GiB.

Qwen3.8-Flash-Next's frames decode on the encoder's GPU with NVDEC (H.264, HEVC, AV1, VP9 and the older codecs FFmpeg's NVDEC takes); a stream NVDEC
refuses decodes on the CPU, with the same pixels. Qwen3.8-27B's frames always decode on the CPU. With `dynamic` the expert cache lends NVDEC the memory for the decode when no
reply is running (tens of ms to lend and refill); beside a running reply the video decodes on the CPU, and the reply keeps going
until the frames are encoded. Another question about the same video, in the same chat or a new one, starts from the video
already read.

NVFP4 pack, `dynamic`, 400 W + 180 W, cold, time to the first token: a 30 s 1280x720 clip (8,202 prompt tokens) 2.7 s; a minute of
3840x2160 HEVC 5.2 s (decode 2.6 s, encode 0.3 s, prompt read 1.9 s); of 1920x1080 H.264 4.3 s. A new question on the same 30 s
clip: 0.4 s.
