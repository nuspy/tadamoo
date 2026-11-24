# basalt-probe

`bin/basalt-probe` starts the engine (as `basalt-bench` does), measures the machine's rates and the cold speed on three fixtures, and
prints suggested values. It is manual: it changes and saves nothing.

```
basalt-probe --model PACK.basalt -- ENGINE FLAGS...
```

| Option | Meaning |
|---|---|
| `--model FILE` | the model file |
| `--engine BIN`, `--fixtures DIR`, `--logs DIR` | as for `basalt-bench` |
| `--gpu A,B` or `auto` | the cards, as for the server; default GPU 0 |
| `-- ARGS` | engine flags, as for `basalt-bench`; the report prints the flags it used. `--expert-profile-save FILE` is compared, never written |

## What it does

1. Starts the engine and reads the rates with `PROBE rates` (GPU copy and link rates, host read rate, CPU expert time per worker count).
2. Runs `conv`, `struct` and `prose` once each, greedy, 400 tokens. These are **cold** speeds: the first request after load counts.
3. Measures its suggestions (below).

The token budget is part of "cold": the tiers keep adapting, so a longer request reads faster.

## Suggested values

Printed only when a value beats the built-in default by more than the measured noise:

- **`--pool-workers N`**: the fewest workers within 2% of the fastest measured CPU expert time.
- **`--adapt-every`, `--adapt-swaps`, `--adapt-decay`**: tried from the start placement (`PROBE adapt`) with a prose-then-struct switch.
  `64 / 256 / 0.7`, `1 / 160` and a small round (swaps 12, the default cadence) are tested against the engine's own, which runs twice to set the noise band.
- **`--prefill-cpu-share F`**: 8192-token chunks at share 0, 1 and the balance. Printed only when at least 8% faster.
- **`--expert-profile-save FILE`**: suggested when a saved profile is faster beyond the band.

## Engine command: `PROBE rates`

The probe suggests a value only where a quick measurement on this machine can pick it and a measured run shows the
pick matters. The other settings with machine-dependent defaults, and why it leaves them:

| setting | covered | why | seconds added |
|---|---|---|---|
| `pool-workers` | yes | the CPU expert time per worker count, from `PROBE rates` | 0 (part of the rates) |
| `prefill-cpu-share` | yes | timed 8192-token chunks at share 0, 1, the balance and the default | the chunks, a few seconds |
| `adapt-every`, `adapt-swaps`, `adapt-decay` | yes | each setting from the start placement in the loaded engine (`PROBE adapt`), no restart; a reset scores within noise of a fresh engine's first requests ([adaptive tier](configuration/adaptive.md)) | 35 (two cards) to 46 (one card): 5 runs of 7 to 9 s, each reset under 1 s |
| `expert-profile-save` | yes | the saved file against the engine's profile, same setting, same sequence | one of those runs |
| `peer-adapt-swaps` | no | fewer peer swaps lost on every workload measured; it follows `adapt-swaps` | 0 |
| `vram-reserve-mib`, `peer-reserve-mib` | no | lower reserves add about 1% more slots, inside run noise; they guard against paging and the start says when one is short | 0 |
| `spec` | no | fixed at start (no per-request value), and spec 7 against 8 differs by 1-2%, inside one run's noise | 0 |
| `prefill`, `expert-cache` | no | sized at start from what the cards can hold and lend | 0 |
| `peer-exclusive`, `resident-budget-gib`, `pool-affinity` | no | picked at start from the RAM and the CPU layout (`[server] auto`) | 0 |
| `suffix-draft`, `mtp-max-t`, `mtp-window`, `short-read` | no | properties of the model and the workload, not the machine | 0 |
| `kv`, `max-context` | no | memory and precision choices, not speed | 0 |

During a probe the RAM copy's free places hold four times 256 experts instead of four times the given count: about
1.6 GiB more RAM than the built-in 96 on the NVFP4 pack.

