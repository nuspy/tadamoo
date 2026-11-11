// tests/program/vram_arbiter_test.cpp - the expert cache's backed size: the segment layouts, the elastic KV's and the
// lends' targets.  The sequences run on a model of the card (the cache's segments, the sessions' KV pages, a program
// that borrows VRAM) through the same arbiter the engine calls: a KV growth to the maximum context never fails while
// VRAM is lent to a borrower that keeps to its share, a fit never maps lent VRAM, the cache never goes below its floor,
// and a return brings the cache back to its size at start.  Without a lend every decision is the old formula's.
#include "vram_arbiter.hpp"
#include "basalt/core/segment_plan.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <vector>

using namespace basalt::program::serve;
using basalt::core::SegmentPlan;
using basalt::core::plan_segments;

static int g_fail = 0;
#define CHECK(c)                                                                         \
    do {                                                                                 \
        if (!(c)) { std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++g_fail; } \
    } while (0)

namespace {

constexpr int64_t MiB = 1ll << 20, GiB = 1ll << 30, kGran = 2 * MiB;

uint64_t g_rng = 0;
uint64_t next_u64() {   // splitmix64: the same sequence on every compiler
    uint64_t z = (g_rng += 0x9e3779b97f4a7c15ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return z ^ (z >> 31);
}
int64_t below(int64_t n) { return n <= 0 ? 0 : (int64_t) (next_u64() % (uint64_t) n); }

// the layout ExpertCache::open_segmented built before the plan: uniform segments, or large ones then a granule tail
SegmentPlan legacy_layout(uint64_t want, uint64_t g, int64_t seg_req, int64_t tail_req) {
    SegmentPlan p;
    const uint64_t total = (want + g - 1) / g * g;
    const uint64_t seg = seg_req > 0 ? ((uint64_t) seg_req + g - 1) / g * g : g;
    const uint64_t tail = std::min<uint64_t>(total, (uint64_t) std::max<int64_t>(tail_req, 0) / g * g);
    const uint64_t head_end = seg_req > 0 ? 0 : total - tail;
    const uint64_t head_seg = std::max<uint64_t>(g, (1ull << 30) / g * g);
    for (uint64_t at = 0; at < total;) {
        const uint64_t n = at < head_end ? std::min(head_seg, head_end - at) : std::min(seg, total - at);
        p.off.push_back((int64_t) at);
        p.size.push_back((int64_t) n);
        at += n;
    }
    p.total = (int64_t) total;
    return p;
}

std::vector<int64_t> ends_of(const SegmentPlan& p) {
    std::vector<int64_t> e(p.off.size());
    for (size_t i = 0; i < e.size(); ++i) e[i] = p.off[i] + p.size[i];
    return e;
}

// slots of 1.6 and 1.8 MiB in turn, each 256-byte aligned as open_sized lays them
std::vector<int64_t> sized_slots(int64_t n) {
    std::vector<int64_t> end((size_t) n);
    int64_t at = 0;
    for (int64_t i = 0; i < n; ++i) {
        const int64_t b = (i % 2 ? 1887437 : 1677722);
        at += (b + 255) / 256 * 256;
        end[(size_t) i] = at;
    }
    return end;
}

// ExpertCache's backed prefix of segments, with its shrink and grow rules
struct FakeCache {
    SegmentPlan p;
    std::vector<int64_t> slot_end;
    int64_t mapped_segs = 0;
    int64_t mapped() const { return mapped_segs > 0 ? p.off[(size_t) mapped_segs - 1] + p.size[(size_t) mapped_segs - 1] : 0; }
    int64_t slots_within(int64_t b) const {
        return (int64_t) (std::upper_bound(slot_end.begin(), slot_end.end(), b) - slot_end.begin());
    }
    int64_t slots() const { return slots_within(mapped()); }
    int64_t keep_segs(int64_t keep) const {
        int64_t k = 0;
        while (k < (int64_t) p.off.size() && p.off[(size_t) k] < keep) ++k;
        return k;
    }
    int64_t slots_after_shrink(int64_t keep) const {
        const int64_t k = keep_segs(keep);
        if (k >= mapped_segs) return slots();
        return slots_within(k > 0 ? p.off[(size_t) k - 1] + p.size[(size_t) k - 1] : 0);
    }
    void shrink(int64_t keep) { mapped_segs = std::min(mapped_segs, keep_segs(keep)); }
    void grow(int64_t want, int64_t free_b) {
        while (mapped_segs < (int64_t) p.off.size() &&
               p.off[(size_t) mapped_segs] + p.size[(size_t) mapped_segs] <= want &&
               p.size[(size_t) mapped_segs] <= free_b) {
            free_b -= p.size[(size_t) mapped_segs];
            ++mapped_segs;
        }
    }
};

// one elastic session: its KV pages back `cells` (2 MiB pages over `cell_bytes` per cell)
struct Session {
    int64_t base, max, cell_bytes, cells;
    int64_t bytes_for(int64_t c) const { return (c * cell_bytes + kGran - 1) / kGran * kGran; }
    int64_t growth() const { return bytes_for(max) - bytes_for(cells); }
};

// the card: everything else fixed, the cache, the KV past the bases, and the borrower's allocation
struct Card {
    FakeCache cache;
    std::vector<Session> ss;
    VramArbiter arb;
    int64_t total = 0, fixed = 0, kv = 0, ext = 0, floor_slots = 0;
    int64_t refused = 0, map_failed = 0, overdrawn = 0;
    int64_t free_b() const { return total - fixed - cache.mapped() - kv - ext; }
    int64_t usage() const { return cache.mapped() + kv; }
    int64_t kv_growth() const {
        int64_t g = 0;
        for (const Session& s : ss) g += s.growth();
        return g;
    }
    // Runtime::kv_fit's decisions without the parking: the room first, then whole segments down to the floor
    bool fit(size_t i, int64_t cells) {
        Session& s = ss[i];
        cells = std::min(std::max(cells, s.base), s.max);
        const int64_t add = s.bytes_for(cells) - s.bytes_for(s.cells);
        if (add <= 0) return true;
        const int64_t room = arb.room(free_b(), usage());
        if (add > room) {
            const int64_t keep = arb.kv_keep(cache.mapped(), add - room, kGran);
            if (cache.slots_after_shrink(keep) < floor_slots) { ++refused; return false; }
            const int64_t before = cache.mapped();
            cache.shrink(keep);
            if (arb.lent_mib() > 0 && before - cache.mapped() + room < add) ++overdrawn;
        }
        if (free_b() < add) { ++map_failed; return false; }
        kv += add;
        s.cells = cells;
        return true;
    }
    void regrow() {
        if (arb.room(free_b(), usage()) > 0) cache.grow(arb.regrow_to(cache.mapped(), free_b(), usage()), free_b());
    }
    void release(size_t i, int64_t cells) {
        Session& s = ss[i];
        cells = std::min(std::max(cells, s.base), s.max);
        if (cells >= s.cells) return;
        kv -= s.bytes_for(s.cells) - s.bytes_for(cells);
        s.cells = cells;
        regrow();
    }
    // a VRAM command, then a borrower that takes what it was promised and is free
    VramArbiter::Plan vram(int64_t reserve_mib, bool kv_term = true) {
        ext = 0;
        const VramArbiter::Plan pl = arb.plan(reserve_mib, cache.mapped(), free_b(), floor_slots, kv_term ? kv_growth() : 0);
        if (pl.kind == VramArbiter::Plan::shrink) cache.shrink(pl.bytes);
        else if (pl.kind == VramArbiter::Plan::grow) cache.grow(pl.bytes, free_b());
        arb.set_reserve_mib(reserve_mib, free_b(), usage(),
                            kv_term ? arb.kv_hold(cache.mapped(), floor_slots, kv_growth()) : 0);
        ext = std::max<int64_t>(0, std::min(free_b() - (arb.start_reserve_mib() << 20), arb.lent_mib() << 20));
        return pl;
    }
};

Card make_card(int64_t slots, int64_t reserve_mib) {
    Card c;
    c.ss = {{32768, 262144, 12 * 1024, 32768}, {4096, 65536, 12 * 1024, 4096}};
    int64_t tail = 0;
    for (const Session& s : c.ss) tail += s.bytes_for(s.max) - s.bytes_for(s.base);
    c.cache.slot_end = sized_slots(slots);
    c.cache.p = plan_segments((uint64_t) c.cache.slot_end.back(), kGran, 512 * MiB, tail);
    c.cache.mapped_segs = (int64_t) c.cache.p.off.size();
    c.fixed = 6 * GiB;
    c.total = c.fixed + c.cache.mapped() + (reserve_mib << 20);
    c.floor_slots = 400;
    c.arb.start(ends_of(c.cache.p), c.cache.slot_end, reserve_mib, true);
    return c;
}

void test_layouts() {
    // the two layouts the engine built before keep every segment where it was
    for (const int64_t want : {(int64_t) 20 * GiB + 12345, (int64_t) 3 * GiB, (int64_t) 7 * MiB}) {
        for (const auto& [seg, tail] : {std::pair<int64_t, int64_t>{512 * MiB, 0}, {64 * MiB, 0}, {0, 2 * GiB + 3},
                                        {0, 5 * MiB}, {0, 50 * GiB}}) {
            const SegmentPlan a = plan_segments((uint64_t) want, kGran, seg, tail);
            const SegmentPlan b = legacy_layout((uint64_t) want, kGran, seg, tail);
            CHECK(a.off == b.off && a.size == b.size && a.total == b.total);
        }
    }
    // both: segments of the asked size up to the tail, granules over it
    const SegmentPlan p = plan_segments((uint64_t) (20 * GiB + 12345), kGran, 512 * MiB, 3 * GiB + 5);
    CHECK(p.total == (20 * GiB + 12345 + kGran - 1) / kGran * kGran);
    CHECK(p.head_end == p.total - 3 * GiB);
    int64_t at = 0;
    for (size_t i = 0; i < p.off.size(); ++i) {
        CHECK(p.off[i] == at);
        CHECK(p.size[i] == (p.off[i] < p.head_end ? std::min<int64_t>(512 * MiB, p.head_end - p.off[i]) : kGran));
        at += p.size[i];
    }
    CHECK(at == p.total);
    CHECK(plan_segments(100, 0, 0, 0).off.empty());
}

void test_geometry() {
    VramArbiter a;
    const std::vector<int64_t> seg = {8 * MiB, 16 * MiB, 18 * MiB, 20 * MiB};
    std::vector<int64_t> slot;
    for (int64_t i = 1; i <= 20; ++i) slot.push_back(i * MiB);
    a.start(seg, slot, 700, true);
    CHECK(a.slots_at_start() == 20 && a.full_bytes() == 20 * MiB);
    CHECK(a.slots_within(0) == 0 && a.slots_within(MiB - 1) == 0 && a.slots_within(3 * MiB + 5) == 3);
    CHECK(a.slots_within(99 * MiB) == 20 && a.bytes_of(0) == 0 && a.bytes_of(5) == 5 * MiB && a.bytes_of(99) == 20 * MiB);
    CHECK(a.boundary_below(7 * MiB) == 0 && a.boundary_below(8 * MiB) == 8 * MiB && a.boundary_below(17 * MiB) == 16 * MiB);
    CHECK(a.boundary_above(7 * MiB) == 8 * MiB && a.boundary_above(16 * MiB) == 16 * MiB && a.boundary_above(99 * MiB) == 20 * MiB);
    // shrink keeps the segment the target falls in, never past what is backed
    CHECK(a.kept_by_shrink(9 * MiB, 20 * MiB) == 16 * MiB && a.kept_by_shrink(0, 20 * MiB) == 0);
    CHECK(a.kept_by_shrink(17 * MiB, 16 * MiB) == 16 * MiB);
    // the reserve in force and what is lent
    CHECK(a.reserve_mib() == 700 && a.lent_mib() == 0);
    // while VRAM is lent the room is what the engine freed itself, whether or not the borrower has allocated yet
    a.set_reserve_mib(6144, 6144 * MiB + 5, 100 * MiB, 0);
    CHECK(a.lent_mib() == 6144 - 700 && a.room(6144 * MiB + 5, 100 * MiB) == 5 && a.room(MiB, 100 * MiB) == 0);
    CHECK(a.room(700 * MiB + 5, 100 * MiB) == 5 && a.room(700 * MiB + 15, 90 * MiB) == 15);
    CHECK(a.room(700 * MiB + 2, 90 * MiB) == 2 && a.regrow_to(50 * MiB, 700 * MiB + 15, 90 * MiB) == 50 * MiB + 15);
    a.set_reserve_mib(300, 0, 0, 0);
    CHECK(a.lent_mib() == 0 && a.reserve_bytes() == 300 * MiB && a.room(301 * MiB, 0) == MiB);
    // a cache without segments cannot lend
    VramArbiter one;
    one.start({}, slot, 700, true);
    CHECK(!one.lending() && one.kept_by_shrink(3 * MiB, 20 * MiB) == 20 * MiB);
}

// without --vram-elastic every target is the formula the engine used before the arbiter; with it, a KV shrink ends on
// a segment boundary
void test_default_identity() {
    Card c = make_card(12000, 700);
    VramArbiter a;
    a.start(ends_of(c.cache.p), c.cache.slot_end, 700, false);
    CHECK(!a.lending() && c.arb.lending());
    for (int k = 0; k < 20000; ++k) {
        const int64_t mapped = below(c.cache.p.total + 1), short_by = below(4 * GiB) - GiB / 2;
        const int64_t free_b = below(8 * GiB);
        const int64_t legacy = std::max<int64_t>(0, (mapped - short_by) / kGran * kGran);
        CHECK(a.kv_keep(mapped, short_by, kGran) == legacy);
        const int64_t lend_keep = c.arb.kv_keep(mapped, short_by, kGran);
        CHECK(lend_keep == c.arb.boundary_below(legacy) && c.arb.kept_by_shrink(lend_keep, c.cache.p.total) == lend_keep);
        const int64_t room = free_b - (700ll << 20);
        const int64_t usage = below(30 * GiB);
        CHECK(a.room(free_b, usage) == std::max<int64_t>(0, room));
        CHECK(a.regrow_to(mapped, free_b, usage) == (room <= 0 ? mapped : mapped + room));
        // the VRAM command without the elastic KV
        const int64_t reserve = 128 + below(16000), fs = below(800);
        const VramArbiter::Plan p = a.plan(reserve, mapped, free_b, fs, 0);
        const int64_t want = reserve << 20;
        if (free_b < want) {
            const int64_t keep = mapped - (want - free_b), floor_b = a.bytes_of(fs);
            CHECK(p.kind == VramArbiter::Plan::shrink && p.bytes == std::max(keep, floor_b) && p.capped == (keep < floor_b));
        } else if (mapped < a.full_bytes()) {
            CHECK(p.kind == VramArbiter::Plan::grow && p.bytes == mapped + (free_b - want));
        } else {
            CHECK(p.kind == VramArbiter::Plan::none);
        }
    }
}

// a lend that would take the KV's reach is granted down to the floor, and the KV then grows to the maximum
void test_policy() {
    Card c = make_card(12000, 700);
    const int64_t full_slots = c.cache.slots(), growth = c.kv_growth();
    CHECK(growth > 0 && full_slots == 12000);
    const int64_t floor_b = c.arb.lend_floor(c.floor_slots, growth);
    CHECK(floor_b == c.arb.boundary_above(c.arb.bytes_of(c.floor_slots)) + growth);
    const int64_t max_mib = c.arb.reserve_max_mib(c.cache.mapped(), c.free_b(), c.floor_slots, growth);
    // within the maximum: granted, the KV still reaches its maximum context
    VramArbiter::Plan p = c.vram(max_mib - 600);
    CHECK(p.kind == VramArbiter::Plan::shrink && !p.capped);
    CHECK(c.free_b() + c.ext >= ((max_mib - 600 - 512) << 20));   // a shrink lends at most a segment less
    CHECK(c.fit(0, 262144) && c.fit(1, 65536) && c.refused == 0 && c.map_failed == 0 && c.overdrawn == 0);
    CHECK(c.cache.slots() >= c.floor_slots && c.ext <= (c.arb.lent_mib() << 20) &&
          c.ext + 512 * MiB >= (c.arb.lent_mib() << 20));
    // beyond it: capped at the floor, said in the plan; the KV again reaches its maximum
    c.release(0, 0);
    c.release(1, 0);
    p = c.vram(max_mib + 4096);
    CHECK(p.kind == VramArbiter::Plan::shrink && p.capped && p.bytes == floor_b);
    CHECK(c.cache.mapped() >= floor_b && c.cache.mapped() < floor_b + 512 * MiB);
    CHECK(c.fit(0, 262144) && c.fit(1, 65536) && c.refused == 0 && c.map_failed == 0 && c.overdrawn == 0);
    CHECK(c.cache.slots() >= c.floor_slots);
    // the return: the borrower frees, the KV gives its cells back, the cache is whole again
    c.vram(700);
    c.release(0, 0);
    c.release(1, 0);
    CHECK(c.arb.lent_mib() == 0 && c.cache.slots() == full_slots && c.free_b() == (700ll << 20));
    // a borrower that takes every byte above the start's reserve, past its share: the KV still grows to its maximum,
    // out of the cache alone
    c.vram(max_mib + 4096);
    c.ext += c.free_b() - (700ll << 20);
    CHECK(c.fit(0, 262144) && c.fit(1, 65536) && c.refused == 0 && c.map_failed == 0);
    CHECK(c.free_b() >= (700ll << 20) && c.cache.slots() >= c.floor_slots);
    // one that takes the start's reserve too: a fit may be refused, but never maps VRAM the cache did not give back
    c.vram(700);
    c.release(0, 0);
    c.release(1, 0);
    CHECK(c.cache.slots() == full_slots);
    c.vram(max_mib + 4096);
    c.ext += c.free_b();
    c.fit(0, 262144);
    c.fit(1, 65536);
    CHECK(c.map_failed == 0 && c.free_b() >= 0 && c.cache.slots() >= c.floor_slots);
    c.refused = 0;
    c.vram(700);
    c.release(0, 0);
    c.release(1, 0);
    CHECK(c.cache.slots() == full_slots);
    // without the KV's term the same lend leaves the KV short: the request would be refused
    Card d = make_card(12000, 700);
    d.vram(max_mib + 4096, false);
    CHECK(!(d.fit(0, 262144) && d.fit(1, 65536)) && d.refused + d.map_failed > 0);
}

// lend, return, fit and release in any order: no failure for a borrower that keeps to its share, no fit that maps
// lent VRAM, never below the floor; the end state is the start's
void test_random_sequences() {
    for (uint64_t seed = 1; seed <= 12; ++seed) {
        g_rng = seed * 1000003ull;
        Card c = make_card(6000 + below(6000), 300 + below(1200));
        const int64_t start_slots = c.cache.slots(), start_free = c.free_b();
        const int64_t start_mib = c.arb.start_reserve_mib();
        for (int step = 0; step < 2000; ++step) {
            const int op = (int) below(10);
            const size_t i = (size_t) below((int64_t) c.ss.size());
            const int64_t slots_before = c.cache.slots();
            if (op < 4) {
                CHECK(c.fit(i, c.ss[i].cells + below(c.ss[i].max)));
            } else if (op < 7) {
                c.release(i, c.ss[i].base + below(c.ss[i].max));
            } else if (op < 9) {
                const int64_t mx = c.arb.reserve_max_mib(c.cache.mapped(), c.free_b() + c.ext, c.floor_slots, c.kv_growth());
                const VramArbiter::Plan p = c.vram(start_mib + below(std::max<int64_t>(1, mx - start_mib + 2048)));
                if (p.kind == VramArbiter::Plan::shrink) CHECK(c.cache.slots() >= std::min(slots_before, c.floor_slots));
            } else {
                c.vram(start_mib);
            }
            CHECK(c.free_b() >= 0);
            CHECK(c.cache.slots() >= std::min(slots_before, c.floor_slots));
            CHECK(c.arb.lent_mib() == std::max<int64_t>(0, c.arb.reserve_mib() - start_mib));
            CHECK(c.ext <= (c.arb.lent_mib() << 20));
        }
        if (c.refused + c.map_failed + c.overdrawn > 0)
            std::printf("seed %llu: refused %lld, map failed %lld, overdrawn %lld\n", (unsigned long long) seed,
                        (long long) c.refused, (long long) c.map_failed, (long long) c.overdrawn);
        CHECK(c.refused == 0 && c.map_failed == 0 && c.overdrawn == 0);
        for (size_t i = 0; i < c.ss.size(); ++i) c.release(i, 0);
        c.vram(start_mib);
        CHECK(c.cache.slots() == start_slots && c.free_b() == start_free && c.kv == 0);
    }
}

}  // namespace

int main() {
    test_layouts();
    test_geometry();
    test_default_identity();
    test_policy();
    test_random_sequences();
    if (g_fail == 0) std::printf("vram_arbiter_test: all passed\n");
    return g_fail == 0 ? 0 : 1;
}
