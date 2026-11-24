# Diagnostics and measurement flags

Flags for measuring, debugging and testing the engine. All are off by default and are not settings.
Binary: **CLI** = `bin/basalt` one-shot (`--tokens IDS`), **serve** = `--serve` engine, **bench** = `basalt-bench`.

## One-shot generation

| Flag | Binary | Default | What it does |
|---|---|---|---|
| `--tokens`, `--tokens-file` | CLI | required | The prompt as token ids. |
| `--max-new` | CLI | 16 | Tokens to generate. |
| `--greedy` | CLI | on | Argmax decoding. |
| `--seed` | CLI | | Sample with this seed under `--top-k` 20, `--top-p` 0.95, `--temperature` 1.0; the same seed gives the same text on every model, and `--temperature 0` stays greedy. |
| `--stop-eos` | CLI | off | Stop at an end-of-turn token. |
| `--eos-ids` | engine | the pack's | The end-of-turn tokens. |
| `--gpu-plan` | engine | | Print the `--gpu` choice as JSON and exit. |

## Timing and profiles

| Flag | Binary | What it does |
| --- | --- | --- |
| `--stats` | CLI | Print the per-token wall time, its host phases and the expert pool's breakdown. |
| `--no-pool` | CLI | Skip the CPU expert pool: the GPU-only floor, without the experts the CPU computes. |
| `--no-capture` | CLI | Run the layers directly instead of replaying graphs. Needs `--no-pool`. |
| `--stage-timing` | CLI | Per-stage shares by kernel count, not a time profile (an uncaptured interval includes host gaps). Needs `--no-capture`. |
| `--graph-only` | CLI | Replay the per-layer `pre` graphs only and time them. Leaves out the `post` graphs and the head, so it is not the GPU floor. |
| `--gpu-only-full` | CLI | Replay every layer's graphs and the head with no pool: the per-token GPU floor. Not with a layer split. |
| `--gpu-stages` | CLI | Capture each layer as three graphs (mixer, FFN and router, post), time them from outside the capture, print and exit. Not with a layer split. |
| `--profile` | serve | GPU stage times of the decode windows, per request. Turns programmatic dependent launch off, so not for A/B timing. |
| `--wait-stats` | serve | The host's wait times in the decode loop, one report per 200 windows. |
| `--decode-timing` | serve | One line per request: where its decode time went. |
| `--draft-log` | serve | Path. One line per verify window (width, accepted, time), for studying the draft policy. |

## Speculation tests

| Flag | Binary | Value | What it does |
| --- | --- | --- | --- |
| `--spec-oracle` | CLI | path | With `--spec N` on a pack without a drafter: draft from a token file (the expected continuation), the exactness test of the verify path. Prompt lookup windows still take precedence. |
| `--spec-corrupt` | CLI | integer, 0 | Replace every Nth draft with a wrong token, to test rejection. |

## Scheduling tests

| Flag | Binary | What it does |
| --- | --- | --- |
| `--force-zero-doorbell` | serve | Run every decode window, solo and batch, on the zero-doorbell graph a fully resident pack uses, whatever the residency. The routed experts not in VRAM drop out, so the output is wrong: it tests the windows' scheduling on a card that cannot hold every expert. |

## The n-gram table reads

The per-layer n-gram (PLE) table stays on disk and its rows are read as needed.

| Flag | Binary | Value, default | What it does |
| --- | --- | --- | --- |
| `--ple-io` | engine | `direct`, `ram`; `direct` | `direct`: unbuffered reads, the table never enters RAM or the file cache. `ram`: map the file with the whole table locked in RAM at start. |
| `--ple-row-cache` | engine | rows, 1048576 | A bounded cache of fetched rows, 90 B each; `0` turns it off. |
| `--ple-inflight` | engine | integer, 256 | Outstanding reads. |
| `--ple-delay-us` | engine | microseconds, 0 | Fault injection: each row read completes no earlier than this. |

## Quality gate

