"""The commit Windows charges for GPU memory is given back in the voice processes: which regions are released, that content an eviction wrote is never
discarded, that a region is judged once, the restore, the opt-out, and that every helper process sweeps. The Windows calls are stood in for."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "callbridge" / "engines"
spec = importlib.util.spec_from_file_location("vram_backing_under_test", ENGINES / "vram_backing.py")
vb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vb)

MIB = 1 << 20
BACKING = vb.BACKING_PROTECT


class FakeApi:
    """An address space as a list of regions; `in_ram` names the bases that have pages in the working set."""

    def __init__(self, regions, in_ram=()):
        self.regions = {r.base: r for r in regions}
        self.in_ram = set(in_ram)
        self.refuse_commit = False
        self.decommitted, self.committed = [], []

    def region(self, addr):
        later = [r for r in self.regions.values() if r.base >= addr]
        return min(later, key=lambda r: r.base) if later else None

    def any_page_in_ram(self, base, size):
        return base in self.in_ram

    def decommit(self, base, size):
        self.regions[base].state = 0x2000                     # MEM_RESERVE
        self.decommitted.append(base)
        return True

    def commit(self, base, size):
        if self.refuse_commit:
            return False
        self.regions[base].state = vb.MEM_COMMIT
        self.committed.append(base)
        return True


def reg(base, size=64 * MIB, protect=BACKING, kind=vb.MEM_PRIVATE, state=vb.MEM_COMMIT, alloc_base=None, alloc_protect=None):
    return vb.Region(base, base if alloc_base is None else alloc_base, size, state, protect, protect if alloc_protect is None else alloc_protect, kind)


def test_a_backing_region_with_no_page_in_ram_is_decommitted_and_counted():
    api = FakeApi([reg(0x10000000), reg(0x20000000)])
    got = vb.Backing(api).release()
    assert (got["released_bytes"], got["released_regions"], got["kept_bytes"]) == (128 * MIB, 2, 0)
    assert api.decommitted == [0x10000000, 0x20000000]


def test_only_the_backing_regions_are_touched():
    other = [reg(0x30000000, protect=0x04),                                   # ordinary read-write memory
             reg(0x40000000, kind=0x40000),                                   # a mapped file
             reg(0x50000000, state=0x2000),                                   # reserved, not committed
             reg(0x60000000 + 64 * MIB, alloc_base=0x60000000)]                # the middle of an allocation, not its start
    api = FakeApi(other)
    assert vb.Backing(api).release()["released_bytes"] == 0 and api.decommitted == []


def test_a_region_with_pages_in_ram_is_kept_because_an_eviction_wrote_there():
    api = FakeApi([reg(0x10000000), reg(0x20000000)], in_ram={0x10000000})
    got = vb.Backing(api).release()
    assert (got["released_bytes"], got["kept_bytes"]) == (64 * MIB, 64 * MIB)
    assert api.decommitted == [0x20000000]


def test_a_region_is_judged_once_so_a_later_sweep_does_not_release_what_was_kept():
    api = FakeApi([reg(0x10000000)], in_ram={0x10000000})
    b = vb.Backing(api)
    b.release()
    api.in_ram.clear()                                                         # the OS paged the evicted content out afterwards
    assert b.release()["released_bytes"] == 0 and api.decommitted == []


def test_a_new_allocation_at_a_released_address_is_judged_afresh():
    api = FakeApi([reg(0x10000000)])
    b = vb.Backing(api)
    assert b.release()["released_bytes"] == 64 * MIB
    api.regions[0x10000000].state = vb.MEM_COMMIT                              # the driver reuses the address: committed again
    assert b.release()["released_bytes"] == 64 * MIB


def test_restore_commits_the_released_regions_again_and_reports_what_the_limit_refused():
    api = FakeApi([reg(0x10000000), reg(0x20000000)])
    b = vb.Backing(api)
    b.release()
    api.refuse_commit = True
    assert b.restore() == {"restored_bytes": 0, "refused_bytes": 128 * MIB}
    api.refuse_commit = False
    assert b.restore() == {"restored_bytes": 128 * MIB, "refused_bytes": 0}
    assert b.release()["released_bytes"] == 0                                  # restored regions stay judged: no later sweep releases them again


def test_freed_allocations_are_forgotten_and_not_restored():
    api = FakeApi([reg(0x10000000)])
    b = vb.Backing(api)
    b.release()
    del api.regions[0x10000000]                                                # cudaFree
    assert b.release()["total_released"] == 0
    assert b.restore() == {"restored_bytes": 0, "refused_bytes": 0}


def test_the_opt_out_and_other_systems_do_nothing(monkeypatch):
    monkeypatch.setattr(vb, "_get", lambda: pytest.fail("the sweep must not run"))
    monkeypatch.setenv(vb.KEEP_ENV, "1")
    assert vb.release() == {} and vb.restore() == {}
    monkeypatch.delenv(vb.KEEP_ENV)
    monkeypatch.setattr(vb.os, "name", "posix")
    assert vb.release() == {}


def test_release_between_sweeps_before_the_first_line_and_after_each(monkeypatch):
    calls = []
    monkeypatch.setattr(vb, "release", lambda: calls.append(len(seen)) or {})
    seen = []
    for line in vb.release_between(["a", "b"]):
        seen.append(line)
    assert calls == [0, 1, 2]                                                  # before a; after a; after b


def test_every_voice_process_sweeps():
    for name in ("chatterbox", "cosyvoice", "dia", "kokoro", "orpheus", "qwen3tts", "xtts"):
        text = (ENGINES / f"{name}_tts_helper.py").read_text(encoding="utf-8")
        assert "import vram_backing" in text and "vram_backing.release_between(sys.stdin)" in text, name
    stt = (ENGINES / "dictation_stt_helper.py").read_text(encoding="utf-8")
    assert "vram_backing.release()" in stt
    worker = (ENGINES.parents[1] / "avatar" / "avatar_worker.py").read_text(encoding="utf-8")
    assert "mod.start_sweeper()" in worker and "_start_vram_backing()\n    engine = Engine()" in worker


def test_voice_json_can_keep_the_commit(tmp_path, monkeypatch):
    from voice import service
    monkeypatch.delenv("TADAMOO_KEEP_VRAM_COMMIT", raising=False)
    path = tmp_path / "voice.json"
    path.write_text('{"keep_vram_commit": true}', encoding="utf-8")
    service._keep_vram_commit(str(path))
    assert os.environ["TADAMOO_KEEP_VRAM_COMMIT"] == "1"
    monkeypatch.delenv("TADAMOO_KEEP_VRAM_COMMIT")
    path.write_text("{}", encoding="utf-8")
    service._keep_vram_commit(str(path))
    service._keep_vram_commit(str(tmp_path / "missing.json"))
    assert "TADAMOO_KEEP_VRAM_COMMIT" not in os.environ
