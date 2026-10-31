// The batch slots' contexts when the last slot is the side slot (--side-context): a short session beside the
// conversations' slots, kept for the harness's short technical calls.  The arithmetic is here so
// slot_contexts_test.cpp can walk it on the CPU; generate.cpp calls it where it carves the slot sessions.
//
//   * every slot holds `max_context` cells, except the side slot, which holds min(side, max_context);
//   * the side slot is the LAST slot, so a slot that does not fit (dropped from the end) is the side slot first;
//   * it needs a slot besides it: with one slot (or none) there is no side slot;
//   * the side slot keeps no conversation checkpoints, so the host RAM kept for checkpoints counts one slot less.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace basalt::program::slot_contexts {

/// The side slot's index among `batch` slots, or -1: `side` cells are asked for and a slot exists besides it.
inline int side_index(int batch, int64_t side) { return side > 0 && batch >= 2 ? batch - 1 : -1; }

/// Each slot's context in cells.
inline std::vector<int64_t> plan(int batch, int64_t max_context, int64_t side) {
    std::vector<int64_t> cap((size_t) std::max(batch, 0), max_context);
    const int s = side_index(batch, side);
    if (s >= 0) cap[(size_t) s] = std::min(side, max_context);
    return cap;
}

/// The side slot's index once `fit` of the `requested` slots could be carved: it survives only when all of them fit.
inline int side_after_fit(int requested, int fit, int64_t side) {
    return fit == requested ? side_index(requested, side) : -1;
}

/// The slots that keep conversation checkpoints in RAM.
inline int checkpoint_slots(int batch, int64_t side) { return batch - (side_index(batch, side) >= 0 ? 1 : 0); }

/// Whether `cells` of a conversation can be copied to or from a slot of `cap` cells.
inline bool copy_fits(int64_t cells, int64_t cap) { return cells >= 0 && cells <= cap; }

}  // namespace basalt::program::slot_contexts