`basalt-bench --quality` runs it; [how quality is measured](../precision/quality.md) has the method.

| Flag or key | Binary | Value, default | What it does |
| --- | --- | --- | --- |
| `--quality` | bench | chunks, 20 | Run the PPL/KLD/top-1 gate through the decode path. Alone it runs only the gate; with `-p` or `-t` it runs after them. |
| `--kld-base` | bench | path | The llama.cpp KL-divergence base file. |
| `--quality-t` | bench | tokens, 3 | Tokens per verify window in the gate. |
| `--ppl-base` | serve | path | The base file that `ppl=` requests score against. |
| `--ppl-out` | serve | path | Where `ppl=` requests write their per-row lines. |
| `ppl=K` | request key | chunk index | Score chunk K of `--ppl-base`, the rest of the chunk teacher-forced through verify windows. |
| `ppl_t=T` | request key | tokens, 3 | The window width of a `ppl=` request. |

The request keys go on a `GEN` line of the engine's stdin protocol, before the token ids.

## basalt-bench

`basalt-bench --help` gives the full text and how the fixtures are made; [building](../building.md) has the community
preset (`--community`, `--submitter`, `--community-file`).

| Flag | Value, default | What it does |
| --- | --- | --- |
| `-t` | any of `conv,struct,prose` | Decode fixtures, always run in that order (draft state carries across requests). |
| `-p` | depths in tokens (`32k` style accepted) | Prefill depths. Without `-t` or `-p` the bench runs `-p 16384 -t conv,struct,prose`. |
| `-r` | integer, 1 | Repetitions of the whole set; above 1 the table shows mean and standard deviation, the first repetition cold. |
| `--seed` | integer >= 1 | Seed every decode request, so the sampled fixtures give the same text each run. |
| `--model`, `--gpu`, `--spec` | as for the engine | Passed to the engine; `--spec 0` is a plain-decode baseline. |
| `--set` | `"FLAG VALUE ..."` | Replace or add engine arguments; repeatable. Arguments after `--` do the same. |
| `--engine` | path | The engine binary; default the one built with the bench. |
| `--fixtures` | directory, `data/bench` | Where the pre-tokenized prompts are. |
| `-o` | `md`, `json`; `md` | A markdown table, or the full detail as JSON on stdout. |
| `--power-label` | watts | The power limit to print instead of the one read from the GPU. |
| `--logs` | directory | Keep the engine log, the draft log and the quality rows there. |
| `--no-windows` | switch | Do not ask the engine for the per-window log; no window statistics. |
| `--print-command` | switch | Print the engine command and exit. |
| `--hardware` | switch | Measure and print the CPU, RAM read speed and each GPU's PCIe link and copy speed, then exit; no model. |

`basalt-device`, a helper in the build directory, takes `--list-devices` (every GPU the runtime sees and whether this
build runs on it), `--link-rates` (one `pcie_h2d BUS_ID GB_PER_S` line per GPU, which `--hardware` reads) and
`--selftest`; with no flag it prints the main GPU and the memory plan.
|---|---|---|
| `--stats` | CLI | Per-token wall time, host phases and the pool's breakdown. |
| `--no-pool` | CLI | Skip the CPU expert pool: the GPU-only floor. |
| `--no-capture` | CLI | Run layers directly, not as graphs. Needs `--no-pool`. |
| `--stage-timing` | CLI | Per-stage shares by kernel count. Needs `--no-capture`. |
| `--graph-only` | CLI | Time the per-layer `pre` graphs only. |
| `--gpu-only-full` | CLI | Replay every graph and the head, no pool: the per-token GPU floor. Not with a layer split. |
| `--gpu-stages` | CLI | Time each layer's three graphs, print, exit. Not with a layer split. |
| `--profile` | serve | GPU stage times per request. Not for A/B timing. |
| `--wait-stats` | serve | Host wait times in the decode loop. |
| `--decode-timing` | serve | One line per request on where decode time went. |
| `--draft-log FILE` | serve | One line per verify window (width, accepted, time). |

