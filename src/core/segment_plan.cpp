// src/core/segment_plan.cpp - see include/basalt/core/segment_plan.hpp.
#include "basalt/core/segment_plan.hpp"

#include <algorithm>

namespace basalt::core {

SegmentPlan plan_segments(uint64_t want, uint64_t gran, int64_t seg_req, int64_t tail_req) {
    SegmentPlan p;
    if (gran == 0) return p;
    const uint64_t g = gran;
    const uint64_t total = (want + g - 1) / g * g;
    const uint64_t seg = seg_req > 0 ? ((uint64_t) seg_req + g - 1) / g * g : g;
    const uint64_t tail = std::min<uint64_t>(total, (uint64_t) std::max<int64_t>(tail_req, 0) / g * g);
    const uint64_t head_end = total - tail;
    const uint64_t head_seg = seg_req > 0 ? seg : std::max<uint64_t>(g, (1ull << 30) / g * g);
    for (uint64_t at = 0; at < total;) {
        const uint64_t n = at < head_end ? std::min(head_seg, head_end - at) : std::min(g, total - at);
        p.off.push_back((int64_t) at);
        p.size.push_back((int64_t) n);
        at += n;
    }
    p.total = (int64_t) total;
    p.head_end = (int64_t) head_end;
    p.seg = (int64_t) seg;
    return p;
}

}  // namespace basalt::core
