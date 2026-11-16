> Reference: [README](../../README.md).

## Loading settings, one by one

Set in the Loader (each with its reason and the live prediction), saved in load profiles, written to the `server` /
`engine` sections of `tadamoo_config.json`. They take effect when the engine is (re)loaded.

| setting | what it does | how to choose |
|---|---|---|
| **Model** (`server.model`) | the `.basalt` file to serve | from the model library; a GGUF is converted first |
| **Port** (`server.port`) | the HTTP port, default 7232 | change it only to run two servers or when 7232 is taken |
| **Parallel requests** (`server.parallel`, 1-8) | requests served at once in the engine's batch slots | 1 for one user or one agent (fastest per request); more only for several agents at once. The context is shared: each slot gets `max-context / N` |
| **Full context per slot** (`server.slot-full-context`) | every slot gets the whole context instead of a share | costs N times the KV cache, taken from the expert cache: leave off unless each agent really needs the full length |
| **Image input** (`server.vision`) | `off`, `dynamic` (borrows expert-cache slots only while it reads an image: no standing VRAM), a GPU number such as `0` (encoder always on that card, about 2.2 GiB of its VRAM; the old names `on` and `peer` still read as GPU 0 and GPU 1), `cpu` (no VRAM, slow) | `dynamic` when the file has a projector, else `off` |
| **Context** (`engine.max-context`) | the longest conversation, in tokens (up to 262,144) | the longest that fits ("Valori sicuri" and the wizard pick it); a shorter one leaves more VRAM for experts (a little faster) and needs less commit |
| **KV cache** (`engine.kv`) | the precision of the attention cache: `fp16`, `int8`, `q4_0`, `k8v4` (INT8 keys, Q4_0 values) | `q4_0`: a quarter of fp16's memory, so long contexts fit; `int8` or `k8v4` for slightly more precise attention at more memory |
| **Prompt chunks** (`engine.prefill`) | how long prompts are read: `auto` (the largest chunk the expert cache can lend), `auto:8192/16384/32768` (a ceiling), or a fixed size | `auto` |
| **Verify window** (`engine.spec`, 0-8) | the longest window: the MTP drafter proposes up to N-1 tokens (fewer when it is unsure), the model checks them in one pass; 0 = off | 7 when the file has a drafter (the wizard measures 5, 7 and 8); the output is the same at any value, only the speed changes |
| **Expert cache** (`engine.expert-cache`) | expert slots kept in VRAM: `auto` (all the VRAM left after the reserve) or a number | `auto` |
| **Resident experts** (`engine.resident-experts`) | the experts not in VRAM are copied into RAM once at load, instead of being read from the file | on when they fit in RAM with the free share kept (they take about 50 GiB for the 3-bit file); off on PCs with less RAM, at a cost in speed |
| **VRAM to leave free** (`engine.vram-reserve-mib`) | VRAM the engine never touches (desktop, driver, other programs) | see [VRAM to leave free](windows-app.md#vram-to-leave-free); the wizard's rule: 0.5 GB + 1.5 GB if the GPU drives the main monitor (about 0.4 GB for a secondary one) |
| **PLE table** (`engine.ple-io`) | the per-layer n-gram table: `direct` (read from the SSD as needed, never in RAM), `ram` (locked in RAM at the start: RAM but no commit), `mmap` (a test arm) | `ram` when it fits with the free share kept, else `direct`; on a slow or rotational disk prefer `ram` |
| **CPU workers** (`engine.pool-workers`) | the threads that compute the experts not in VRAM | the performance cores plus half the efficiency cores, less one (one core stays for the server and the desktop) |
| **KV kept in VRAM** (`engine.kv-resident`, 0 or from 20,480 cells) | only this many cells of each layer's KV stay in VRAM, the whole K/V is pinned in RAM and the VRAM it frees goes to experts; a context that fits in these cells is not streamed | very long contexts on a small card; slower the more is streamed; not with `k8v4`. The plan moves the KV pool from the VRAM figure to the RAM figure |
| **Context extension** (`engine.rope-scaling`: `none`, `linear`, `yarn`) | extends the context past the trained length | fixed for the run and it changes the output; empty = the model file's |
| **Extension factor** (`engine.rope-scale`, 1 or more) | the factor for `linear` and `yarn` | empty = the model file's, else 1 |
| **RoPE frequency base** (`engine.rope-freq-base`, 0 or above 1) | the raw frequency base, 0 = the model's own | leave empty unless you know the value you want; fixed for the run |
| **CPU share of the prompt experts** (`engine.prefill-cpu-share`, 0-1) | the share of the streamed prompt experts the CPU computes; 0 has the GPU stream them all | 0.6 by default; lower on a weaker CPU |
| **Adaptive swaps** (`engine.adapt-swaps`) | the most experts one adaptive round swaps into the main GPU's expert cache; 0 = off | 96 by default; with swaps on, the output is not bit-identical from run to run |
| **RAM budget for experts** (`engine.resident-budget-gib`, GiB) | at most this many GiB of experts copied to RAM, the most used first | empty = automatic; the plan's RAM figure is limited to it; the engine refuses a budget that leaves less than its RAM floor free |
| **Experts in the file mapping** (`engine.mmap-experts`) | no resident RAM copy: the experts are read through a file mapping; it wins over resident experts and the RAM budget | usually slower (it depends on the OS page cache); off by default |
| **Elastic expert cache** (`engine.vram-elastic`) | the expert cache in segments, so `POST /v1/vram` can lend VRAM to other programs between requests | on; `false` (`--no-vram-elastic`) keeps one allocation |
| **Keep the GPU memory's commit** (`engine.keep-vram-commit`, Windows) | keeps the system commit WDDM charges for the GPU memory instead of giving it back | off (the commit is given back); switch it on if another program allocates GPU memory while the engine idles, since the engine can hang if that program pushes its VRAM out |
| **Expert profile file** (`engine.expert-profile-save`, advanced) | the file the engine saves what the adaptive cache learned to, on exit and periodically | empty = not saved; `tools/convert/convert.py --profile` reads it |
| **Profile save interval** (`engine.expert-profile-save-every`, minutes, advanced) | how often the profile is saved; 0 = on exit only | 10 by default |
| **Prompt checkpoints** (`engine.prompt-cache`) | conversation checkpoints kept between requests, about 118 MB of RAM each per slot; 0 reads every prompt from the start | 6 by default; the plan counts them |
| **Checkpoint every** (`engine.prompt-cache-every`, tokens) | also checkpoint every N freshly read prompt tokens; 0 = off | 16,384 by default |
| **First-turn checkpoint** (`engine.prompt-cache-root`, tokens) | checkpoint the end of the system prompt when it is at least this long; 0 = never | 2,048 by default |
| **Parked conversations** (`engine.conversation-cache-mib`, MiB) | RAM budget for parked conversations, so separate chats come back without re-reading; 0 = off | 0 by default; the plan counts the whole budget |
| **Parked conversations, at most** (`engine.conversation-cache-slots`) | how many conversations are parked at a time | 4 by default; 0 turns the cache off |
| **RAM kept free when parking** (`engine.conversation-cache-min-free-mib`, MiB) | the physical RAM floor kept free when a conversation is parked | 2,560 by default |
| **Grammar** (`server.grammar`) | holds every tool call and JSON response format to its schema inside the engine | on by default; applies at the next Load |

One more rule is per PC, not per profile: the **share of RAM and commit kept free** for other programs (15% unless the
wizard was told otherwise, 5-50%, at least 2 GB). Every plan, "Valori sicuri" and the wizard keep it free, and a load
that would need the page file to grow is refused.

## Chat (inference) settings, one by one

In the chat's settings panel; saved per client, and as named **inference presets** shared through the server (the
**Machine Default** preset is the wizard's). A request carries only the fields that are set, so everything left empty
uses the **model's own values** (from the source GGUF's `general.sampling.*`, shown as placeholders), then the
engine's. API clients send the same fields in their requests.

