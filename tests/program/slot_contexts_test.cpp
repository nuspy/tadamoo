// tests/program/slot_contexts_test.cpp - the batch slots' contexts with a side slot (--side-context).
//
//   1. a side slot smaller than the others: the last slot holds the side cells, the rest the full context;
//   2. a side context larger than the context is clamped to it;
//   3. one slot or none has no side slot (batch 1: the side context is ignored);
//   4. the batch cap of 8 slots: the side slot is slot 7;
//   5. the last slot not fitting drops the side slot first, and only when every requested slot fit does it stay;
//   6. checkpoint slots count one slot less with a side slot;
//   7. a copy into or out of a slot is refused past the slot's cells.
#include "basalt/program/slot_contexts.hpp"

#include <cstdint>
#include <cstdio>
#include <vector>

namespace sc = basalt::program::slot_contexts;

namespace {
int g_fail = 0;
void check(bool ok, const char* what) {
    std::printf("  %-70s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}
}  // namespace

int main() {
    std::printf("slot_contexts_test\n");
    {
        const std::vector<int64_t> cap = sc::plan(4, 65536, 30000);
        check(cap.size() == 4 && cap[0] == 65536 && cap[1] == 65536 && cap[2] == 65536 && cap[3] == 30000,
              "batch 4: three full slots and a 30000-cell side slot last");
        check(sc::side_index(4, 30000) == 3, "the side slot is the last index");
    }
    {
        const std::vector<int64_t> cap = sc::plan(3, 8192, 30000);
        check(cap[2] == 8192, "a side context above the context is the context");
    }
    {
        const std::vector<int64_t> cap = sc::plan(3, 8192, 0);
        check(cap[0] == 8192 && cap[1] == 8192 && cap[2] == 8192 && sc::side_index(3, 0) == -1,
              "side 0: every slot is full, no side slot");
    }
    {
        const std::vector<int64_t> one = sc::plan(1, 8192, 4096);
        check(one.size() == 1 && one[0] == 8192 && sc::side_index(1, 4096) == -1,
              "batch 1: no side slot, the slot keeps the context");
        check(sc::plan(0, 8192, 4096).empty() && sc::side_index(0, 4096) == -1, "batch 0: nothing");
        check(sc::side_index(2, 4096) == 1 && sc::plan(2, 8192, 4096)[1] == 4096, "batch 2: one main slot and the side slot");
    }
    {
        const std::vector<int64_t> cap = sc::plan(8, 32768, 12000);
        check(cap.size() == 8 && cap[6] == 32768 && cap[7] == 12000, "batch 8: the side slot is slot 7");
    }
    {
        check(sc::side_after_fit(4, 4, 30000) == 3, "all four slots fit: the side slot stays");
        check(sc::side_after_fit(4, 3, 30000) == -1, "the last slot did not fit: the side slot is the one dropped");
        check(sc::side_after_fit(4, 2, 30000) == -1, "two slots fit: no side slot");
        check(sc::side_after_fit(4, 4, 0) == -1, "no side asked: none");
    }
    {
        check(sc::checkpoint_slots(4, 30000) == 3, "checkpoint slots: all but the side slot");
        check(sc::checkpoint_slots(4, 0) == 4, "checkpoint slots without a side slot: all");
        check(sc::checkpoint_slots(1, 30000) == 1, "checkpoint slots at batch 1: the one slot");
    }
    {
        check(sc::copy_fits(30000, 30000) && !sc::copy_fits(30001, 30000), "a copy fits up to the slot's cells");
        check(!sc::copy_fits(-1, 30000), "a negative length is refused");
    }
    std::printf(g_fail ? "FAIL\n" : "PASS\n");
    return g_fail ? 1 : 0;
}