## Dumps and speculation tests

| Flag | Binary | What it does |
|---|---|---|
| `--dump-residual`, `--dump-mixed`, `--dump-final-r` | CLI | Write residual or head inputs as f32, for bisecting. |
| `--dump-routing` | CLI | Routed expert ids and weights per layer and position. |
| `--spec-oracle FILE` | CLI | With `--spec N` on a pack without a drafter: draft from an expected continuation. The verify exactness test. |
| `--prefill-until P` | CLI | Batch the first P prompt positions; run the rest teacher-forced. |
| `--spec-corrupt N` | CLI | Replace every Nth draft with a wrong token, to test rejection. |
| `--diag-batch-check` | CLI | With `--batch N` (2 or more) and a prompt: every session's windows alone against the same windows batched, bitwise ([batched decoding](../design/batching.md)). |
| `--diag-teacher` | CLI | DeepSeek with `--spec-oracle`: teacher-forces the file's tokens and counts differing picks. |
| `--diag-tier-frozen` | CLI, serve | DeepSeek: fixed placement (no copies or promotion). |
| `--diag-drafter-gpu DEV` | CLI, serve | DeepSeek: the card the drafter runs on. |
| `--force-zero-doorbell` | serve | Scheduling test on a card that cannot hold every expert. The output is wrong by design. |

## Experts and n-gram table

| Flag | Binary | Default | What it does |
|---|---|---|---|
| `--ple-io direct`, `ram` | engine | `direct` | Read the n-gram table unbuffered (`direct`) or lock it in RAM (`ram`). |
| `--ple-row-cache`, `--ple-inflight`, `--ple-delay-us` | engine | 1048576 rows, 256, 0 | Row cache size, outstanding reads, and injected read delay for tests. |

## Quality gate and bench

| Flag or key | Binary | Default | What it does |
|---|---|---|---|
| `--quality` | bench | 20 chunks | Run the PPL/KLD/top-1 gate through decode. [method](../precision/quality.md) |
| `--kld-base FILE` | bench | | The llama.cpp KL-divergence base. |
| `--quality-t N` | bench | 3 | Tokens per verify window in the gate. |
| `--ppl-base`, `--ppl-out` | serve | | Base file for `ppl=` requests, and where they write rows. |
| `ppl=K`, `ppl_t=T` | request keys | | Score chunk K; window width T. |

| Bench flag | Default | What it does |
|---|---|---|
| `-t conv,struct,prose` | | Decode fixtures, run in that order. |
| `-p DEPTHS` | `-p 16384` with no `-t` or `-p` | Prefill depths, e.g. `32k`. |
| `-r N` | 1 | Repetitions; above 1 the table shows mean and deviation. |
| `--seed N` | | Seed every decode request. |
| `--set "FLAG VALUE"` | | Replace or add engine arguments (repeatable). |
| `--engine PATH`, `--fixtures DIR` | `basalt` beside the bench, `data/bench` | Binary and fixtures. |
| `-o md` or `json` | md | Output format. |
| `--power-label W` | read from the GPU | The power limit to print. |
| `--logs DIR`, `--no-windows`, `--print-command` | | Logs, window stats, command printing. |
| `--hardware` | | Measure CPU, RAM and each GPU's link, then exit. |
| `--community`, `--submitter NAME`, `--community-file PATH` | bench | | The community preset and its row, which names the model ([community runs](../community.md)). |

`basalt-device` (in the build folder): `--list-devices`, `--link-rates`, `--selftest`.

## Experiment keys (`--exp`)

`--exp NAME=VALUE[,...]` sets experiment switches. They carry no stability promise and go away with their experiment.

- `concur9_logit_hash=1`: print a hash of each accepted row's head logits on stderr.
- `spec_log=1`: with `--draft-log`, also write each row's target distribution.
- `host_ev=1`: print GPU time per window's verify, commit and draft, and the idle gap.