| setting | request field | what it does | guidance |
|---|---|---|---|
| **Thinking** (beside the message box; effort) | `reasoning_effort` | how much the model reasons before answering: High (default), Medium, Low, Off | High for hard problems, Low/Off for quick replies and helper calls (much faster) |
| **Reasoning budget** | `reasoning_budget_tokens` | after N thinking tokens the thinking is closed and the model answers; empty, 0 or -1 = no limit | 2,000-8,000 to bound slow answers; too low cuts the reasoning short |
| **Max output tokens** | `max_tokens` | the longest answer, thinking included | leave empty for the context's room; set it for short, predictable replies |
| **System prompt** | first `system` message | instructions kept at the head of every conversation | short and specific |
| **Stop strings** | `stop` | the answer ends before any of these strings | for formats that end with a marker |
| **Temperature** | `temperature` | randomness: 0 always takes the likeliest token | the model's value (1.0 for Qwen3.8-Flash-Next); 0.6-0.8 for steadier code |
| **Top-p** | `top_p` | samples only from the likeliest tokens that together reach this probability | the model's (0.95) |
| **Top-k** | `top_k` | samples only among the k likeliest tokens (1-64) | the model's (20) |
| **Min-p** | `min_p` | drops tokens less likely than this share of the top one | 0-0.05 |
| **Repetition penalty** | `repetition_penalty` | makes recent tokens less likely (1 = off) | 1-1.1; higher degrades code and lists |
| **Presence penalty** | `presence_penalty` | a flat penalty for any token already used (0-2) | 0; up to 1.5 if answers loop |
| **Frequency penalty** | `frequency_penalty` | a penalty that grows with each use (0-2) | 0 |
| **Penalty window** | `penalty_last_n` | how many recent tokens the penalties look at | 64 (the engine's) |
| **Seed** | `seed` | the same seed with the same settings repeats an answer | empty for varied answers |
| **Context policy** | (chat page) | what the chat does when the conversation outgrows the context: **Stop at the limit**, **Sliding window** (drops the oldest turns), **Compact** (the model summarizes the middle turns at the threshold; the system prompt, the first message and the newest two turns stay verbatim) | Compact for long sessions; Stop when every word must stay |
| **Compact at (% of context)** | (chat page) | the share of the context that starts compaction (30-100%, default 80%) | 80% |
| **Structured output** | `response_format` | Text, JSON object, or JSON schema (with a name and the schema) | for programs that parse the answer |
| **Tries after a rejected answer** | `structured_retries` | how many times an invalid JSON answer is generated again, with the error fed back (0-5, default 2) | 2 |

**Which preset a client opens with**: the one it used last, if it still exists; otherwise it follows the current
default (Machine Default, else the model's values). Settings changed by hand are kept, and the panel says which preset
is in use. **Model defaults** puts the model's own values back; **Restore Machine Default** the wizard's.

## Calibration

The Loader predicts the answer and prompt speeds of every plan. The prediction starts from a reference measurement and
scales it by the expert-cache hit rate (how many experts the plan keeps in VRAM), the verify window and the prompt
chunk. **Calibrate** in the Loader (also run automatically at the first load of a model) measures the running
configuration with three requests: an answer without reasoning (512 tokens), one with reasoning (1,024 tokens) and a
long prompt (about 8,000 tokens). The result goes to `calibration\<model>.json` in the user data folder, keyed by the
model and the settings. From then on the predictions of that model start from the calibration of exactly the same settings, else
from the model's newest one, instead of the built-in reference, and the Loader says which one it used. The wizard's measurement is the same calibration, repeated
for the verify windows it tries. Next to the speeds, the file keeps the RAM and commit the load
really took (see "Measured memory").

## Language

English by default; Italian on request. One choice serves the app, the web page, the scripts and the command-line
tools, in this order: `--lang it|en` (scripts and app, for that run) > the `TADAMOO_LANG` environment variable > the
choice saved in **Settings → Language** on the web page (or with `python -m serve.lang set it|en|auto`, kept in
`prefs.json` in the user data folder) > English. The language of Windows is not used. The Monitor and Hardware pages
and some technical server messages are in English only.

---
