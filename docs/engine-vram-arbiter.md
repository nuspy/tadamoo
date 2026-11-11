# The VRAM arbiter: elastic KV and VRAM lending together

The expert cache on the main card is one reserved address range backed by physical segments. Its last segments have
two users: the elastic KV, which takes them while a request needs more than the base cells, and `VRAM <reserve_mib>`
(the server's `POST /v1/vram`, with `--vram-elastic`), which gives them to other programs between requests. This page
says how one owner decides between them. The code is `src/program/serve/vram_arbiter.cpp` (the rules, host
arithmetic only) and `include/basalt/core/segment_plan.hpp` (the segment layout).

## Before

The two excluded each other. With `--vram-elastic` the session backed every KV cell at start (no elastic KV), so the
cache held the KV's whole size less; without it, `VRAM` was refused. The reasons in the code were:

- the layout: `--vram-elastic` cut the whole arena into `--vram-segment-mib` segments and dropped the granule-sized
  tail the KV shrinks by, so `elastic_bytes()` was 0 and the KV had nothing to take;
- the reserve: `kv_fit` and `cache_regrow` measured their room against the start reserve, so a request after a lend
  would map the lent VRAM back into the cache while the other program had not taken it yet;
- the rounding: a shrink keeps the segment that holds its target, which inside a large segment frees less than the
  KV asks for; the KV then maps the difference out of free VRAM, which during a lend is the lent memory;
- nothing kept the KV's growth: a lend could leave the cache too small for the KV to reach `--max-context`.

## Claims

The cache's backed size is decided for three claims, in this order:

1. **The base**: what running sessions already hold (their backed cells, the prompt path's floor of slots). Never
   taken by anything below.
2. **KV growth**: `kv_fit` for an active request. It takes cache segments, never VRAM another program holds.
3. **Lending**: `VRAM <reserve_mib>`. It gets what is left after 1 and after 2 at its largest (every elastic session
   at its own maximum context).

## The policy

A lend never takes what the KV needs to reach its maximum context. The cache's lowest backed end during a lend is

    lend_floor = boundary_above(bytes_of(floor_slots)) + kv_growth

where `floor_slots` is the larger of the `VRAM` command's floor (128 slots plus a 256-token prompt chunk's) and the
elastic KV's floor (`cache_floor_slots`), `kv_growth` is the sum over the elastic sessions on the main card of what
backing their maximum cells would add now (`session_kv_grow_bytes(s, s.max_cells)`), and `boundary_above(x)` is the
first segment boundary at or past `x`. A `VRAM` asking for more is granted down to that floor, exactly as the cache's
floor was already handled: the reply says so (`capped=1`, the log line's note), `reserve_max_mib` is the largest
reserve that would have been granted in full, and `vram_free_mib` is what is free.

Why this one and not "lend anything, refuse the request later":

- Lent VRAM belongs to another process once it allocates. CUDA has no way to take it back, and under WDDM the only
  way back is eviction, which can hang the engine (see [WINDOWS](WINDOWS.md#the-commit-of-the-gpu-memory)). A
  request refused because of a lend would fail at a time that depends on what the other program did, so the same
  request would pass or fail from one run to the next.
- The borrower learns its share when it asks, before it allocates; a request never learns about a lend.
- The KV's reach is the elastic tail the cache gained in the first place: lending stops where the cache would be
  smaller than with the KV fully resident plus the floor.
- It is the existing floor rule with one more term, so `VRAM` has one rule, not two.

Without the elastic KV (`kv_growth` 0) the floor and every decision are the ones `--vram-elastic` had before.

## State

`reserve_mib`: the VRAM kept free now, the start's `--vram-reserve-mib` until a `VRAM <reserve_mib>` sets another.
`lent_mib = max(0, reserve_mib - start)`. Every room the cache or the KV measures is `free - reserve_mib`: a KV fit
takes the room first and then cache segments; `cache_regrow` (after the KV gives cells back, an idle conversation
moves out or a lend is returned) grows only into that room, so it never maps lent VRAM.

| state | what holds the tail | moves |
| --- | --- | --- |
| full | the cache | `kv_fit` -> KV grown; `VRAM r` -> lent |
| KV grown | the KV (its cells past the base) | `kv_release`, a park -> full; `VRAM r` -> lent + KV |
| lent | another program (above `lend_floor`) | `kv_fit` -> lent + KV; `VRAM` (return) -> full |
| lent + KV | both, the KV inside its reach | `kv_release` -> lent; `VRAM` -> KV grown |
| asleep | nobody (`SLEEP`) | `WAKE` -> the state before, the lend kept |

`VRAM` alone returns the lend: the reserve goes back to the start's and the cache grows into what is free, each slot
refilled from RAM. `SLEEP` shrinks to the floor without the KV term (nothing runs until `WAKE`) and leaves
`reserve_mib` as it was; `WAKE` grows back to that reserve, so a lend survives a sleep.

## The layout

With both features the arena is `--vram-segment-mib` segments up to the elastic tail, then segments of the driver's
granularity over the tail. A lend's shrink keeps the segment its target falls in, so it lends at most one segment
less than asked. While VRAM is lent, a KV fit that ends inside a large segment gives that whole segment back (the
largest boundary at or below its target): the part the KV does not map stays free above the reserve and is the next
fit's room. With nothing lent the KV's shrink is the one it always had.

## RAM copies

With the resident RAM copy of the experts, a lend's shrink first copies to RAM the experts of the slots it gives back
and of the slots the KV may take afterwards (`[slots_within(boundary_below(kept - kv_growth)), kept)`), so neither a
lend nor a later KV fit reads an expert from the file. It is refused, nothing changed, when the RAM above the floor
cannot hold them. A return frees the copies of the slots below both the prompt loan and the KV's reach.

## Failure modes

- **The borrower allocates more than it was lent.** Free VRAM drops below the reserve. The KV still grows (it takes
  cache segments, which are the engine's), the cache does not grow back, and on Windows the engine's memory can be
  evicted: keep `--keep-vram-commit` when a borrower may exceed its share.
- **The borrower holds the lent VRAM and the KV needs to grow.** The KV never needs it: its reach was kept.
- **Another program allocates between the cache's shrink and the KV's map.** The map fails and the request is
  refused with the KV's error, which then says how much VRAM is lent and that `VRAM` returns it; the freed segment
  is mapped back by the next regrow.
- **A return while the borrower has not freed its memory.** The cache grows as far as free VRAM allows; the reply's
  `expert_slots` and `vram_free_mib` say how far. The next `VRAM`, KV release or idle park grows it further.
- **RAM short for the copies.** The lend is refused; the cache is as it was.
- **Batch slots decoding, a prompt loan out.** `VRAM` is refused, as before.

## Windows: freeing, not eviction

A lend releases the segments' physical memory (`cuMemUnmap` and `cuMemRelease`), so the borrower's allocations come
out of free VRAM and never make the video memory manager evict the engine. The segments and KV pages mapped again
later are new allocations: with the decommit of the VRAM backing on (the default), the sweep after each command gives
their backing back as it does for any allocation. The eviction hazard is unchanged for a borrower that goes past its
share.

## Protocol

The `VRAM` reply line keeps its keys and adds `lent_mib`, `expert_slots_start`, `kv_growth_mib`, `reserve_max_mib`
and `capped`. `INFO` adds `kv_elastic=<cells>` (the base cells, 0 without the elastic KV); `vram_elastic=1` now
appears together with it. The server reads both lines as `key=value` and passes the `VRAM` reply through
`/v1/status`'s `vram`, so it needs no change to work.

## What does not change

A server without `--vram-elastic` builds the same arena, the same slots and makes the same KV decisions as before:
the rules above reduce to the old formulas there, and decode is bitwise identical. Precision is not touched: only
where experts and KV cells live changes, never how they are computed.
