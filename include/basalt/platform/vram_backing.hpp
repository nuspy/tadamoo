// include/basalt/platform/vram_backing.hpp - give back the system commit WDDM charges for GPU memory.
//
// Under WDDM every device allocation is charged its full size to the process commit when it is created: the video
// memory manager keeps committed, write-combined private regions to evict the allocation into, and leaves them
// untouched while the allocation stays in VRAM.  Decommitting them frees that much of the commit limit (RAM + page
// file).  The price: an allocation evicted afterwards (another program's GPU allocation while the engine idles, a
// sleep) has nowhere to go, and the engine can hang.  Opt-in (--release-vram-commit); nothing happens off Windows.
#pragma once

#include <cstdint>
#include <string>

namespace basalt::platform {

struct BackingSweep {
    uint64_t released_bytes = 0;     ///< decommitted by this sweep
    uint64_t released_regions = 0;
    uint64_t kept_bytes = 0;         ///< new backing regions with pages in RAM (evicted content): left committed
    uint64_t total_released = 0;     ///< every region released and still decommitted
};

/// Decommits the eviction backing of the GPU allocations made since the last sweep: committed private regions with
/// PAGE_READWRITE | PAGE_WRITECOMBINE none of whose pages is in the working set.  A region is judged once, on the
/// first sweep that sees it, so content an eviction wrote and the OS paged out later is never discarded.
BackingSweep release_vram_backing();

/// Commits every released region again.  Returns the bytes committed; `refused` gets those the commit limit refused.
uint64_t restore_vram_backing(uint64_t* refused = nullptr);

/// Restores the backing when the machine is about to sleep (VRAM is saved into it); what was restored stays
/// committed after the resume.  False with `why` when the notification is not available.
bool restore_vram_backing_on_suspend(std::string& why);

/// Whether this platform has a backing to release.
bool vram_backing_supported();

}  // namespace basalt::platform
