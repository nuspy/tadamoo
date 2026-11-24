# Configuration reference

Every setting a user can pass to TaDaMoo: the server options, the engine flags, the per-request sampling fields and
the model file's sampling defaults. For what a setting does to speed, memory and output precision see the
[running](../running.md) and [precision](../precision/index.md).

- [How settings are given](#how-settings-are-given)
- [Server](#server)
- [Model and GPUs](#model-and-gpus)
- [Context and KV cache](#context-and-kv-cache)
- [Expert cache and memory](#expert-cache-and-memory)
- [Prompt processing](#prompt-processing)
- [Speculative decoding (MTP)](#speculative-decoding-mtp)
- [Parallel slots](#parallel-slots)
- [Conversation cache](#conversation-cache)
- [Vision](#vision)
- [Control vectors](#control-vectors)
- [LoRA adapters](lora.md)
- [Sampling and thinking](sampling.md)
- [The adaptive tier](adaptive.md)
- [Web search](web-search.md)
- [Voice models](../voice/models.md)
- [Example configurations](examples.md)
- [Diagnostics, measurement and `--exp` flags](diagnostics.md)
Every setting: server options, engine flags, and the sampling fields. Effects on speed and precision: [running](../running.md),
[precision](../precision/index.md).

- [How settings are given](#how-settings-are-given) · [Automatic settings](#automatic-settings) · [Server](#server) · [Model and GPUs](#model-and-gpus)
- [Context and KV](#context-and-kv-cache) · [Expert cache](#expert-cache-and-memory) · [Prompt](#prompt-processing) · [Drafting](#drafting)
- [Slots](#parallel-slots) · [Conversation cache](#conversation-cache) · [Vision](#vision) · [Control vectors](#control-vectors)
- Pages: [models and the router](models.md), [sampling](sampling.md), [adaptive tier](adaptive.md), [examples](examples.md), [diagnostics](diagnostics.md)

## How settings are given

```sh
bin/basalt serve --config tadamoo_config.json [options]
bin/basalt serve MODEL.basalt [--python PY] [options]
```

- **Server options** (the Python server): model, address, port, slots, vision, logging. `--name value` on the command line.
- **Engine flags** (`bin/basalt --help`): context, KV, expert cache, prefill, speculation. Given like any flag; the server passes them
  on in order and asks the engine which it accepts.

A config file is one JSON object. `models` lists the models; `engine` applies to every model that takes it; `server` is shared:

```json
{ "version": 2, "models": [{ "model": "model.basalt", "kv": "int8" }],
  "engine": { "resident-experts": true }, "server": { "port": 8080 } }
```

- Keys are the flag names without the dashes (`_` works for `-`). Values are strings or numbers; a switch is `true`, and
  `false` leaves it off. A repeatable option (`script`) takes a list.
- `model` and `tokenizer` paths are relative to the config file.
- **Precedence:** command line > config file > built-in defaults. The engine receives the config file's flags, then
  the command line's, and a flag given twice takes its last value.
- An engine flag in `server`, or a server option in `engine`, stops the server at startup, as does an unknown name.
- `api-key` cannot be set in a config file; a file that sets it is refused.
- The built-in defaults pick the fastest settings that need no knowledge of the machine (speculation 8 with a drafter,
  expert cache auto, prefill auto, resident experts when the RAM holds them, adaptive rounds every window on a server), and keep
  the settings that change output or memory use (4096 context, fp16 KV). A config file sets those. The startup line `[server] engine ...` lists what came from the file or the command line.

In the tables, "default" is what applies when the setting is absent everywhere.
- Where a setting goes in a config file: `server` is the `server` block, `entry` a model entry or the top-level `engine`,
  `engine` (an engine flag) the same places, `model entry` a model entry only.
- Keys are flag names without dashes. Switches are `true`. A repeatable option (`allowed-host`, `cors-origin`, `image-url-host`,
  `image-dir`) takes a list. `model` paths are relative to the file.
- A row that names its models (Flash-Next only, DeepSeek only, ...) stops the start in a model entry or on the command line of another model; in the top-level `engine` it is left out for that model, with a start line ([checks at start](models.md#checks-at-start)).
- Precedence: command line, then config file, then defaults; `--model` and `--alias` do not go with `--config` (its entries name the models).
  A wrong block or an unknown name stops the start. `api-key` cannot be in a file.
- The defaults are the fastest settings that need no knowledge of the machine. The keys that change output or memory (8,192 context,
  fp16 KV) are kept for you to set.

## Automatic settings

When left out, the engine picks `peer-exclusive`, `resident` (`resident-budget-gib`), `pool-affinity` and `pool-workers`. A given value
always wins. It prints one `[server] auto` line, where `ram-experts` is the `resident` amount (the experts held in RAM, not the whole model):

| Setting | Rule |
| --- | --- |
| `peer-exclusive` | Flash-Next only. On only with a peer card, when the full expert set does not fit in free RAM but the set without the peer's share does |
| `resident` | Flash-Next only. Free RAM above the safety floor, capped at the experts the GPUs do not hold |
| `pool-affinity` | `auto` |
| `pool-workers` | physical cores minus the host thread; on hybrid CPUs, P-cores minus one plus half the E-cores |

## Server

Server options. Command line `--name`, config `server` block `"name"`.

| Setting | Type, values | Default | What it does and when to change it |
| --- | --- | --- | --- |
| `model` | path | none (required) | The `.basalt` model file. The weights, tokenizer, chat template, drafter, vision projector and default sampling all come from it. |
| `alias` | string | the name stored in the model file | The model name the API reports. Change it when a client expects a particular name. |
| `host` | address | `127.0.0.1` | The address to listen on. `127.0.0.1` is this machine only; `0.0.0.0` also serves the network. Never listen beyond `127.0.0.1` without `api-key`. |
| `port` | integer | `7232` | The TCP port. |
| `api-key` | string | none | Require this key on `/v1/*` (`Authorization: Bearer ...` or `x-api-key`). Command line only. Set it whenever `host` is not `127.0.0.1`. |
| `fit-max-tokens` | switch | on | Clamp a request's `max_tokens` to the context that remains instead of rejecting it with 400; `--no-fit-max-tokens` rejects it. |
| `open` | switch | off | Open the local web page in the browser once the model is ready. |
| `lazy` | switch | off | Start the text-only API unloaded and load the model on the first request. Turns vision off. |
| `idle-unload` | seconds | off | Unload the model after this long without requests; the next request loads it again. Frees VRAM for other programs. |
| `idle-sleep` | seconds | off | Put the model to sleep after this long without requests: its VRAM is freed, the process keeps its RAM copy, conversations and learned expert placement, and the next request wakes it in seconds. Linux only (the driver suspends the engine's GPU state); it stays awake when that state would not fit in the free RAM. [running](../running.md#sleep---idle-sleep) |
| `min-free-vram-mib` | integer | off | Load an unloaded model only when this much VRAM is free, otherwise answer 503. Pairs with `lazy` and `idle-unload`. |
| `grammar` | switch (`--no-grammar` or `grammar: false` turns it off) | on | Hold every tool call and JSON response format (`json_object`, `json_schema`) to its schema inside the engine, so a malformed call or object cannot be produced (as in llama.cpp's Qwen3-Coder parser, a call's required parameters may come in any order, each once, with up to six required ones permuted and more kept in declared order, followed by the optional ones in any order, which may repeat); the model's JSON is returned as it is generated, with no check after the fact. A JSON format sent together with tools (`tool_choice` auto) holds the answer to tool calls or the JSON value; a forced call holds it to the call. A request with `strict: true` gets the grammar even when this is off; with it off, any other JSON `response_format` (or Responses `text.format`, or Anthropic `output_config.format`) is refused with HTTP 400. Needs an engine that has `--grammar`; the server turns the engine's grammar support on whenever it has the flag. Works in batch slots: a slot's first pick the grammar refuses is resampled under the mask (the solo rule) and its window ends there. |
| `debug` | switch | off | One log line per request and the raw model text after each reply. |
| `request-lines` | switch | off | Echo the engine's per-request summary on stdout, for a supervisor that sees only that output. |
| `temperature`, `top-p`, `top-k`, `min-p`, `presence-penalty`, `frequency-penalty`, `repetition-penalty`, `penalty-last-n`, `seed` | number (`top-k`, `penalty-last-n`, `seed`: integer) | the model file's `sampling` value, else the engine's | The sampling default for a request that leaves that field out, over the model file's. Same meaning and allowed values as the [model file's sampling keys](sampling.md#the-model-files-sampling-defaults). The server applies them to each request; the engine never sees them. |
| `thinking-loop-tokens` | integer | `0` (never) | End the thinking, and let the model answer, when it repeats one token this many times in a row inside the thinking. See [Thinking loops](sampling.md#thinking-loops). |
| `answer-room` | switch (`--answer-room` or `answer-room: true` turns it on) | off | Reserve space for closing the thinking and for the answer when the effective output cap is at least 1024 tokens; this cuts a long thinking short. A request budget of `0` or `-1` opts out. See [sampling](sampling.md). |
| `passage-loop-tokens`, `passage-loop-times`, `passage-loop-window` | integer | `12`, `4`, `2000` | An answer that writes the same passage of this many tokens this many times within this many tokens ends as `length`; `passage-loop-tokens` `0` turns it off. Code blocks and groups of three or fewer different tokens never count. |
| `measure-length` | switch (`--no-measure-length` turns it off) | on | At a model's first load, measure how long a reply it writes by itself and keep it with the model; see [length](length.md). |
| `tokenizer` | path | `pack/full/tokenizer` in the source tree (a byte tokenizer if absent) | Tokenizer directory. Mock engine only. |
| `mock-engine`, `script` | switch, string (repeatable) | off, a short greeting | Testing: run a scripted engine instead of the model, for clients and tests (no `model` needed); `script` is its answer, and given more than once, requests get them in turn. |
| `config` | path | none | The config file. Command line only. |
Command line `--name`; the Where column says which config block takes it.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `model` | model entry | path | required | The `.basalt` file: weights, tokenizer, chat template, drafter, vision projector and default sampling. |
| `alias` | model entry | string | name in the file | The model name the API reports. |
| `host` | server | address | `127.0.0.1` | Listen address. `0.0.0.0` serves the network; never without `api-key`. |
| `port` | server | integer | `8080` | TCP port. |
| `api-key` | command line | string | none | Require this key on `/v1/*` (`Authorization: Bearer` or `x-api-key`). Command line only. |
| `api-key-file` | command line | path | none | Read that key from the first line of this file, which keeps it out of the process list. Not with `api-key`. Command line only. |
| `allowed-host` | server | string, repeatable | none | Also answer to this `Host` name; `"*"` turns the check off. |
| `cors-origin` | server | string, repeatable | none | Let web pages from this origin call `/v1/*`. |
| `fit-max-tokens` | entry | switch | off | Clamp `max_tokens` to the remaining context instead of a 400. |
| `open` | server | switch | off | Open the web page when ready. |
| `lazy` | server | switch | off | Start unloaded; load on the first request. Drops a sidecar encoder. |
| `idle-unload` | entry | seconds | off | Unload after this long idle. Frees VRAM for other programs. |
| `idle-sleep` | entry | seconds | off | Sleep after this long idle: VRAM freed, RAM kept, wake in seconds. Linux only. [running](../running.md#sleep---idle-sleep) |
| `min-free-vram-mib` | entry | integer | off | Load an unloaded model only when this much VRAM is free; else 503. |
| `grammar` | entry | switch (`--no-grammar`) | on | Hold tool calls and JSON formats (`json_object`, `json_schema`) to their schema inside the engine. Without it, other JSON formats are refused with 400. |
| `max-payload-mib` | server | MiB | `256` | Largest request body the server reads; a bigger one gets a 413. |
| `debug` | server | switch | off | One log line per request, and the raw model text. |
| `request-lines` | server | switch | off | Echo the per-request summary on stdout. |
| `temperature`, `top-p`, `top-k`, `min-p`, `presence-penalty`, `frequency-penalty`, `repetition-penalty`, `penalty-last-n`, `seed` | entry | number (`top-k`, `penalty-last-n`, `seed`: integer) | the model file's `sampling`, else the engine's | Default for a request that leaves the field out. See [sampling](sampling.md). |
| `reasoning-effort` | entry | `none`, `low`, `medium`, `high`, `xhigh` or `max` | unset (the template's default) | Reasoning effort for requests that name no effort, thinking switch or budget. See [sampling](sampling.md). |
| `reasoning-budget-tokens` | entry | integer | `0` | Thinking budget for requests that name none. A request's own value wins. |
| `thinking-loop-tokens` | entry | integer | `256` | End thinking when one token repeats this many times in a row; `0` turns it off. |
| `answer-room` | entry | `auto` or integer | `auto` | Output tokens kept for closing the thinking and the answer: `auto`, or a fixed number (`0`: none). See [sampling](sampling.md#thinking). |
| `config` | command line | path | none | The config file. Command line only. |

## Model and GPUs

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `gpu` | entry, `basalt`, `basalt-bench`, `basalt-probe` | `"0"`, `"0,1"`, `"auto"` | GPU 0 alone | Cards, numbered as `nvidia-smi`. The first is the main card. `auto` takes every Blackwell card and makes the fastest main. [GPUs](gpus.md) |
| `peer-reserve-mib` | engine | MiB | 600 | VRAM the second card keeps free after its experts and prompt buffers. Raise it when the start says the peer's buffers do not fit. [GPUs](gpus.md#two-16-gb-cards) |

## Context and KV cache

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `max-context` | engine | integer tokens | 4096 | The longest context the engine allows. Raise it for long prompts; the KV cache grows with it. With `parallel`, this is shared by the slots. |
| `kv` | engine | `fp16`, `int8`, `q4_0`, `k8v4` | `fp16` | KV storage. `int8` halves the VRAM. `q4_0` is 4-bit after a Hadamard rotation, half of `int8` again, slightly lower precision. `k8v4` keeps K in int8 and V in rotated 4-bit (816 B per cell against `int8`'s 1056); not with `kv-resident`. Use `int8` for long contexts; go lower when VRAM is short. See [precision](../precision/index.md). |
| `kv-resident` | engine | integer cells, minimum 20480, or `0` | 0 (all KV in VRAM) | Keep only this many cells of each attention layer's KV in VRAM and the whole K/V in pinned RAM; the freed VRAM goes to expert slots. A context that fits in N cells is not streamed. For very long contexts on a small card. |
| `rope-scaling` | engine | `none`, `linear`, `yarn` | the model file's, else `none` | Extend the context past the trained length. Fixed for the run: K in the cache is stored after RoPE. |
| `rope-scale` | engine | number | the model file's, else 1 | The extension factor for `linear` and `yarn`. |
| `rope-freq-base`, `rope-freq-scale` | engine | number | 0 (the model's own) | Raw frequency base and angle shrink. Leave at 0 unless you know the values you want. |
| `yarn-orig-ctx`, `yarn-ext-factor`, `yarn-attn-factor`, `yarn-beta-fast`, `yarn-beta-slow` | engine | number | 0 (262144), auto, 1, 32, 1 | YaRN's own parameters, as in llama.cpp. |

A server with `max-context` above 32768 has an elastic KV cache: each session backs its first 32768 cells at startup and
takes more from the expert cache only when a request needs it. With `parallel`, each slot whose share is above 4096
tokens does the same from 4096 cells, growing by 4096, and an idle conversation's KV moves to RAM once requests beside
it have run 15 s. It is off with `kv-resident`. See [running](../running.md#concurrent-requests---parallel-n).
| `max-context` | engine | tokens | 8192 | Longest context. Shared by `parallel` slots. The KV cache grows with it. |
| `kv` | engine | Flash-Next: `fp16`, `int8`, `q4_0`, `k8v4`; DeepSeek: `fp8`, `fp16`; Qwen3.8-27B: `fp16`, `int8`; Inkling 2 Mini: `fp16`, `fp8` | Flash-Next `fp16`, DeepSeek `fp8`, Qwen3.8-27B `fp16`, Inkling 2 Mini `fp16` | KV storage. Qwen3.8-27B: `int8` takes about half of `fp16`'s memory per position ([guide](../models/qwen38-27b.md#memory)). Flash-Next: `int8` halves VRAM; `q4_0` is 4-bit after rotation; `k8v4` keeps K int8 and V 4-bit (816 bytes per cell against `int8`'s 1,056); not with `kv-resident`. DeepSeek: `fp8` rounds as the model's reference (FP8 window rows, FP4 compressed rows and index keys, 920 bytes per position); `fp16` keeps the unrounded values (3,200 bytes per position). Inkling 2 Mini: `fp8` halves the full-attention caches (E4M3, unit scale; [guide](../models/inkling2.md#memory)). A model refuses the other's values. |
| `kv-resident` | engine | cells (Flash-Next: min 20,480) or `0` | 0 | Flash-Next and Qwen3.8-27B. Keep N cells of KV in VRAM and the rest in pinned RAM. Flash-Next: with `parallel` and `kv` `int8`, every slot streams its own K/V this way. Qwen3.8-27B: each attention layer keeps its first N positions on the card and every decoded token reads the rest over the bus, so decode slows with each one ([guide](../models/qwen38-27b.md#memory)); output is unchanged. |
| `rope-scaling` | engine | `none`, `linear`, `yarn` | model's, else `none` | Flash-Next and Qwen3.8-27B. Extend past the trained length (Qwen3.8-27B: `--max-context` up to 262,144 x `rope-scale`). Fixed for the run, and it changes every position's rotation, short prompts too ([precision](../precision/qwen38-27b.md#context-past-262144)). |
| `rope-scale` | engine | number | model's, else 1 | Flash-Next and Qwen3.8-27B. Factor for `linear` and `yarn`. |
| `rope-freq-base`, `rope-freq-scale` | engine | number | 0 (model's own) | Flash-Next and Qwen3.8-27B. Raw RoPE values. Leave at 0 unless you know them. |
| `yarn-orig-ctx`, `yarn-ext-factor`, `yarn-attn-factor`, `yarn-beta-fast`, `yarn-beta-slow` | engine | number | 0 (the model's 262144), auto, 1, 32, 1 | Flash-Next and Qwen3.8-27B. YaRN parameters, as in llama.cpp. |

## Expert cache and memory

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `vram-reserve-mib` | engine | integer MiB, at least 256 (128 on Linux) | 700 (lowered automatically, down to 300, on a card that would otherwise get too few slots; a value you give is kept unless it is raised as described here) | VRAM left free on the main card for graphs, scratch and the head when `expert-cache auto` sizes the cache. A server's kernels and window graphs that load after the cache (96 MiB, plus 24 MiB per slot with `--parallel`) load into it; a reserve too small to hold them and keep the floor free is raised to that: on Linux with `--parallel 4`, any value below 320 MiB (128 + 96 + 4 x 24) becomes 320, and the `expert cache auto` start line gives both (`320 MiB reserved (150 asked, ...)`). Below the floor is refused. Raise it if the machine pages GPU memory or other programs need VRAM. |
| `resident-experts` | engine | switch | on when the engine has an expert profile for the model | Keep the experts the GPU does not hold in page-locked RAM, so none is read from the file again. Leaves a RAM floor free for the OS; when the RAM cannot hold them it falls back to a plain file mapping. `mmap-experts` turns it off. |
| `resident-budget-gib` | engine | number GiB | auto when not set | Like `resident-experts` but with at most N GiB of experts in RAM, hottest first. Refused when it would leave less than the RAM floor free; the error names the largest safe value. |
| `mmap-experts` | engine | switch | off | Opt out of the default resident RAM copy and back to a file mapping. Speed then depends on the OS page cache. |
| `shared-expert-arena` | engine | file path | off | Back the resident arena with one shared file so several processes use one copy. Put it on `/dev/shm`. |
| `expert-profile-save` | engine | file path | off | Save what the adaptive tier learned (experts in VRAM and routing counts) to this file on exit and periodically. A start that finds the file places it instead of the engine's profile, so the first request runs on what the last run learned: faster when the workload continues, slower when it changes ([adaptive tier](adaptive.md)). Needs the adaptive tier on. `tools/convert/convert.py --profile` reads it too. |
| `expert-profile-save-every` | engine | minutes | 10 (`0` = on exit only) | How often the profile is saved. |
| `resident-cpu-experts` | engine | switch | off (implied by `resident-experts`) | With `mmap-experts` and an expert profile: keep the experts the GPU does not hold in ordinary RAM, so none is read from the file again. `resident-experts` is this with the copy page-locked when possible and the file mapping as a fallback when it does not fit. |
| `vram-elastic` | engine | switch | on (`false` gives `--no-vram-elastic`) | Hold the expert cache in segments so `POST /v1/vram` can lend VRAM to other programs between requests. With the elastic KV it lends only what leaves the KV room to reach `max-context` (a larger lend is granted down to that; the reply's `reserve_max_mib` and `capped` say so), and `null` returns the lend ([design](../engine-vram-arbiter.md)). Works with `resident-experts`: a shrink first copies the experts it gives back into RAM (and those of the slots the KV may take during the lend), so none is read from the file, and is refused (nothing changed) when the RAM above the floor cannot hold them; a grow frees those copies. |
| `vram-segment-mib` | engine | integer MiB, at least 64 | 512 | The segment size of `vram-elastic`'s expert cache: the step VRAM is lent and taken back in. Below 64 turns `vram-elastic` off with a warning. |
| `expert-cache` | engine | `auto`, or an integer count of expert blobs | `auto` (with an expert profile) | Keep this many expert blobs in VRAM and compute their rows on the GPU. `auto` takes as many as fit in the VRAM left after the weights, the session and `vram-reserve-mib`. |
| `adapt-every` | engine | integer windows | 1 on a server, 4 with a layer split (`0` = off) | An adaptive round every N decode windows. [Adaptive tier](adaptive.md). |
| `adapt-swaps` | engine | integer | 96 (`0` = off) | Most experts one adaptive round swaps into the main GPU; a server round after a large backlog swaps four times as many, and the RAM copy keeps four times N free places for them. [Adaptive tier](adaptive.md). |
| `peer-adapt-swaps` | engine | integer | the value of `adapt-swaps` | Most experts one round swaps into the peer card. Only with a peer card. |
| `adapt-decay` | engine | number | 0.85 on a server, 0.7 with a layer split | The factor the routing counts are multiplied by after each round; lower forgets sooner. [Adaptive tier](adaptive.md). |
| `keep-vram-commit` | engine | switch | off | Windows: keep the system commit WDDM charges for the GPU memory. Off, the engine gives that commit back once the model is loaded (see [WINDOWS.md](WINDOWS.md#the-commit-of-the-gpu-memory)); if another program then pushes the engine's VRAM out, the engine can hang. |

### The adaptive tier

The engine moves the experts the conversation routes to most into VRAM, in rounds beside decode. What the four
settings above do, what they trade and the measured numbers: [adaptive tier](adaptive.md).

The startup lines `RAM floor ...` and `RAM ... free, guard ...` give the figures behind the memory floors. See the
[running](../running.md#memory-floors).
| `vram-reserve-mib` | engine | MiB, at least 256 (128 on Linux) | 700 (lowered to 300 on a card with too few slots) | VRAM kept free on the main card for graphs, scratch and the head. Raised to the floor the kernels need (`--parallel 4` on Linux: at least 320). |
| `resident-experts` | engine | switch | on with a profile | Flash-Next only. Keep experts the GPU does not hold in page-locked RAM. Falls back to a file mapping when RAM is short. |
| `resident-budget-gib` | engine | GiB | auto | At most N GiB of experts in RAM, hottest first. |
| `mmap-experts` | engine | switch | off | Flash-Next only. Use a file mapping instead of the RAM copy. The first rounds after a restart run slower while pages fault in, so drop them from benchmarks. |
| `shared-expert-arena` | engine | file path | off | Flash-Next only. One shared file for the resident arena across processes (put it on `/dev/shm`). |
| `expert-profile` | engine | file | `data/profiles/<arch>-<layers>x<experts>.prof` | Which experts start in VRAM. |
| `expert-profile-save` | engine | file | off | Flash-Next only. Save the learned placement on exit and periodically; a start places from it. Needs the adaptive tier. |
| `expert-profile-save-every` | engine | minutes | 10 (`0` = on exit only) | Flash-Next only. Save period. |
| `resident-cpu-experts` | engine | switch | off (implied by `resident-experts`) | Flash-Next only. With `mmap-experts`: keep RAM-resident experts in ordinary RAM. |
| `vram-elastic` | engine | switch | off | Flash-Next only. Hold the expert cache in segments so `POST /v1/vram` can lend VRAM between requests ([queued while a request runs](../running.md#lending-vram-post-v1vram)). |
| `vram-segment-mib` | engine | MiB, at least 64 | 512 | Flash-Next only. Segment size for `vram-elastic`. |
| `expert-cache` | engine | `auto` or a count | `auto` | Expert blobs kept in VRAM. `auto` takes what fits after weights, session and reserve. A count is honoured exactly or the start is refused with a one-line message (what fits, the free VRAM, the reserve); it is never trimmed. |
| `ram-reserve-mib` | engine | MiB | 2048 | RAM left free for the rest of the system. The expert tiers take what is above it after the engine's own host buffers, and the serving RAM guard holds it. `0` keeps nothing and turns the guard off. [memory floors](../running.md#memory-floors) |
| `adapt-every` | engine | windows | 1 on a server, 4 with a layer split (`0` = off) | Flash-Next only. An adaptive round every N windows. [adaptive](adaptive.md) |
| `adapt-swaps` | engine | integer | 96 (`0` = off) | Flash-Next only. Most experts one round swaps into the main GPU. [adaptive](adaptive.md) |
| `peer-adapt-swaps` | engine | integer | `adapt-swaps` | Flash-Next only. Most experts one round swaps into the peer card. |
| `adapt-decay` | engine | number | 0.85 (0.7 with a layer split) | Flash-Next only. Factor applied to routing counts after each round. [adaptive](adaptive.md) |

## Prompt processing

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `prefill` | engine | `auto`, `auto:N` (N = 8192, 16384 or 32768), or tokens | `auto` | Flash-Next only. Prompt chunk size. |
| `prefill-cpu-share` | engine | 0 to 1 | 0.6 | Flash-Next only. Share of streamed prompt experts the CPU computes. Fixed, so output is the same run to run. |
| `short-read` | engine | tokens | 64 (`0` = off) | Flash-Next only. A fresh part of at most N tokens is read through decode windows. |
| `no-prompt-skip` | engine | switch | off | DeepSeek only. Run every row through every layer. Slower; the same math. [precision](../precision/deepseek41.md#prompt-skip) |
| `no-peer-prefill` | engine | switch | off | Flash-Next only. The peer card computes none of the prompt rows; the prompt stays on the main card. |

## Drafting

Unless `relaxed-drafting` is on, output is exactly plain decoding's.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `spec` | engine | integer | 8 (MTP), 6 (DSpark), 16 (Qwen3.8-27B), 8 (Inkling 2 Mini's DSpark), else 0 | Verify windows of at most N tokens (Qwen3.8-27B: up to 16). `0` or `1` turns it off. |
| `draft-experts` | engine | 32, 64, 96, 128, or 0 | 32 | DeepSeek only. Experts each drafter stage keeps (`0` = all 128). |
| `suffix-draft` | engine | tokens | 3 (`0` = the drafter only) | Flash-Next and Qwen3.8-27B. Prompt lookup: fill a window from an earlier repeat of at least N tokens. Its index takes host RAM by `max-context`: 64 MiB per slot at 262,144 tokens. |
| `mtp-max-t` | engine | integer | 0 (= `spec`) | Flash-Next only. Caps the MTP's own windows. |
| `mtp-window` | engine | cells | 32768 (`0` = all) | Flash-Next only. The drafter attends to the last N cells. |
| `relaxed-drafting` | engine (`[P]`) | `true` or 0 to 1 | off | Flash-Next only. **Changes output.** Under sampling, keeps a draft the model gives probability P or more. Greedy, `logit_bias`, JSON and tool calls stay exact. [precision](../precision/qwen38-flash-next.md#relaxed-drafting) |
| `route-tail-skip` | engine (`[R]`) | `true` or 1 to 9 | off | Flash-Next only. **Changes output.** Skips experts no GPU holds when they rank R or lower in every window entry. Needs the device MoE tail. [precision](../precision/qwen38-flash-next.md#lossy-routing-flags) |
| `route-prior` | engine (`[LAMBDA]`) | `true` or 0 to 16 | off | Flash-Next only. **Changes output.** Adds LAMBDA to held experts' router logits. Needs the device MoE tail. [precision](../precision/qwen38-flash-next.md#lossy-routing-flags) |

## Parallel slots

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `parallel` | server | integer 1..8 | 1 | Serve N requests at once in batch slots; further requests wait their turn. `max-context` is shared: each slot holds `max-context / N` tokens. The slots' KV cache comes out of the expert cache. Sets the engine's `batch`. |
| `slot-full-context` | server | switch | off | With `parallel`, give every slot the whole `max-context` instead of an equal share. Costs N times the KV cache. |
| `side-slot` | server | `0` or tokens from 4096 (`true`: 30000) | 30000 | The technical session (`0`: off): one more slot of that many tokens for the harness's short calls (a request with `cache_slot: "side"`), so they never wait behind a conversation or overwrite its cache. Sets the engine's `batch` to `parallel + 1` (at most 8 in all) and `side-context` to this value. Costs the KV cache and state of that many tokens; while it is on `end_turn_bias`, `logit_bias` and relaxed drafting are unavailable. Left off, with the reason in the log, when the setup cannot hold it. See [running](../running.md#the-technical-session---side-slot-tokens). |
| `control-secret` | server | switch | off | Ask for a secret on the changes made through `/api/*` and `/api/control/*`; the page receives it as a cookie, programs send it in `X-Tadamoo-Control`. The Loader sets it (Server group) and saves it in this file. The names browsers may use besides IP addresses, `localhost` and the PC's own are `allowed-hosts` (a list): see [server security](../server-security.md). |
| `control-secret-value` | server | text, letters, digits and signs, up to 200 | empty | With `control-secret`, the text used as the secret instead of a random one made at each start. Kept in this file as written; the Loader never shows it again. |
| `host` / `port` | server | address / 1..65535 | `127.0.0.1` / `7232` | Where the server listens. Settings > Network edits them; a change applies when the server is started again. |
| `allowed-hosts` | server | list of host names | empty | Names (besides IP addresses, `localhost` and this PC's own name) that may reach the server: the Host rule ([server-security.md](../server-security.md)). |
| `public-name` | server | host name | empty | The name the server is reached by from outside (a domain behind a reverse proxy): allowed as a Host name like an entry of `allowed-hosts`. |
| `no-web` | server | switch | off | Serve only the API: no web page, no `/web/` files, no fonts; `/` answers `{"web": false}`. For a server that the desktop app, the phone app or other programs use. |
| `batch` / `slots` | engine | integer 2..8 | off | The engine flag `parallel` sets. Use `parallel`; setting both is an error. |
| `side-context` | engine | integer tokens | 0 (off) | The engine flag `side-slot` sets: the last `batch` slot holds this many tokens (at most `max-context`) and keeps no conversation checkpoints. Use `side-slot`; setting both is an error. |
| `parallel` | entry | 1..8 | 1 | Serve N requests at once. Sets the engine's `batch`. Flash-Next needs `kv` `int8` for N above 1 (another format stops the start, naming it). With `kv-resident`, each slot keeps only those cells in VRAM. |
| `slot-full-context` | entry | switch | off | Give every slot the whole `max-context`, at N times the KV. |

## Conversation cache

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `prompt-cache` | engine | integer | 6 | Conversation checkpoints kept, about 118 MB of RAM each on Flash-Next (page-locked, held from start with one more per slot). `0` reads every prompt from the start. |
| `prompt-cache-every` | engine | tokens | 16384 (`0` = off) | Flash-Next only. Also checkpoint every N freshly read tokens (RAM only). |
| `prompt-cache-root` | engine | tokens | 2048 (`0` = never) | Checkpoint the end of the system prompt when it is at least this long. |
| `turn-token` | engine | token id | 248045 | The token that opens a chat turn; checkpoints go at the last one. |
| `tail-role-token` | engine | token id | -1 (off) | A short trailing turn that opens with this token keeps the checkpoint in front of it. |
| `conversation-cache-mib` | engine | MiB | 0 (off) | RAM budget for parked conversations. |
| `conversation-cache-slots` | engine | integer | 4 | Most parked conversations. |
| `conversation-cache-min-free-mib` | engine | MiB | 2560 | Physical RAM kept free when parking. |
| `conversation-cache-min-tokens` | engine | tokens | 0 | Park only conversations of at least this many tokens. |
| `conversation-cache-disk-mib` | engine | MiB | 0 (off) | Disk budget for parked conversations. [running](../running.md#conversations-on-disk---conversation-cache-disk-mib) |
| `conversation-cache-dir` | engine | directory | model cache folder | Another folder for the disk tier. |

## Vision

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `vision` | server | `off`, `dynamic`, `cpu`, a GPU number | `off` | Image input; booleans are refused. A GPU number, as `gpu` numbers them: encoder sidecar always on that card (about 2.2 GiB of its expert cache), whatever the engine's cards are and no engine role needed. `dynamic`: inside the engine, borrowing expert-cache slots only while it reads an image. `cpu`: sidecar on the CPU, no VRAM, slow. Latencies: [running](../running.md#vision---vision). A GPU number needs the `basalt-vision` binary next to `bin/basalt`. Needs a model file with a vision projector. One image is capped at 64 megapixels before it is decoded. |
| `vision-projector` | server | `auto`, `embedded`, or a projector file | `auto` | The projector image input reads with. `auto`: the model file's own when the encoder reads it, else the first projector found in the models folders; `embedded`: the model file's own; a path: that GGUF (read in place when the encoder reads it as it is, else rewritten once into the cache). The Loader lists the choices with the RAM each keeps. Only used when `vision` is not `off`. |
| `vision-hold` | server | `keep`, `release` | `keep` | With `vision` `dynamic`: `keep` leaves the projector's weights in pinned RAM (the first image is quick); `release` makes the engine free them after the warm-up and after each batch of images (`--vision-release`) and read them from the file again at the next one, so the RAM and commit they hold are free in between. The Loader leaves them out of the steady need and notes that a moment of that much free memory is needed while an image is read. Needs an engine built with `--vision-release`. |
| `vision` | entry | `off`, `dynamic`, `cpu`, GPU number | `off` | Image input. A GPU number runs a sidecar on that card (about 2.2 GiB of its expert cache). `cpu` is slow. Needs a model with a vision projector. 32 MiB and 64 megapixels per image. [running](../running.md#vision---vision) |
| `image-url-host` | server | `HOST` or `HOST:PORT`, repeatable | none | Allow `http(s)` image URLs on this host from other machines. [image sources](image-sources.md) |
| `image-dir` | server | directory, repeatable | none | Allow image files under this directory from other machines. [image sources](image-sources.md) |
| `image-remote-only` | server | switch | off | Treat this machine's requests as remote too. [image sources](image-sources.md) |
| `video-max-tokens` | entry | 64 to 65536 | 8192 | The most image tokens one video takes over all its frames. Video needs `vision` and FFmpeg 6, 7, 8 or 9 on the machine. [running](../running.md#video) |

Diagnostic flags: [diagnostics](diagnostics.md).

## Control vectors

A GGUF control vector added to the residual stream. A request switches it with `experimental_speed_projection`. A layer split
or a prompt split keeps it on every card: each stage applies the vector to the steered layers it reads.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `control-vector` | engine | file path | off | A control vector at scale 1. |
| `control-vector-scaled` | engine | `FILE:SCALE[,...]` | off | Control vectors with their scales. |
| `control-vector-layer-range` | engine | two layer numbers, `A B` | 1 to the last layer | The layers it follows, inclusive. |
| `cvec-mode` | engine | `add`, `project` | `add` | `add`: h += s v. `project`: h -= s (h.v) v with v a unit vector. |
| `cvec-dir` | engine | `per-layer`, `single:L` | `per-layer` | Each layer's own direction, or layer L's direction everywhere (`project`). |

## Sampling

The request fields, the defaults a request inherits, the thinking budget and the loop safeguard are in
[sampling](sampling.md).
A request carries its own sampling fields. A field the request leaves out takes the launch option of the same name
(`--temperature`, `--top-p`, ... in the [server options](#server); command line over the config file's `server`
block), then the model file's `sampling` default (below), then the engine's. Request fields always win. `/props`
reports the defaults in effect.

| Field | Type, values | What it does |
| --- | --- | --- |
| `temperature` | number >= 0 | `0` is greedy (argmax); higher is more random. Greedy requests stay exact under every other setting. |
| `top_p` | number in (0, 1] | Nucleus sampling: keep the smallest set of tokens whose probability adds up to this. `1` is off. |
| `top_k` | integer 1..64 | Keep only the K likeliest tokens. The sampler keeps at most 64; a larger value, or `0`, is 64. |
| `min_p` | number in [0, 1] | Sampling cutoff: drop tokens less likely than `min_p` times the likeliest token's probability. `0` is off. It acts on the model's tokens. |
| `repetition_penalty` | number > 0 | Multiplicative penalty on tokens seen in the window; `1` is off. |
| `presence_penalty` | number | Flat penalty on any token seen in the window; `0` is off. |
| `frequency_penalty` | number | Penalty that grows with how often a token was seen; `0` is off. |
| `penalty_last_n` | integer > 0 | The window, in tokens, the three penalties look back over. Applies only when a penalty is set; 64 when omitted. |
| `seed` | integer > 0 | Fixes the random draw for a reproducible sampled reply. |
| `end_turn_bias` | number from -20 to 0 | A bias on the token that ends a turn (`<|im_end|>`), so the model is a little less ready to stop; `0` or absent is off. Chat Completions only, and not with parallel slots (it is a `logit_bias`; see [Request logit bias](precision.md#request-logit-bias)). The Chat's settings, the saved presets and the agent's conversations carry it; a provider other than this server never receives it. |
| `relaxed_drafting` | number in [0, 1], or `false` | This request's relaxed-drafting threshold; `false` or `0` forces exact sampling. Needs speculation; see [Speculative decoding](#speculative-decoding-mtp). |

Other request fields the API reads: `max_tokens` / `max_completion_tokens` (the Anthropic `max_tokens`; `0` or
`-1` means the rest of the context), `stop` / `stop_sequences`, `logit_bias`, `reasoning_effort`, `enable_thinking`
and `reasoning_budget_tokens` (a cap on thinking tokens; `0` or `-1` is no limit). Anthropic's `thinking.budget_tokens` and
llama.cpp's `thinking_budget_tokens` are the same budget, and `reasoning_budget_tokens` wins when more than one is
sent. A request that names none of them has no thinking budget.

### The model file's sampling defaults

The model file's `meta` section carries a `sampling` object. Its keys are the request fields above, with the same
meaning:

| Key | Allowed values |
| --- | --- |
| `temperature` | number >= 0 |
| `top_p` | 0 < value <= 1 |
| `top_k` | integer 1..64 |
| `min_p` | 0 <= value <= 1 |
| `presence_penalty` | number >= 0 |
| `frequency_penalty` | number >= 0 |
| `repetition_penalty` | number > 0 (`1` = off) |
| `penalty_last_n` | non-negative integer |
| `seed` | positive integer |

A bad value stops the server at startup; an unknown key is named in a warning and ignored. The defaults in effect
are printed at startup as `[server] sampling ...`, each marked `(cli)` or `(config)` when a launch
option set it and unmarked when it comes from the model file. A launch option overrides the model file's value for
that key; the others stay.
| `control-vector` | engine | file | off | Flash-Next only. A vector at scale 1. |
| `control-vector-scaled` | engine | `FILE:SCALE[,...]` | off | Flash-Next only. Vectors with their scales. |
| `control-vector-layer-range` | engine | `A B` | 1 to last layer | Flash-Next only. Layers it applies to, inclusive. |
| `cvec-mode` | engine | `add`, `project` | `add` | Flash-Next only. `add`: h += s v. `project`: h -= s (h.v) v. |
| `cvec-dir` | engine | `per-layer`, `single:L` | `per-layer` | Flash-Next only. Each layer's own direction, or layer L's everywhere. |
