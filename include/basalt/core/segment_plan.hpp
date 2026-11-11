// include/basalt/core/segment_plan.hpp - how a segmented expert cache cuts its arena into physical segments.
// Host arithmetic only, so a layout can be checked without a device.
#pragma once

#include <cstdint>
#include <vector>

namespace basalt::core {

/// The arena `[0, total)` as segments: `off[i]`, `size[i]` (ascending, back to back).  `head_end`: where the
/// granule-sized elastic tail starts (`total` when there is none).  `seg`: the segment size asked for, rounded to the
/// granularity (the granularity when none was asked).
struct SegmentPlan {
    std::vector<int64_t> off, size;
    int64_t total = 0, head_end = 0, seg = 0;
};

/// `want` bytes rounded up to `gran`; head segments of `seg_req` (rounded up to `gran`), or of 1 GiB when it is 0, up
/// to the last `tail_req` bytes (rounded down to `gran`), which are segments of `gran`.  Empty when `gran` is 0.
SegmentPlan plan_segments(uint64_t want, uint64_t gran, int64_t seg_req, int64_t tail_req);

}  // namespace basalt::core
