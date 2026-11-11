"""Gives back the system commit that Windows charges for GPU memory, in the processes of the voice engines (STT, TTS, avatar).

Under WDDM every GPU allocation (cudaMalloc and the like) is charged its full size to the process commit when it is created: the video memory
manager keeps committed, write-combined private regions (PAGE_READWRITE | PAGE_WRITECOMBINE) to evict the allocation into, and leaves them
untouched while the allocation stays in VRAM. Decommitting such a region frees that much of the commit limit (RAM + page file). A region is
released only when none of its pages is in RAM, and judged once, so content an eviction wrote is never discarded; `restore()` commits the
released regions again. The price, as for the engine (docs/WINDOWS.md): an allocation evicted afterwards has nowhere to go and its process
can hang, so `TADAMOO_KEEP_VRAM_COMMIT=1` (or `"keep_vram_commit": true` in voice.json) keeps the commit as it is.

This module imports only the standard library: the helpers run as scripts in their own Python environments. Nothing happens off Windows."""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Dict, Iterable, Iterator, Optional

BACKING_PROTECT = 0x04 | 0x400          # PAGE_READWRITE | PAGE_WRITECOMBINE
MEM_COMMIT, MEM_PRIVATE = 0x1000, 0x20000
MEM_DECOMMIT = 0x4000
KEEP_ENV = "TADAMOO_KEEP_VRAM_COMMIT"
BATCH_PAGES = 16384


class Region:
    """What VirtualQuery says about the run of pages at `base`."""

    def __init__(self, base: int, alloc_base: int, size: int, state: int, protect: int, alloc_protect: int, kind: int) -> None:
        self.base, self.alloc_base, self.size = base, alloc_base, size
        self.state, self.protect, self.alloc_protect, self.kind = state, protect, alloc_protect, kind


