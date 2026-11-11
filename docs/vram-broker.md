# The VRAM broker

The GPU holds the model's expert cache. Functions that need VRAM only now and then (document reading, embeddings and reranking,
the avatar, later image and video models) borrow it from that cache for the time they work and give it back. A heavy function runs
alone: the model may become very slow meanwhile, and the cache may shrink to its minimum. A bigger card simply never lends.

## What lends the VRAM

The engine, with `--vram-elastic` (`engine.vram-elastic` in the config file). `POST /v1/vram {"reserve_mib": N}` shrinks the expert
cache so that N MiB stay free; `{"reserve_mib": null}` gives the cache its VRAM back (it refills from RAM in a few seconds). The reply
carries `reserve_mib`, `expert_slots`, `expert_slots_full`, `expert_cache_mib`, `vram_free_mib` and, with newer engines, more keys
(`lent_mib`, `capped`, `reserve_max_mib`, ...): the broker keeps the last reply whole and depends on none of the extra keys.

## The broker (`serve/vram_broker.py`)

`VramBroker(service, probe=...)` puts a lease around that command. One lease at a time.

- `acquire(owner, mib, purpose, hold_s, wait_s)` answers `{"ok": true, "lease": {id, owner, mib, granted_mib, lent_mib, expires_at,
  ...}}` or `{"ok": false, "reason", "message", "message_it"}`. It never raises.
- `touch(id)` extends the hold; `release(id)` gives the VRAM back; `state()` shows the lease, the queue, the free VRAM and the reserve.
- The reserve asked is the reserve in force (the last one set, else the one the engine started with) plus `mib`. The free VRAM is read
  afterwards, NVML through `serve/loader.system_now`, and the lease is granted only if it is at least `mib`; else the reserve goes back
  at once and the answer is `not_enough`. If the free VRAM already covers `mib` plus the engine's own reserve nothing is lent.
- With no model loaded the lease only checks the free VRAM.
- `hold_s` without a `touch` ends the lease (a timer thread looks once a second; a caller that died is covered). When the last lease
  ends, and no one waits, the reserve goes back to what it was before the first lease; also when the server stops (best effort). If the
  engine is busy with a request when that happens, the broker tries again every second.
- `wait_s` above 0 puts the caller in a FIFO queue instead of answering `busy`. The reserve is not put back between two queued jobs.

| reason | meaning | what the caller does |
|---|---|---|
| `no_engine` | nothing here holds VRAM | CPU |
| `unavailable` | the engine does not run with `--vram-elastic` (or the free VRAM cannot be read, or the broker is off: `disabled`) | CPU |
| `busy` | another lease, a model that is loading, or a request the engine still runs | wait, or CPU |
| `not_enough` | not enough even with the whole cache lent | CPU, or a smaller model |
| `engine_died` | the engine ended while asked | try later |

## Routes

All from the PC the server runs on (a phone cannot lend the PC's VRAM). The voice service reaches the three lease routes with its
bearer token on loopback, and no other `/api/vram` route.

| route | what |
|---|---|
| `POST /api/vram/lease {owner, mib, purpose?, hold_s?, wait_s?}` | 200 with the lease, or 409 (400 for a bad request) with the reason |
| `POST /api/vram/lease/<id>/touch` | extend the hold; 404 when the lease ended |
| `POST /api/vram/lease/<id>/release` | give the VRAM back; 404 when it already ended |
| `GET /api/vram/leases` | the state: `leases`, `queue`, `lent_mib`, `reserve_mib`, `free_mib`, `engine`, `last_reply` |
| `GET/POST /api/vram/settings {enabled?, hold_s?}` | the persisted block: `enabled` (true), `hold_s` (60, from 5 to 3600) |

The block is `vram-broker.json` in the user data folder, not the `--config` file: that file is read by the engine's own parser. Settings >
General shows one read-only line: what is lent now and the free VRAM.

## The client (`tadamoo/vram_lease.py`)

Standard library only, so the voice service and the knowledge workers import it without importing `serve`.

```python
with VramLease("docling", 3000, "reading a PDF", token=voice_token) as lease:
    device = "cuda" if lease.gpu_ok else "cpu"
```

`gpu_ok` is False, with `reason`, `message` and `message_it`, when the broker refused, answered `unavailable` or `no_engine`, or does not
answer at all (granted_mib 0: use the CPU). While the block runs the hold is extended every third of `hold_s`; if the server forgets the
lease (`lost` True) the VRAM may be taken back. A program that is not the voice service sends the server's control secret
(`control_secret=`) as every other change of the server does.

## What the broker does not do

It does not choose who goes first: the queue is first come first served, and a lease held by a job that is only idle (the avatar
between two sessions) blocks the others until its owner lets go. It does not lend while a second GPU is the expert tier or while a
prompt's loan is out (the engine refuses; the answer is `unavailable` or `busy`). The first load of a model that wants VRAM still has
to ask for it: nothing is lent unasked.

The picture and video tools are users too (owners `image` and `video`, 11000 and 25000 MiB, [media/README.md](media/README.md)).
The avatar is the first user: [voice/loading.md](voice/loading.md#avatar-on-request). The voice functions share one lease (owner `voice`,
sized to the sum of what they borrowed) because only one holder is allowed: [voice/loading.md](voice/loading.md#a-function-added-while-the-model-runs).

Lending is Basalt-Plus and Strata only. With a cloud service answering, or llama.cpp or vLLM, a lease only checks the free VRAM.

When even the whole cache is not enough (`not_enough`), a heavy job may ask to put the engine to sleep and move the conversations to a provider the
person chose: [heavy-swap.md](heavy-swap.md).
