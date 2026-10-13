"""The plan's RAM for the prompt path's lend region (loader.plan): with resident experts the engine also keeps the
lent cache slots' experts in RAM, as far as RAM allows down to its own margin. The first real load took 6.1 GiB of
RAM above a plan that left them out. They are shown (elastic: the engine keeps what fits) and are not held against the
share kept free; a memory record measured against the older plan is not added on top."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from serve import loader

GiB = 1 << 30
ROOMY = {"ram_total": 128 * GiB, "ram_available": 116 * GiB, "commit_limit": 144 * GiB, "commit_used": 14 * GiB,
         "vram_total": 32 * GiB, "vram_free": 30 * GiB}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name})
        self.env.start()
        self.values = loader.safe(loader.DEMO_FACTS, ROOMY)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def plan(self, system=ROOMY, **changes):
        return loader.plan({**self.values, **changes}, loader.DEMO_FACTS, system, correction={"ram": 0, "commit": 0})


class LendRegion(Isolated):
    def item(self, p):
        return next((i["bytes"] for i in p["ram"]["items"] if i["key"] == "lend"), 0)

    def test_with_room_the_whole_region(self):
        p = self.plan()
        lend = self.item(p)
        self.assertEqual(lend, min(32768, p["slot_context"]) * loader.LEND_BYTES_PER_TOKEN)
        self.assertEqual(p["ram"]["elastic"], lend)
        self.assertTrue(p["fits"])
        self.assertEqual(p["ram"]["need"], sum(i["bytes"] for i in p["ram"]["items"]))

    def test_a_smaller_chunk_lends_less(self):
        self.assertLess(self.item(self.plan(**{"engine.prefill": "auto:8192"})), self.item(self.plan()))

    def test_tight_ram_keeps_the_engine_margin_and_still_fits(self):
        whole = self.item(self.plan())
        base = self.plan(**{"engine.resident-experts": True})
        before = sum(i["bytes"] for i in base["ram"]["items"] if i["key"] != "lend")   # the PLE table in RAM included
        margin = max(2 * GiB, ROOMY["ram_total"] * 15 // 100) + loader.ENGINE_LATE_HOST
        tight = {**ROOMY, "ram_available": before + margin + 3 * GiB}       # room for 3 GiB of copies only
        p = self.plan(tight)
        self.assertEqual(self.item(p), 3 * GiB)
        self.assertLess(self.item(p), whole)
        # elastic: the copies end at the engine's margin, and the rest stays within the share kept free
        self.assertTrue(p["fits"], p["problems"])
        self.assertGreater(p["ram"]["need"] - p["ram"]["elastic"] + p["ram"]["keep"], 0)

    def test_no_copies_without_resident_experts_or_without_room(self):
        self.assertEqual(self.item(self.plan(**{"engine.resident-experts": False})), 0)
        full = {**ROOMY, "ram_available": 20 * GiB}
        self.assertEqual(self.item(self.plan(full)), 0)


class OldMeasurements(Isolated):
    def write(self, record):
        facts = {**loader.DEMO_FACTS, "meta_hash": "0123456789abcdef"}
        path = loader.calibration_path(facts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": {}, "memory": record}), encoding="utf-8")
        loader._memory_cache.clear()
        return facts

    def test_a_record_against_the_older_plan_is_not_added(self):
        facts = self.write({"ram_extra": 6 * GiB, "commit_extra": 7 * GiB})
        self.assertEqual(loader.memory_correction(facts), {"ram": 0, "commit": 0})
        facts = self.write({"plan_version": loader.MEMORY_PLAN_VERSION, "ram_extra": GiB, "commit_extra": 2 * GiB})
        self.assertEqual(loader.memory_correction(facts), {"ram": GiB, "commit": 2 * GiB})

    def test_a_new_record_says_its_plan_version(self):
        facts = {**loader.DEMO_FACTS, "meta_hash": "fedcba9876543210"}
        after = {**ROOMY, "ram_available": ROOMY["ram_available"] - 60 * GiB, "commit_used": ROOMY["commit_used"] + 80 * GiB}
        rec = loader.record_load_memory(facts, self.values, ROOMY, after)
        self.assertEqual(rec["plan_version"], loader.MEMORY_PLAN_VERSION)


if __name__ == "__main__":
    unittest.main()
