// src/program/serve/vram_arbiter.cpp - see vram_arbiter.hpp.
#include "vram_arbiter.hpp"

#include <algorithm>
#include <utility>

namespace basalt::program::serve {

void VramArbiter::start(std::vector<int64_t> seg_end, std::vector<int64_t> slot_end, int64_t reserve_mib,
                        bool lending) {
    seg_end_ = std::move(seg_end);
    slot_end_ = std::move(slot_end);
    start_mib_ = reserve_mib < 0 ? 0 : reserve_mib;
    reserve_mib_ = start_mib_;
    lending_ = lending && !seg_end_.empty();
}

int64_t VramArbiter::slots_within(int64_t bytes) const {
    if (bytes >= full_bytes()) return (int64_t) slot_end_.size();
    if (bytes <= 0) return 0;
    return (int64_t) (std::upper_bound(slot_end_.begin(), slot_end_.end(), bytes) - slot_end_.begin());
}

int64_t VramArbiter::bytes_of(int64_t n) const {
    if (n <= 0 || slot_end_.empty()) return 0;
    return slot_end_[(size_t) std::min<int64_t>(n, (int64_t) slot_end_.size()) - 1];
}

int64_t VramArbiter::boundary_below(int64_t bytes) const {
    if (seg_end_.empty()) return std::max<int64_t>(0, bytes);
    if (bytes <= 0) return 0;
    const auto it = std::upper_bound(seg_end_.begin(), seg_end_.end(), bytes);
    return it == seg_end_.begin() ? 0 : *(it - 1);
}

int64_t VramArbiter::boundary_above(int64_t bytes) const {
    if (seg_end_.empty()) return std::max<int64_t>(0, bytes);
    if (bytes <= 0) return 0;
    const auto it = std::lower_bound(seg_end_.begin(), seg_end_.end(), bytes);
    return it == seg_end_.end() ? seg_end_.back() : *it;
}

int64_t VramArbiter::kept_by_shrink(int64_t keep, int64_t mapped) const {
    if (seg_end_.empty()) return mapped;
    if (keep <= 0) return 0;
    return std::min(mapped, boundary_above(keep));
}

void VramArbiter::set_reserve_mib(int64_t mib, int64_t free_b, int64_t usage, int64_t hold_b) {
    reserve_mib_ = mib < 0 ? 0 : mib;
    if (reserve_mib_ > start_mib_)
        reserve_mib_ = std::max(start_mib_, std::min(reserve_mib_, std::max<int64_t>(0, free_b - hold_b) >> 20));
    budget_ = usage + std::max<int64_t>(0, free_b - reserve_bytes());
}

int64_t VramArbiter::room(int64_t free_b, int64_t usage) const {
    if (lent_mib() == 0) return std::max<int64_t>(0, free_b - reserve_bytes());
    return std::max<int64_t>(0, std::min(budget_ - usage, free_b - (start_mib_ << 20)));
}

int64_t VramArbiter::kv_keep(int64_t mapped, int64_t short_by, int64_t gran) const {
    const int64_t g = std::max<int64_t>(gran, 1);
    const int64_t t = std::max<int64_t>(0, (mapped - short_by) / g * g);
    return lending_ ? boundary_below(t) : t;
}

int64_t VramArbiter::regrow_to(int64_t mapped, int64_t free_b, int64_t usage) const {
    const int64_t r = lent_mib() == 0 ? free_b - reserve_bytes() : room(free_b, usage);
    return r <= 0 ? mapped : mapped + r;
}

int64_t VramArbiter::lend_floor(int64_t floor_slots, int64_t kv_growth) const {
    const int64_t f = bytes_of(floor_slots);
    return kv_growth > 0 ? boundary_above(f) + kv_growth : f;
}

int64_t VramArbiter::kv_hold(int64_t mapped, int64_t floor_slots, int64_t kv_growth) const {
    return kv_growth > 0 ? std::max<int64_t>(0, lend_floor(floor_slots, kv_growth) - mapped) : 0;
}

VramArbiter::Plan VramArbiter::plan(int64_t reserve_mib, int64_t mapped, int64_t free_b, int64_t floor_slots,
                                    int64_t kv_growth) const {
    Plan p;
    const int64_t want_free = reserve_mib << 20;
    if (free_b < want_free) {
        const int64_t floor_b = lend_floor(floor_slots, kv_growth);
        p.kind = Plan::shrink;
        p.bytes = mapped - (want_free - free_b);
        if (p.bytes < floor_b) {
            p.bytes = floor_b;
            p.capped = true;
        }
    } else if (mapped < full_bytes()) {
        p.kind = Plan::grow;
        p.bytes = mapped + (free_b - want_free);
    }
    return p;
}

int64_t VramArbiter::reserve_max_mib(int64_t mapped, int64_t free_b, int64_t floor_slots, int64_t kv_growth) const {
    const int64_t kept = kept_by_shrink(lend_floor(floor_slots, kv_growth), mapped);
    const int64_t freed = std::max<int64_t>(0, mapped - kept);
    return std::max<int64_t>(0, free_b + freed - kv_hold(kept, floor_slots, kv_growth)) >> 20;
}

int64_t VramArbiter::reach_from(int64_t kept, int64_t kv_growth) const {
    return slots_within(boundary_below(std::max<int64_t>(0, kept - kv_growth)));
}

}  // namespace basalt::program::serve
