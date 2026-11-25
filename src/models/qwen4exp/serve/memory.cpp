// src/models/qwen4exp/serve/memory.cpp - memory operations for the persistent engine.
#include "basalt/platform/vram_backing.hpp"
#include "basalt/models/qwen4exp/shared_names.hpp"
#include "qfn_state.hpp"

#include "basalt/core/host_plan.hpp"

namespace basalt::models::qwen4exp::serve {

// the prompt path's loan with `live` slots: its chunk as large as fits, and the first slot it covers
int32_t QfnRuntime::loan_plan(int64_t live, int64_t& chunk) {
    PfPart& p = pf_parts[0];
    auto fits = [&](int64_t c) {
        const int64_t k = part_slots(p, c);
        return k > 0 && k + 128 <= live && !(o.prefill_auto && k * 100 > auto_lend_pct * live);
    };
    // auto: the largest chunk on the start-up's 256-token grid, so a few slots lent away cost a few tokens
    // of chunk, not half of it
    int64_t c = chunk_full;
    if (o.prefill_auto && !fits(c)) {
        c = engine_local::biggest_chunk(chunk_full, fits);
        if (c == 0) c = 256;
    }
    while (c > 256 && !fits(c)) c = std::max<int64_t>(256, c / 2 / 256 * 256);
    chunk = c;
    return (int32_t) std::max<int64_t>(0, live - part_slots(p, c));
}

void QfnRuntime::relend() {   // the prompt path's loan: the end of the slots left, its chunk as large as fits
    if (pf_parts.empty() || pf_parts[0].first < 0) return;
    PfPart& p = pf_parts[0];
    int64_t c = 0;
    p.first = loan_plan(xcache.slots(), c);
    o.prefill_chunk = c;
    p.first_now = -1;   // laid out again at the next loan
}

// The resident RAM mode holds only the experts no slot holds, so an expert in a slot about to go has no RAM
// copy: it is copied from its slot into a RAM home first and read from there like the compact copy's.  Refused
// (nothing copied) when the RAM above the floor cannot take them.  `live`: the slots that stay.
bool QfnRuntime::home_given_up(int64_t live, std::string& e, int64_t reach_from) {
    if (!src.complement_ready()) return true;
    // the slots the loan will overwrite keep a RAM copy too, so a prompt never costs a file read, and so do the
    // slots the elastic KV may take during a lend (from `reach_from`)
    int64_t from = live;
    if (!pf_parts.empty() && pf_parts[0].first >= 0) {
        int64_t c = 0;
        from = std::min<int64_t>(live, loan_plan(live, c));
    }
    from = std::min(from, reach_from);
    std::vector<std::pair<int32_t, int32_t>> give;
    std::vector<int32_t> slots;
    for (size_t i = 0; i < host_res.size(); ++i) {
        const int64_t layer = (int64_t) i / g.n_expert, ex = (int64_t) i % g.n_expert;
        if (host_res[i] >= from && !src.has_resident(layer, ex)) {
            give.emplace_back((int32_t) layer, (int32_t) ex);
            slots.push_back(host_res[i]);
        }
    }
    if (give.empty()) return true;
    std::vector<uint8_t*> dst;
    if (!src.homes_take(give, e, dst)) return false;
    for (size_t k = 0; k < give.size(); ++k) {
        const size_t bytes = (size_t) basalt::kernels::cpu::expert_layout().blob_bytes(give[k].first);
        if (cudaMemcpyAsync(dst[k], xcache.device_slot(slots[k]), bytes, cudaMemcpyDeviceToHost, 0) != cudaSuccess) {
            e = std::string("copying an expert to RAM failed: ") + cudaGetErrorString(cudaGetLastError());
            (void) cudaDeviceSynchronize();   // queued copies stop writing before their regions are freed
            (void) cudaGetLastError();
            src.homes_abort();
            return false;
        }
    }
    if (cudaDeviceSynchronize() != cudaSuccess) {
        e = std::string("copying the experts to RAM failed: ") + cudaGetErrorString(cudaGetLastError());
        (void) cudaDeviceSynchronize();
        (void) cudaGetLastError();
        src.homes_abort();
        return false;
    }
    src.homes_commit();
    basalt::log::info("basalt serve", "VRAM: %zu experts moved from the cache to RAM (%.0f MiB held)", give.size(),
                 (double) src.homes_bytes() / 1048576.0);
    return true;
}

// Windows: the commit WDDM charges for every GPU allocation is given back after each command, once more has been
// allocated since the last sweep
void QfnRuntime::vram_commit_sweep() {
    if (!o.release_vram_commit || !basalt::platform::vram_backing_supported()) return;
    const basalt::platform::BackingSweep sweep = basalt::platform::release_vram_backing();
    if (sweep.released_bytes >= (64ull << 20))
        basalt::log::info("basalt serve", "VRAM commit: %.2f GiB more given back (%.2f GiB in all)",
                          (double) sweep.released_bytes / 1073741824.0, (double) sweep.total_released / 1073741824.0);
}

// the cache is backed less than it was opened with (the start's shortfall, an elastic KV's tail) and no VRAM is lent
// out: the idle loop keeps looking for room
bool QfnRuntime::regrow_pending() {
    return !regrow_failed && !peer.valid() && arbiter.lending() && arbiter.lent_mib() == 0 && xcache.segmented() &&
           xcache.mapped_bytes() < xcache.full_bytes();
}

// between requests: the VRAM another program gave up on its own (a model it unloaded) is mapped into the cache again,
// into the room the arbiter counts (the free VRAM above the reserve), each new slot filled from the RAM copy.  The room
// must last kRegrowSettleSeconds first: a function that unloads and loads again within that time finds its VRAM free
// and the cache does not have to give it back.
void QfnRuntime::regrow_idle() {
    constexpr auto kRegrowSettle = std::chrono::seconds(45);
    if (!regrow_pending()) return;
    size_t free_b = 0, total_b = 0;
    cudaMemGetInfo(&free_b, &total_b);
    if (arbiter.room((int64_t) free_b, vram_usage()) < (64ll << 20)) {
        regrow_room_since = {};
        return;
    }
    const auto now = std::chrono::steady_clock::now();
    if (regrow_room_since == std::chrono::steady_clock::time_point{}) regrow_room_since = now;
    if (now - regrow_room_since < kRegrowSettle) return;
    regrow_room_since = {};    std::string e;
    apply_pending(true);                     // the adaptive tier's swaps in flight land first
    if (!xq_drain()) e = "an adaptive swap failed";
    const int64_t before = xcache.slots();
    if (e.empty() && cache_regrow(e) && xcache.slots() > before) {
        cudaMemGetInfo(&free_b, &total_b);
        basalt::log::info("basalt serve", "the expert cache grew by %lld slots into the VRAM that freed: %lld of %lld slots "
                          "backed, %.2f GiB free", (long long) (xcache.slots() - before), (long long) xcache.slots(),
                          (long long) xcache.full_slots(), (double) free_b / 1073741824.0);
    }
    if (!e.empty()) {
        regrow_failed = true;
        basalt::log::warn("basalt serve", "the expert cache could not grow into the free VRAM: %s", e.c_str());
    }
}

// the owner of the cache's backed size, before anything shrinks or grows it: the layout, the start's reserve and
// slots, and whether VRAM may lend
void QfnRuntime::init_arbiter() {
    std::vector<int64_t> slot_end((size_t) std::max<int64_t>(xcache.full_slots(), 0));
    for (size_t i = 0; i < slot_end.size(); ++i) slot_end[i] = xcache.bytes_of((int64_t) i + 1);
    arbiter.start(xcache.segment_ends(), std::move(slot_end), o.vram_reserve_mib, o.vram_elastic && xcache.segmented());
}

// `for_sleep` (SLEEP and WAKE): the cache goes to its smallest size and comes back to the same placement, with no
// request between the two, so neither --vram-elastic (any segmented cache does) nor a peer card's tier stands in
// the way.  The arbiter keeps the reserve in force: a VRAM command sets it, SLEEP leaves it, WAKE grows back to it.
bool QfnRuntime::vram_command(const std::string& cmd, std::string& e, bool for_sleep, std::string* reply) {
    // `VRAM` alone: the reserve the engine started with (--vram-reserve-mib), which returns a lend; WAKE's: the one
    // in force before the SLEEP
    char* end = nullptr;
    const bool bare = cmd.find_first_not_of(' ', 4) == std::string::npos;
    const long long reserve = bare ? (long long) (for_sleep ? arbiter.reserve_mib() : arbiter.start_reserve_mib())
                                   : std::strtoll(cmd.c_str() + 4, &end, 10);
    if (!bare && (end == cmd.c_str() + 4 || reserve < 0)) { e = "expected: VRAM [reserve_mib]"; return false; }
    if (reserve < vram_floor_mib() && !for_sleep) {
        e = "VRAM: a reserve below " + std::to_string(vram_floor_mib()) + " MiB is too small for what is allocated "
            "after loading; the smallest safe value is " + std::to_string(vram_floor_mib());
        return false;
    }
    if ((!o.vram_elastic && !for_sleep) || !xcache.segmented()) {
        e = for_sleep ? "SLEEP needs a segmented expert cache (an elastic KV cache or --vram-elastic)"
                      : "VRAM needs an engine started with --vram-elastic (one NVIDIA GPU, --serve)";
        return false;
    }
    if (peer.valid() && !for_sleep) { e = "VRAM: not with a second GPU as expert tier"; return false; }
    for (const PfPart& p : pf_parts)
        if (!p.lent.empty()) { e = "VRAM: a prompt's loan is still out"; return false; }
    if (!ver.wait_commit(e)) return false;
    apply_pending(true);                     // the adaptive tier's swaps in flight land first
    if (!xq_drain()) { e = "VRAM: an adaptive swap failed"; return false; }
    if (cudaDeviceSynchronize() != cudaSuccess) {
        e = std::string("VRAM: ") + cudaGetErrorString(cudaGetLastError());
        return false;
    }
    const auto t0 = Clock::now();
    size_t free_b = 0, total_b = 0;
    cudaMemGetInfo(&free_b, &total_b);
    const int64_t mapped = xcache.mapped_bytes();
    const int64_t before = xcache.slots();
    // what stays at least: the prompt path's smallest loan (256 tokens) and the 128 slots it must leave; with the
    // elastic KV, also its floor and what the KV still needs to reach its maximum cells (not for SLEEP: nothing runs
    // until WAKE)
    int64_t floor_slots = std::min<int64_t>(xcache.full_slots(),
        128 + (pf_parts.empty() || pf_parts[0].first < 0 ? 0 : part_slots(pf_parts[0], 256)));
    const int64_t kv_growth_now = kv_growth_bytes(), kv_growth = for_sleep ? 0 : kv_growth_now;
    if (kv_growth_now > 0) floor_slots = std::max(floor_slots, cache_floor_slots());
    const VramArbiter::Plan plan = arbiter.plan(reserve, mapped, (int64_t) free_b, floor_slots, kv_growth);
    const int64_t reserve_max = arbiter.reserve_max_mib(mapped, (int64_t) free_b, floor_slots, kv_growth);
    std::string note;
    if (plan.kind == VramArbiter::Plan::shrink) {
        const int64_t keep = plan.bytes;
        if (plan.capped)
            note = kv_growth > 0 ? " (the cache keeps the prompt path's buffers and the " + std::to_string(kv_growth >> 20) +
                                       " MiB the KV needs to reach its maximum context; at most " +
                                       std::to_string(reserve_max) + " MiB can be kept free now)"
                                 : std::string(" (the cache keeps its smallest size: the prompt path's buffers)");
        const int64_t reach = kv_growth > 0 ? arbiter.reach_from(arbiter.kept_by_shrink(keep, mapped), kv_growth)
                                            : INT64_MAX;
        if (!home_given_up(xcache.slots_after_shrink(keep), e, reach)) { e = "VRAM: " + e + " (nothing changed)"; return false; }
        // the homes are committed by now: whatever the shrink left or took is followed from the cache itself
        const bool shrunk = xcache.shrink(keep, e);
        const int64_t live = xcache.slots();
        for (size_t i = 0; i < host_res.size(); ++i)
            if (host_res[i] >= live) {
                vram_evicted.emplace_back((int32_t) i, host_res[i]);
                host_res[i] = basalt::core::kNotResident;
            }
        res_upload();
        if (!shrunk) return false;
    } else if (plan.kind == VramArbiter::Plan::grow) {
        std::string gerr;
        if (!xcache.grow(plan.bytes, gerr)) note = " (" + gerr + ")";
        const int64_t live = xcache.slots();
        std::string ferr;
        std::vector<std::pair<int32_t, int32_t>> keep_out;
        struct Fill { int32_t slot; const uint8_t* b; int64_t bytes; };
        std::vector<Fill> fills;
        for (const auto& [i, slot] : vram_evicted) {
            if (slot >= live) { keep_out.emplace_back(i, slot); continue; }
            const int64_t layer = i / g.n_expert;
            int64_t pick = i;
            if (host_res[(size_t) i] >= 0) {   // back already (the adaptive tier): the layer's most-routed miss
                pick = -1;
                float best = -1.0f;
                for (int64_t ex = 0; ex < g.n_expert; ++ex) {
                    const size_t j = (size_t) (layer * g.n_expert + ex);
                    if (host_res[j] >= 0 || (src.complement_ready() && !src.has_resident(layer, ex))) continue;
                    const float u = drive.d.usage.empty() ? 0.0f : drive.d.usage[j];
                    if (u > best) { best = u; pick = (int64_t) j; }
                }
            }
            if (pick < 0) continue;
            const uint8_t* b = srcp->blob(layer, pick % g.n_expert);
            if (b == nullptr) {
                e = "VRAM: refilling the cache failed: an expert has no RAM copy";
                return false;
            }
            fills.push_back({slot, b, (int64_t) basalt::kernels::cpu::expert_layout().blob_bytes(layer)});
            host_res[(size_t) pick] = slot;
        }
        // after a SLEEP the RAM copy is registered again in address order (WAKE): the copies follow it
        const bool repinning = src.complement_unpinned();
        if (repinning)
            std::sort(fills.begin(), fills.end(),
                      [](const Fill& x, const Fill& y) { return std::less<const uint8_t*>()(x.b, y.b); });
        for (const Fill& f : fills) {
            if (repinning && !src.repin_wait(f.b, (uint64_t) f.bytes)) {
                e = "VRAM: the RAM copy could not be registered again";
                return false;
            }
            if (!xcache.fill_slot_queued(f.slot, f.b, ferr, f.bytes)) {
                e = "VRAM: refilling the cache failed: " + ferr;
                return false;
            }
        }
        vram_evicted.swap(keep_out);
        if (!xcache.sync_queued(ferr)) { e = "VRAM: " + ferr; return false; }
        res_upload();
        relend();
        // the slots hold the experts again: every expert in a slot below the loan has its RAM home freed (the
        // copy is the slots' now: those given up, those the loan range kept, and those the adaptive tier moved
        // back).  The loan's own slots keep theirs: a prompt overwrites them and reads those experts from RAM.
        if (src.complement_ready()) {
            const int64_t loan_from = pf_parts.empty() || pf_parts[0].first < 0 ? INT64_MAX : pf_parts[0].first;
            // the slots the KV may take while VRAM stays lent keep theirs too
            const int64_t keep_from = kv_growth > 0 && reserve > arbiter.start_reserve_mib()
                ? std::min(loan_from, arbiter.reach_from(xcache.mapped_bytes(), kv_growth)) : loan_from;
            size_t released = 0;
            for (size_t j = 0; j < host_res.size(); ++j)
                if (host_res[j] >= 0 && host_res[j] < keep_from &&
                    src.home_release((int64_t) j / g.n_expert, (int64_t) j % g.n_expert))
                    ++released;
            const uint64_t freed = src.homes_reclaim() + src.homes_compact();
            basalt::log::info("basalt serve", "VRAM: %zu RAM homes released, %.0f MiB freed, %.0f MiB kept for the "
                              "prompt loan (slots from %lld)", released, (double) freed / 1048576.0,
                              (double) src.homes_bytes() / 1048576.0, (long long) std::min<int64_t>(loan_from, live));
        }
    }
    relend();
    cudaMemGetInfo(&free_b, &total_b);
    if (!for_sleep || bare)   // not SLEEP's: the reserve granted, which leaves the KV what it needs beyond the cache
        arbiter.set_reserve_mib(reserve, (int64_t) free_b, vram_usage(),
                                arbiter.kv_hold(xcache.mapped_bytes(), floor_slots, kv_growth_now));
    const bool capped = plan.capped || (reserve > arbiter.start_reserve_mib() && arbiter.reserve_mib() < reserve);
    basalt::log::info("basalt serve", "VRAM %lld MiB kept free: the expert cache %lld -> %lld of %lld slots "
                         "(%.2f of %.2f GiB), %lld MiB free now, prompt chunk %lld, in %.0f ms%s",
                 reserve, (long long) before, (long long) xcache.slots(), (long long) xcache.full_slots(),
                 (double) xcache.mapped_bytes() / 1073741824.0, (double) xcache.full_bytes() / 1073741824.0,
                 (long long) (free_b >> 20), (long long) o.prefill_chunk,
                 std::chrono::duration<double, std::milli>(Clock::now() - t0).count(), note.c_str());
    char line[512];
    std::snprintf(line, sizeof line, "VRAM reserve_mib=%lld expert_slots=%lld expert_slots_full=%lld "
                  "expert_cache_mib=%lld expert_cache_full_mib=%lld vram_free_mib=%lld prompt_chunk=%lld "
                  "ram_homes_mib=%lld lent_mib=%lld expert_slots_start=%lld kv_growth_mib=%lld reserve_max_mib=%lld "
                  "capped=%d\n", reserve, (long long) gpu_slots(), (long long) xcache.full_slots(),
                  (long long) (xcache.mapped_bytes() >> 20), (long long) (xcache.full_bytes() >> 20),
                  (long long) (free_b >> 20), (long long) o.prefill_chunk, (long long) (src.homes_bytes() >> 20),
                  (long long) arbiter.lent_mib(), (long long) arbiter.slots_at_start(), (long long) (kv_growth >> 20),
                  (long long) reserve_max, capped ? 1 : 0);
    if (reply != nullptr) *reply = line;
    else std::fputs(line, stdout);
    return true;
}

int QfnRuntime::init_memory() {
    // the load's own buffers and the RAM held for buffers nothing made are gone: RAM still free keeps the next-hottest
    // experts the copy left out
    if (const uint64_t held = basalt::core::host_plan_release(); held > 0)
        basalt::log::info("basalt serve", "RAM: %.2f GiB held for host buffers not made, given back",
                          (double) held / 1073741824.0);
    if (srcp == &src && src.complement_ready() && o.resident_budget == 0) {
        int64_t n_top = 0;
        const uint64_t add = src.top_up(stages.empty() ? profile : profile_all, [&](int32_t l, int32_t e) {
            return host_res[(size_t) l * g.n_expert + (size_t) e] >= 0 || (peer.valid() && peer.has(l, e));
        }, o.resident_pin, n_top);
        if (add > 0) {
            basalt::platform::watch_resident_ram(true, src.resident_bytes() + add);
            basalt::log::info("basalt generate", "RAM top-up: +%.2f GiB, %lld experts (total %.2f GiB)",
                         (double) add / 1073741824.0, (long long) n_top,
                         (double) (src.resident_bytes() + add) / 1073741824.0);
        }
    }
    report_expert_tiers();
    // from here the RAM guard watches: below its floor the engine sheds and new requests wait, and a running
    // request stops only once the RAM is critical.  The checkpoints stay through a shed: a live request's rewind and
    // the deferred copy in flight use them.
    start_guard();

    // a VRAM shrink's RAM homes stop 256 MiB above the reserve
    src.set_homes_reserve(256ull << 20);
    if (const uint64_t avail = basalt::platform::available_memory(); avail > 0)
        basalt::log::info("basalt serve", "RAM %.2f GiB free, guard %.2f GiB", (double) avail / 1073741824.0,
                          (double) guard_floor / 1073741824.0);
    return 0;
}

}  // namespace basalt::models::qwen4exp::serve