class WindowsApi:
    """The few Windows calls, in one place so that a test can stand in for them."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD),
                        ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

        class WSEX(ctypes.Structure):
            _fields_ = [("VirtualAddress", ctypes.c_void_p), ("VirtualAttributes", ctypes.c_size_t)]

        self.MBI, self.WSEX = MBI, WSEX
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.query_fn = k32.VirtualQuery
        self.query_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        self.query_fn.restype = ctypes.c_size_t
        self.free_fn = k32.VirtualFree
        self.free_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        self.free_fn.restype = wintypes.BOOL
        self.alloc_fn = k32.VirtualAlloc
        self.alloc_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        self.alloc_fn.restype = ctypes.c_void_p
        self.ws_fn = ctypes.WinDLL("psapi", use_last_error=True).QueryWorkingSetEx
        self.ws_fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        self.ws_fn.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        self.process = k32.GetCurrentProcess()
        self.page = 4096

    def region(self, addr: int) -> Optional[Region]:
        m = self.MBI()
        if self.query_fn(addr, self.ct.byref(m), self.ct.sizeof(m)) != self.ct.sizeof(m):
            return None
        return Region(m.BaseAddress or 0, m.AllocationBase or 0, m.RegionSize, m.State, m.Protect, m.AllocationProtect, m.Type)

    def any_page_in_ram(self, base: int, size: int) -> bool:
        """True when a page of the range is in the working set, or when that cannot be told (the region is then kept)."""
        pages = size // self.page
        for first in range(0, pages, BATCH_PAGES):
            n = min(BATCH_PAGES, pages - first)
            rows = (self.WSEX * n)()
            for i in range(n):
                rows[i].VirtualAddress = base + (first + i) * self.page
            if not self.ws_fn(self.process, rows, self.ct.sizeof(rows)):
                return True
            if any(r.VirtualAttributes & 1 for r in rows):
                return True
        return False

    def decommit(self, base: int, size: int) -> bool:
        return bool(self.free_fn(base, size, MEM_DECOMMIT))

    def commit(self, base: int, size: int) -> bool:
        return bool(self.alloc_fn(base, size, MEM_COMMIT, BACKING_PROTECT))


class Backing:
    def __init__(self, api: Any) -> None:
        self.api = api
        self.lock = threading.Lock()
        self.released: Dict[int, int] = {}       # base -> size of the regions a sweep decommitted
        self.judged: set = set()                 # allocation bases a sweep has already looked at

    def release(self) -> Dict[str, int]:
        """Decommits the backing of the GPU allocations made since the last sweep: {released_bytes, released_regions, kept_bytes, total_released}."""
        out = {"released_bytes": 0, "released_regions": 0, "kept_bytes": 0, "total_released": 0}
        with self.lock:
            alive: set = set()
            addr = 0
            while True:
                r = self.api.region(addr)
                if r is None:
                    break
                addr = r.base + r.size
                if r.kind != MEM_PRIVATE or r.alloc_protect != BACKING_PROTECT:
                    continue
                alive.add(r.alloc_base)
                if r.state != MEM_COMMIT or r.protect != BACKING_PROTECT or r.base != r.alloc_base:
                    continue
                if r.alloc_base in self.judged:
                    if r.alloc_base not in self.released:
                        continue
                    del self.released[r.alloc_base]          # released and committed again by someone else: a new allocation at the same address
                self.judged.add(r.alloc_base)
                if not self.api.any_page_in_ram(r.base, r.size) and self.api.decommit(r.base, r.size):
                    self.released[r.base] = r.size
                    out["released_bytes"] += r.size
                    out["released_regions"] += 1
                else:
                    out["kept_bytes"] += r.size
            self.judged &= alive
            self.released = {b: s for b, s in self.released.items() if b in alive}
            out["total_released"] = sum(self.released.values())
        return out

    def restore(self) -> Dict[str, int]:
        """Commits every released region again: {restored_bytes, refused_bytes}."""
        done = refused = 0
        with self.lock:
            left: Dict[int, int] = {}
            for base, size in self.released.items():
                r = self.api.region(base)
                if r is None or r.alloc_base != base or r.kind != MEM_PRIVATE or r.state == MEM_COMMIT:
                    continue
                if self.api.commit(base, size):
                    done += size
                else:
                    refused += size
                    left[base] = size
            self.released = left
        return {"restored_bytes": done, "refused_bytes": refused}


class _Never:
    def region(self, addr: int) -> None:
        return None


_backing: Optional[Backing] = None
_lock = threading.Lock()


def enabled() -> bool:
    return os.name == "nt" and os.environ.get(KEEP_ENV, "").strip().lower() not in ("1", "true", "yes")


def _get() -> Backing:
    global _backing
    with _lock:
        if _backing is None:
            try:
                _backing = Backing(WindowsApi())
            except Exception as e:                       # no ctypes, no psapi: nothing to give back
                sys.stderr.write(f"vram_backing: not available ({type(e).__name__}: {e})\n")
                _backing = Backing(_Never())
        return _backing


def release() -> Dict[str, int]:
    """One sweep (see `Backing.release`); an empty answer when the commit is kept or this is not Windows. Never raises."""
    if not enabled():
        return {}
    try:
        got = _get().release()
    except Exception as e:
        sys.stderr.write(f"vram_backing: sweep failed ({type(e).__name__}: {e})\n")
        return {}
    if got["released_bytes"] or got["kept_bytes"]:
        kept = f", {got['kept_bytes'] / 1048576:.0f} MiB kept (in RAM)" if got["kept_bytes"] else ""
        sys.stderr.write(f"vram_backing: {got['released_bytes'] / 1048576:.0f} MiB of commit given back in {got['released_regions']} regions{kept}\n")
        sys.stderr.flush()
    return got


def restore() -> Dict[str, int]:
    """Commits the released regions again (before the machine sleeps, or when the commit is needed back)."""
    if not enabled() or _backing is None:
        return {}
    try:
        return _backing.restore()
    except Exception as e:
        sys.stderr.write(f"vram_backing: restore failed ({type(e).__name__}: {e})\n")
        return {}


def start_sweeper(every: float = 5.0) -> None:
    """For a process whose work does not go through one input loop (the avatar worker): a thread that sweeps every `every` seconds."""
    if not enabled():
        return

    def run() -> None:
        import time
        while True:
            release()
            time.sleep(every)
    threading.Thread(target=run, name="vram-backing", daemon=True).start()


def release_between(lines: Iterable[Any]) -> Iterator[Any]:
    """The lines of a helper's input, with a sweep before the first and after each one is handled: what a load or a synthesis allocated is given back
    while the helper waits for the next request."""
    release()
    for line in lines:
        yield line
        release()