The engine's `PROBE set workers=K share=F` sets the pool's active worker count and the share for the requests that
follow, between requests; the probe uses it and the engine is stopped afterwards.

## Report

```
basalt-probe
  5090 1541 GB/s | 5060 Ti 388 GB/s | PCIe 28.9 GB/s | RAM 58 GB/s
  experts 5090 9553 / 5060 Ti 5711 / RAM rest | KV int8 | ctx 262k
  decode cold  prose 194 conv 255 struct 223 tok/s (measured)
  adapt test  1/96/0.85 284 286 | 64/256/0.70 203 | 1/160/0.85 285 | saved profile 286 tok/s (band 3.0%)
  suggested: --pool-workers 8 (52 us/expert, best 51 us)
  suggested: --prefill-cpu-share 0.45 (prefill +9%)
  config: --gpu 0,1 --kv int8 ...
  wall  97.3 s

  Measured on this machine. Hand-tuned settings can still beat these.
```

The `wall` line is the total wall time of the run, engine launch included. (The numbers above only show the layout.) Timing and engine progress go to stderr.

## `PROBE rates` (engine command)

With `--serve`, the line `PROBE rates` on stdin, between requests, measures the machine's rates inside the loaded
engine in about half a second and prints one block on stdout: `PROBE begin rates v1`, lines `PROBE <section> key=value ...`,
`PROBE end ms=<wall>`. It changes no engine state (tiers, caches, KV, RNG, the CPU pool's active worker count) and saves
nothing. Sections:

| section | measures |
|---|---|
| `gpu`, `host`, `pack` | device, link generation and width, power limit, RAM, CPU pool, expert format |
| `link` | pinned host-to-device and device-to-host GB/s per GPU, best of six 64 MiB bursts |
| `gpu_mem` | device-to-device copy GB/s per GPU (counts the bytes read and written) |
| `host_mem` | GB/s the CPU pool reads from RAM-resident experts, by active worker count |
| `cpu_expert` | cold experts through the pool at 1, 2, 3 and 8 per call, at every measured worker count and the current one |
| `peer` | peer round trip by entry count (1 to 8) and the linear fit: base and per-entry microseconds |
| `copy` | one expert-sized copy into each GPU, and the peer round trip while such a copy runs |

A step that cannot run prints `n/a` with a reason (no peer, no RAM-resident experts, a non-native expert format).
During a batch the command answers `ERR PROBE: ...`.

`PROBE set workers=K share=F` (either key) sets the CPU pool's active worker count and the prompt path's CPU share for the
requests that follow, and answers `PROBE end workers=<count>`. It is for `basalt-probe`; the settings last until
the engine stops.

`PROBE adapt [every=N] [swaps=N] [decay=F] [profile=FILE]` puts the expert tiers back where they were at `READY`, or
with `profile=` places that saved profile's ranking into the same slots, copying only the slots whose expert differs.
It zeroes the routing counts, the round counter and the draft policy, drops the session's cached prompts, and sets the
adaptive tier's settings for the requests that follow, so the next request runs as the first after a load. It answers
`PROBE end adapt every= swaps= decay= moved= peer_moved= kept= ms=` (slots copied on each card, slots left as they
were, wall time). It needs the adaptive tier on and one main card (no layer split, at most one peer card, not
`--peer-exclusive`); a swap count above the start's `--adapt-swaps` finds fewer free RAM places for its rounds.
With `--serve`, `PROBE rates` on stdin between requests answers in about half a second with `PROBE begin rates v1`, lines
`PROBE <section> key=value` (sections `gpu`, `host`, `pack`, `link`, `gpu_mem`, `host_mem`, `cpu_expert`, `peer`, `copy`) and
`PROBE end ms=`. It changes no engine state. `PROBE set workers=K share=F` sets the pool's worker count and the prompt CPU share;
`PROBE adapt [every=N] [swaps=N] [decay=F] [profile=FILE]` resets the tiers and sets the adaptive settings for the next request.
