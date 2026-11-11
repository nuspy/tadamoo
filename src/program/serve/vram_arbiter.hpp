// src/program/serve/vram_arbiter.hpp - one owner for how much of the main card's segmented expert cache is backed.
//
// Three claims share the cache's last segments, in this order: what the running sessions hold, the elastic KV's
// growth for a request, and VRAM lent to other programs (`VRAM <reserve_mib>`).  The arbiter keeps the reserve in
// force and computes every target; the caller shrinks, grows and maps.  Plain numbers in, decisions out; no device
// calls.  docs/engine-vram-arbiter.md is the design.
#pragma once

#include <cstdint>
#include <vector>

namespace basalt::program::serve {

class VramArbiter {
public:
    /// The cache's layout - each segment's end and each slot's end, ascending byte offsets (`seg_end` empty: one
    /// allocation) - the start's reserve, and whether `VRAM` may lend (--vram-elastic on a segmented cache).
    void start(std::vector<int64_t> seg_end, std::vector<int64_t> slot_end, int64_t reserve_mib, bool lending);

    bool lending() const { return lending_; }
    int64_t slots_at_start() const { return (int64_t) slot_end_.size(); }
    int64_t start_reserve_mib() const { return start_mib_; }
    /// The VRAM kept free now: the start's reserve until a `VRAM <reserve_mib>` sets another.
    int64_t reserve_mib() const { return reserve_mib_; }
    int64_t reserve_bytes() const { return reserve_mib_ << 20; }
    /// The reserve above the start's: VRAM granted to other programs.
    int64_t lent_mib() const { return reserve_mib_ > start_mib_ ? reserve_mib_ - start_mib_ : 0; }
    /// A `VRAM` command applied (SLEEP leaves the reserve, WAKE grows back to it), given the VRAM free once it is, the
    /// engine's own use then (the cache's backed bytes and the sessions' KV) and `hold_b`, free VRAM the KV still needs
    /// beyond the cache (kv_hold).  A reserve above the start's is what is granted: `mib` at most, what is free less
    /// `hold_b` at most, the start's at least.  While VRAM is lent the engine's room is what it frees beyond that use,
    /// so a borrower's allocations never count as the engine's.
    void set_reserve_mib(int64_t mib, int64_t free_b, int64_t usage, int64_t hold_b);

    int64_t full_bytes() const { return slot_end_.empty() ? 0 : slot_end_.back(); }
    /// The leading slots wholly inside the first `bytes` bytes, and the bytes the first `n` slots span.
    int64_t slots_within(int64_t bytes) const;
    int64_t bytes_of(int64_t n) const;
    /// The largest segment boundary at or below `bytes` (0 is one), and the smallest at or past it (at most the
    /// arena's end).  Without segments: `bytes` itself.
    int64_t boundary_below(int64_t bytes) const;
    int64_t boundary_above(int64_t bytes) const;
    /// The backed end `ExpertCache::shrink(keep)` leaves a cache backed to `mapped`: the segment `keep` falls in stays.
    int64_t kept_by_shrink(int64_t keep, int64_t mapped) const;

    /// What the cache and the KV may map now (never negative): the free VRAM above the reserve; while VRAM is lent,
    /// what the engine freed since the lend (`usage`: its use now), never out of the start's reserve.
    int64_t room(int64_t free_b, int64_t usage) const;
    /// The shrink target that gives back `short_by` bytes of a cache backed to `mapped`, on the `gran` grid.  With
    /// lending on it is a segment boundary, so the shrink frees at least that much and the KV never maps the reserve
    /// or lent VRAM.
    int64_t kv_keep(int64_t mapped, int64_t short_by, int64_t gran) const;
    /// The grow target after cells come back: `mapped` plus the room (`mapped` when there is none).
    int64_t regrow_to(int64_t mapped, int64_t free_b, int64_t usage) const;

    /// The lowest backed end a lend may leave: the floor's slots stay once the KV has grown by `kv_growth`.
    int64_t lend_floor(int64_t floor_slots, int64_t kv_growth) const;
    /// The free VRAM a lend must leave the engine when the cache is backed to `mapped`: what the KV's growth needs
    /// beyond the cache above the floor.
    int64_t kv_hold(int64_t mapped, int64_t floor_slots, int64_t kv_growth) const;
    /// What a `VRAM <reserve_mib>` does to a cache backed to `mapped` with `free_b` free: nothing, shrink to `bytes`
    /// (`capped`: the floor stopped it short of the reserve), or grow to `bytes`.  `kv_growth` 0 for SLEEP and WAKE.
    struct Plan {
        enum Kind { none, shrink, grow } kind = none;
        int64_t bytes = 0;
        bool capped = false;
    };
    Plan plan(int64_t reserve_mib, int64_t mapped, int64_t free_b, int64_t floor_slots, int64_t kv_growth) const;
    /// The largest reserve a `VRAM` command would grant in full now.
    int64_t reserve_max_mib(int64_t mapped, int64_t free_b, int64_t floor_slots, int64_t kv_growth) const;
    /// The first slot the KV may take from a cache backed to `kept` while it grows by `kv_growth`: the slots from it
    /// on keep a RAM copy of their experts during a lend.
    int64_t reach_from(int64_t kept, int64_t kv_growth) const;

private:
    std::vector<int64_t> seg_end_, slot_end_;
    int64_t start_mib_ = 0, reserve_mib_ = 0;
    int64_t budget_ = 0;   // during a lend: the use the engine may reach, its use then plus what it kept free
    bool lending_ = false;
};

}  // namespace basalt::program::serve
