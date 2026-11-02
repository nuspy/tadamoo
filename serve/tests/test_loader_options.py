"""The Loader's memory, cache, context and server options of the original engine: validation, the config file the
engine and server read, the plan's RAM and VRAM accounting, saved configurations and the GPU names."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import config, hardware, loader, loader_api, profiles, settings, supervisor  # noqa: E402
from serve.engine.flags import flag_value  # noqa: E402
from serve.tests.test_loader import make_service  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GiB, MiB = 1 << 30, 1 << 20
ROOMY = {"ram_total": 128 * GiB, "ram_available": 120 * GiB, "commit_limit": 144 * GiB, "commit_used": 10 * GiB,
         "vram_total": 32 * GiB, "vram_free": 31 * GiB}
F = loader.DEMO_FACTS
BASE = {"engine.max-context": 65536, "engine.kv": "int8", "engine.spec": 0, "engine.expert-cache": 2000, "server.side-slot": 0,
        "engine.resident-experts": True}
NEW = {"engine.kv-resident": 40960, "engine.resident-budget-gib": 24.5, "engine.adapt-swaps": 0,
       "engine.prefill-cpu-share": 0.25, "engine.vram-elastic": True, "engine.keep-vram-commit": True,
       "engine.expert-profile-save": "profiles/learned.json", "engine.expert-profile-save-every": 5,
       "engine.prompt-cache": 3, "engine.prompt-cache-every": 8192, "engine.prompt-cache-root": 0,
       "engine.conversation-cache-mib": 2048, "engine.conversation-cache-slots": 2,
       "engine.conversation-cache-min-free-mib": 4096, "engine.rope-scaling": "yarn", "engine.rope-scale": 2,
       "engine.rope-freq-base": 1000000, "server.grammar": False, "server.reasoning-budget-tokens": 4096}


def item(p, bar, key):
    return next((i["bytes"] for i in p[bar]["items"] if i["key"] == key), 0)


class Validation(unittest.TestCase):
    def test_accepted(self):
        ok = [("engine", "kv-resident", 0, 0), ("engine", "kv-resident", 20480, 20480), ("engine", "kv-resident", "40000", 40000),
              ("engine", "resident-budget-gib", "24.5", 24.5), ("engine", "resident-budget-gib", 8, 8),
              ("engine", "mmap-experts", True, True), ("engine", "adapt-swaps", 0, 0), ("engine", "adapt-swaps", 96, 96),
              ("engine", "prefill-cpu-share", 0, 0), ("engine", "prefill-cpu-share", "0,6", 0.6),
              ("engine", "prefill-cpu-share", 1.0, 1), ("engine", "vram-elastic", False, False),
              ("engine", "keep-vram-commit", True, True), ("engine", "expert-profile-save", " p/x.json ", "p/x.json"),
              ("engine", "expert-profile-save-every", 0, 0), ("engine", "expert-profile-save-every", 2.5, 2.5),
              ("engine", "prompt-cache", 0, 0), ("engine", "prompt-cache-every", 0, 0), ("engine", "prompt-cache-root", 2048, 2048),
              ("engine", "conversation-cache-mib", 0, 0), ("engine", "conversation-cache-slots", 4, 4),
              ("engine", "conversation-cache-min-free-mib", 2560, 2560),
              ("engine", "rope-scaling", "YaRN", "yarn"), ("engine", "rope-scale", 1, 1), ("engine", "rope-scale", "2.5", 2.5),
              ("engine", "rope-freq-base", 0, 0), ("engine", "rope-freq-base", 1000000, 1000000),
              ("server", "grammar", False, False), ("server", "reasoning-budget-tokens", 0, 0),
              ("server", "reasoning-budget-tokens", 2048, 2048)]
        for section, key, value, want in ok:
            self.assertEqual(settings.check_value(section, key, value), want, (key, value))

    def test_refused(self):
        bad = [("engine", "kv-resident", 1), ("engine", "kv-resident", 20479), ("engine", "kv-resident", -1),
               ("engine", "kv-resident", 30000.5), ("engine", "kv-resident", True),
               ("engine", "resident-budget-gib", 0), ("engine", "resident-budget-gib", -2),
               ("engine", "resident-budget-gib", "lots"), ("engine", "resident-budget-gib", float("nan")),
               ("engine", "mmap-experts", "yes"), ("engine", "mmap-experts", 1), ("engine", "adapt-swaps", -1),
               ("engine", "adapt-swaps", 1.5), ("engine", "prefill-cpu-share", 1.1), ("engine", "prefill-cpu-share", -0.1),
               ("engine", "prefill-cpu-share", "x"), ("engine", "vram-elastic", "on"), ("engine", "keep-vram-commit", 1),
               ("engine", "expert-profile-save", ""), ("engine", "expert-profile-save", "a\nb"),
               ("engine", "expert-profile-save", "--kv"), ("engine", "expert-profile-save", 5),
               ("engine", "expert-profile-save-every", -1), ("engine", "expert-profile-save-every", True),
               ("engine", "prompt-cache", -1), ("engine", "prompt-cache", 5000), ("engine", "prompt-cache-every", -1),
               ("engine", "prompt-cache-root", 1.5), ("engine", "conversation-cache-mib", -1),
               ("engine", "conversation-cache-slots", 100000), ("engine", "conversation-cache-min-free-mib", "x"),
               ("engine", "rope-scaling", "ntk"), ("engine", "rope-scaling", 1), ("engine", "rope-scale", 0.5),
               ("engine", "rope-scale", 0), ("engine", "rope-freq-base", 0.5), ("engine", "rope-freq-base", 1),
               ("engine", "rope-freq-base", -3), ("server", "grammar", "off"), ("server", "grammar", 0),
               ("server", "reasoning-budget-tokens", -1), ("server", "reasoning-budget-tokens", "many")]
        for section, key, value in bad:
            with self.assertRaises(ValueError, msg=(key, value)):
                settings.check_value(section, key, value)

    def test_errors_name_the_field_and_the_rule(self):
        with self.assertRaisesRegex(ValueError, r"engine\.kv-resident: 0 .* from 20480"):
            settings.check_value("engine", "kv-resident", 100)
        with self.assertRaisesRegex(ValueError, r"engine\.prefill-cpu-share: .*0\.\.1"):
            settings.check_value("engine", "prefill-cpu-share", 3)

    def test_combinations_that_cannot_go_together(self):
        with self.assertRaisesRegex(ValueError, "k8v4"):
            settings.check_combination({"kv": "k8v4", "kv-resident": 30000})
        settings.check_combination({"kv": "k8v4", "kv-resident": 0})
        with self.assertRaisesRegex(ValueError, "mmap-experts"):
            settings.check_combination({"mmap-experts": True, "resident-experts": True})
        with self.assertRaisesRegex(ValueError, "mmap-experts"):
            settings.check_combination({"mmap-experts": True, "resident-budget-gib": 8})
        settings.check_combination({"mmap-experts": True, "resident-experts": False})
        settings.check_combination({"resident-experts": True, "resident-budget-gib": 8})


class ConfigFile(unittest.TestCase):
    """The fields reach the engine and the server as a --config file does it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "basalt_config.json")
        Path(self.path).write_text(json.dumps({"server": {"port": 7232}, "engine": {"kv": "int8"}}), encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_saved_values_become_engine_words_and_server_options(self):
        values = {k: v for k, v in NEW.items()}
        settings.save(self.path, values, settings.LOADER_FIELDS)
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(raw["engine"]["kv-resident"], 40960)
        self.assertIs(raw["server"]["grammar"], False)
        a, _argv, file_words, _cli = config.parse_settings(settings._parser(), ["--config", self.path])
        self.assertFalse(a.grammar)
        self.assertEqual(a.reasoning_budget_tokens, 4096)
        for words in (["--kv-resident", "40960"], ["--resident-budget-gib", "24.5"], ["--adapt-swaps", "0"],
                      ["--prefill-cpu-share", "0.25"], ["--vram-elastic"], ["--keep-vram-commit"],
                      ["--expert-profile-save", "profiles/learned.json"], ["--expert-profile-save-every", "5"],
                      ["--prompt-cache", "3"], ["--prompt-cache-every", "8192"], ["--prompt-cache-root", "0"],
                      ["--conversation-cache-mib", "2048"], ["--conversation-cache-slots", "2"],
                      ["--conversation-cache-min-free-mib", "4096"], ["--rope-scaling", "yarn"], ["--rope-scale", "2"],
                      ["--rope-freq-base", "1000000"]):
            at = file_words.index(words[0])
            self.assertEqual(file_words[at:at + len(words)], words)
        self.assertNotIn("--grammar", file_words)                       # a server option, not an engine word

    def test_grammar_on_is_the_default_and_a_switch_off_is_not_passed(self):
        settings.save(self.path, {"server.grammar": True, "engine.mmap-experts": False}, settings.LOADER_FIELDS)
        a, argv, file_words, _ = config.parse_settings(settings._parser(), ["--config", self.path])
        self.assertTrue(a.grammar)
        self.assertNotIn("--mmap-experts", file_words)
        self.assertIn("--grammar", argv)

    def test_a_pair_the_engine_refuses_is_not_saved(self):
        for changes in ({"engine.kv": "k8v4", "engine.kv-resident": 30000},
                        {"engine.mmap-experts": True, "engine.resident-experts": True}):
            with self.assertRaises(ValueError, msg=changes):
                settings.save(self.path, changes, settings.LOADER_FIELDS)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8"))["engine"], {"kv": "int8"})

    def test_an_empty_value_removes_the_key(self):
        settings.save(self.path, {"engine.adapt-swaps": 12}, settings.LOADER_FIELDS)
        settings.save(self.path, {"engine.adapt-swaps": None}, settings.LOADER_FIELDS)
        self.assertNotIn("adapt-swaps", json.loads(Path(self.path).read_text(encoding="utf-8"))["engine"])

    def test_a_flag_the_binary_lacks_is_named_before_the_engine_starts(self):
        known = {"--kv", "--kv-resident"}
        self.assertEqual(loader_api.unsupported({"engine.kv-resident": 40960, "engine.mmap-experts": True,
                                                 "engine.adapt-swaps": None, "engine.vram-elastic": False,
                                                 "server.grammar": True}, known), ["engine.mmap-experts"])
        self.assertEqual(loader_api.unsupported({"engine.mmap-experts": True}, None), [])
        with self.assertRaisesRegex(ValueError, "--mmap-experts is not an engine flag"):
            config.check_engine_flags(["--mmap-experts"], known, "--config x")


class PlanAccounting(unittest.TestCase):
    def plan(self, **values):
        return loader.plan({**BASE, **values}, F, ROOMY, correction={"ram": 0, "commit": 0})

    def test_defaults_change_nothing(self):
        p = self.plan()
        self.assertEqual(item(p, "ram", "checkpoints"), 6 * 118 * MiB)
        self.assertEqual(p["notes"], [])
        self.assertEqual(item(p, "ram", "kv_host"), 0)
        explicit = self.plan(**{"engine.kv-resident": 0, "engine.prompt-cache": 6, "engine.conversation-cache-mib": 0})
        self.assertEqual((explicit["vram"]["need"], explicit["ram"]["need"]), (p["vram"]["need"], p["ram"]["need"]))

    def test_kv_resident_moves_the_kv_pool_from_vram_to_pinned_ram(self):
        whole, cut = self.plan(), self.plan(**{"engine.kv-resident": 30000})
        pool_cell = loader.ATTENTION_LAYERS * loader.KV_CELL_BYTES["int8"]
        saved = pool_cell * (65536 - 30000)
        self.assertEqual(item(whole, "vram", "kv") - item(cut, "vram", "kv"), saved)
        self.assertEqual(item(cut, "ram", "kv_host"), pool_cell * 65536)
        self.assertEqual([n["code"] for n in cut["notes"]], ["kv_stream"])
        self.assertEqual(cut["ram"]["need"] - whole["ram"]["need"], pool_cell * 65536)
        slots = lambda p: p["vram"]["expert_slots"]                       # noqa: E731
        auto = lambda **v: loader.plan({**BASE, "engine.expert-cache": "auto", **v}, F, ROOMY)    # noqa: E731
        self.assertGreater(slots(auto(**{"engine.kv-resident": 30000})), slots(auto()))      # the VRAM goes to experts
        self.assertEqual(item(self.plan(**{"engine.kv-resident": 20480}), "ram", "kv_host"), pool_cell * 65536)
        self.assertEqual(item(self.plan(**{"engine.kv-resident": 1}), "vram", "kv"),
                         item(self.plan(**{"engine.kv-resident": 20480}), "vram", "kv"))   # the engine's floor

    def test_kv_resident_that_fits_or_with_k8v4_streams_nothing(self):
        p = self.plan(**{"engine.kv-resident": 100000})
        self.assertEqual(item(p, "vram", "kv"), item(self.plan(), "vram", "kv"))
        self.assertEqual([n["code"] for n in p["notes"]], ["kv_fits"])
        k = self.plan(**{"engine.kv": "k8v4", "engine.kv-resident": 30000})
        self.assertEqual(item(k, "ram", "kv_host"), 0)
        self.assertEqual([n["code"] for n in k["notes"]], ["kv_k8v4"])

    def test_parallel_slots_each_keep_their_pool(self):
        p = self.plan(**{"server.parallel": 2, "server.slot-full-context": True, "engine.kv-resident": 30000})
        self.assertEqual(item(p, "ram", "kv_host"), 2 * loader.ATTENTION_LAYERS * loader.KV_CELL_BYTES["int8"] * 65536)

    def test_the_technical_session_costs_one_more_session_of_its_own_size(self):
        off, on = self.plan(), self.plan(**{"server.side-slot": 30000})
        self.assertEqual(on["side_context"], 30000)
        self.assertEqual(off["side_context"], 0)
        side_kv = loader.KV_TOKEN_BYTES["int8"] * 30000 + loader.SESSION_FIXED
        self.assertEqual(item(on, "vram", "kv") - item(off, "vram", "kv"), side_kv)
        self.assertEqual([n["code"] for n in on["notes"]], ["side"])
        self.assertEqual(on["notes"][0], {"code": "side", "context": 30000, "kv": side_kv})
        # it comes out of the expert cache, not on top of it (auto cache), and the checkpoints are the conversations' only
        auto_off = self.plan(**{"engine.expert-cache": "auto"})
        auto_on = self.plan(**{"engine.expert-cache": "auto", "server.side-slot": 30000})
        self.assertLess(auto_on["vram"]["expert_slots"], auto_off["vram"]["expert_slots"])
        self.assertEqual(item(on, "ram", "checkpoints"), item(off, "ram", "checkpoints"))
        two = self.plan(**{"server.parallel": 2, "server.side-slot": 30000})
        self.assertEqual(two["slot_context"], 32768)                              # the conversations' slots divide the context
        self.assertEqual(item(two, "vram", "kv") - item(self.plan(**{"server.parallel": 2}), "vram", "kv"), side_kv)

    def test_the_technical_session_is_never_larger_than_a_slot(self):
        p = self.plan(**{"server.parallel": 4, "server.side-slot": 30000})           # slots of 16384
        self.assertEqual(p["side_context"], 16384)
        self.assertEqual(item(p, "vram", "kv") - item(self.plan(**{"server.parallel": 4}), "vram", "kv"),
                         loader.KV_TOKEN_BYTES["int8"] * 16384 + loader.SESSION_FIXED)

    def test_the_technical_session_adds_a_draft_cache_and_turns_relaxed_drafting_off(self):
        spec = {"engine.spec": 7}
        off, on = self.plan(**spec), self.plan(**spec, **{"server.side-slot": 30000})
        cell = loader.KV_CELL_BYTES["int8"] + 128
        self.assertEqual(item(on, "vram", "drafter") - item(off, "vram", "drafter"), cell * 30000)
        rx = self.plan(**spec, **{"engine.relaxed-drafting": 0.3, "server.side-slot": 30000})["relaxed"]
        self.assertEqual((rx["effective"], rx["why"]), (False, ["side"]))
        self.assertTrue(self.plan(**spec, **{"engine.relaxed-drafting": 0.3})["relaxed"]["effective"])

    def test_the_technical_session_is_saved_and_checked(self):
        self.assertEqual(settings.check_value("server", "side-slot", 0), 0)
        self.assertEqual(settings.check_value("server", "side-slot", "30000"), 30000)
        self.assertEqual(settings.check_value("server", "side-slot", True), config.SIDE_SLOT_DEFAULT)
        for bad in (100, -1, 2.5, "x"):
            with self.assertRaises(ValueError, msg=bad):
                settings.check_value("server", "side-slot", bad)
        self.assertIn(("server", "side-slot"), settings.LOADER_FIELDS)               # persisted with the other Loader fields
        self.assertEqual(loader.effective({})["server.side-slot"], config.SIDE_SLOT_DEFAULT)      # on unless the Loader says 0
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "basalt.json")
            Path(path).write_text(json.dumps({"server": {"model": "m.basalt"}}), encoding="utf-8")
            settings.save(path, {"server.side-slot": 30000}, settings.LOADER_FIELDS)
            self.assertEqual(settings.values_of(settings.read_file(path), settings.LOADER_FIELDS)["server.side-slot"], 30000)
            ap = config.build_parser()
            a, _, file_words, _ = config.parse_settings(ap, ["--config", path])
            self.assertEqual((a.side_slot, flag_value(file_words, "--side-context")), (30000, "30000"))

    def test_resident_budget_limits_the_ram_of_the_experts(self):
        full, capped = self.plan(), self.plan(**{"engine.resident-budget-gib": 10})
        spare = loader.RESIDENT_SPARE_BLOBS * F["max_blob"]
        self.assertEqual(item(capped, "ram", "experts"), 10 * GiB + spare)
        self.assertGreater(item(full, "ram", "experts"), item(capped, "ram", "experts"))
        self.assertEqual([n["code"] for n in capped["notes"]], ["budget"])
        big = self.plan(**{"engine.resident-budget-gib": 500})
        self.assertEqual(item(big, "ram", "experts"), item(full, "ram", "experts"))     # a budget above the copy changes nothing
        only = self.plan(**{"engine.resident-experts": False, "engine.resident-budget-gib": 10})
        self.assertEqual(item(only, "ram", "experts"), 10 * GiB + spare)                # a budget makes them resident

    def test_mmap_experts_wins_over_the_resident_copy(self):
        p = self.plan(**{"engine.mmap-experts": True, "engine.resident-budget-gib": 10})
        off = self.plan(**{"engine.resident-experts": False})
        self.assertEqual(item(p, "ram", "experts"), item(off, "ram", "experts"))
        self.assertEqual(p["ram"]["need"], off["ram"]["need"])
        self.assertIn("mmap_wins", [n["code"] for n in p["notes"]])
        self.assertEqual([n["code"] for n in self.plan(**{"engine.mmap-experts": True, "engine.resident-experts": False})["notes"]], [])

    def test_the_conversation_caches_cost_ram(self):
        p = self.plan(**{"engine.prompt-cache": 10, "engine.conversation-cache-mib": 4096})
        self.assertEqual(item(p, "ram", "checkpoints"), 10 * 118 * MiB)
        self.assertEqual(item(p, "ram", "conversation"), 4096 * MiB)
        self.assertEqual(p["ram"]["need"] - self.plan()["ram"]["need"], 4 * 118 * MiB + 4096 * MiB)
        self.assertEqual([n["code"] for n in p["notes"]], ["caches"])
        self.assertEqual(item(self.plan(**{"engine.prompt-cache": 0}), "ram", "checkpoints"), 0)
        self.assertEqual(item(self.plan(**{"engine.conversation-cache-mib": 4096, "engine.conversation-cache-slots": 0}),
                              "ram", "conversation"), 0)                            # no slots, no parked conversations
        two = self.plan(**{"server.parallel": 2, "engine.prompt-cache": 4})
        self.assertEqual(item(two, "ram", "checkpoints"), 2 * 4 * 118 * MiB)

    def test_a_settings_that_only_change_behaviour_leave_the_plan_alone(self):
        p = self.plan()
        q = self.plan(**{"engine.adapt-swaps": 0, "engine.prefill-cpu-share": 0.2, "engine.rope-scaling": "yarn",
                         "engine.rope-scale": 2, "engine.expert-profile-save": "x.json", "server.grammar": False,
                         "engine.vram-elastic": True, "engine.keep-vram-commit": True})
        self.assertEqual((q["vram"]["need"], q["ram"]["need"]), (p["vram"]["need"], p["ram"]["need"]))

    def test_the_fingerprint_of_a_calibration_made_before_these_options_is_unchanged(self):
        v = loader.effective(BASE)
        old = hashlib.sha256(json.dumps({k: v.get(k) for k in loader.SPEED_KEYS}, sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(loader.fingerprint(BASE), old)
        self.assertEqual(loader.fingerprint({**BASE, "engine.adapt-swaps": 96, "engine.prefill-cpu-share": 0.6}), old)
        for key, value in (("engine.adapt-swaps", 0), ("engine.prefill-cpu-share", 0.2), ("engine.kv-resident", 30000),
                           ("engine.mmap-experts", True), ("engine.resident-budget-gib", 8), ("engine.vram-elastic", True)):
            self.assertNotEqual(loader.fingerprint({**BASE, key: value}), old, key)

    def test_every_new_field_has_an_engine_default(self):
        for name in NEW:
            self.assertIn(name, loader.ENGINE_DEFAULTS, name)
            self.assertIn(name, [f"{s}.{k}" for s, k in settings.LOADER_FIELDS], name)


class SavedConfigurations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_save_read_back_export_import_and_apply(self):
        values = {**BASE, **NEW, "engine.kv-resident": 0, "engine.resident-budget-gib": None, "server.port": 7300}
        values = {k: v for k, v in values.items() if v is not None}
        profiles.change({"action": "save", "name": "Lunga", "values": values})
        got = profiles.get_load("Lunga")
        self.assertEqual(got, settings_checked(values))
        self.assertIs(got["server.grammar"], False)
        data = json.loads(json.dumps(profiles.export()))
        profiles.change({"action": "delete", "name": "Lunga"})
        profiles.change({"action": "import", "data": data})
        self.assertEqual(profiles.get_load("Lunga"), got)
        cfg = os.path.join(self.dir.name, "basalt_config.json")
        Path(cfg).write_text(json.dumps({"server": {"port": 7232}, "engine": {"kv": "fp16"}}), encoding="utf-8")
        profiles.apply("Lunga", cfg)
        raw = json.loads(Path(cfg).read_text(encoding="utf-8"))
        self.assertEqual((raw["engine"]["prompt-cache"], raw["engine"]["rope-scaling"], raw["server"]["grammar"],
                          raw["server"]["reasoning-budget-tokens"]), (3, "yarn", False, 4096))

    def test_an_older_configuration_without_them_loads_and_takes_the_defaults(self):
        old = {"engine.max-context": 65536, "engine.kv": "int8", "server.port": 7300}
        profiles.change({"action": "save", "name": "Vecchia", "values": old})
        self.assertEqual(profiles.get_load("Vecchia"), old)
        e = loader.effective(profiles.get_load("Vecchia"))
        self.assertEqual((e["engine.adapt-swaps"], e["server.grammar"], e["engine.prompt-cache"]), (96, True, 6))
        self.assertEqual(profiles.listing()["load"][1]["summary"]["text"], "64K · int8 · no MTP · technical session 30000")

    def test_a_bad_or_contradictory_configuration_is_refused(self):
        for values in ({"engine.kv-resident": 5}, {"engine.rope-scaling": "ntk"}, {"engine.kv": "k8v4", "engine.kv-resident": 30000},
                       {"engine.mmap-experts": True, "engine.resident-experts": True}, {"server.grammar": "no"}):
            with self.assertRaises(ValueError, msg=values):
                profiles.change({"action": "save", "name": "x", "values": values})
        profiles.change({"action": "save", "name": "Vuota", "values": {"engine.adapt-swaps": "", "engine.rope-scale": None}})
        self.assertEqual(profiles.get_load("Vuota"), {})                        # an empty field is not saved

    def test_the_comparison_with_the_running_server_sees_the_new_fields(self):
        running = {"engine.max-context": 65536, "engine.adapt-swaps": 96, "server.grammar": True}
        same = profiles.compare(running, None, {"engine.max-context": 65536, "engine.adapt-swaps": 96,
                                                "server.grammar": True}, None)
        self.assertTrue(same["same"])
        diff = profiles.compare(running, None, {"engine.max-context": 65536, "engine.adapt-swaps": 0, "server.grammar": False,
                                                "engine.rope-scale": 2, "engine.mmap-experts": True}, None)
        self.assertEqual(sorted(d["field"] for d in diff["differences"]),
                         ["engine.adapt-swaps", "engine.mmap-experts", "engine.rope-scale", "server.grammar"])
        self.assertTrue(profiles.compare({}, None, {"engine.prefill-cpu-share": 0.6}, None)["same"])    # the default is the same as nothing

    def test_the_running_server_reports_the_new_fields(self):
        a = mock.Mock(port=7232, parallel=None, vision="off", vision_projector="auto", vision_hold="keep",
                      slot_full_context=False, shared_context=None, config=None, model=None, grammar=False,
                      reasoning_budget_tokens=2048)
        words = ["--kv-resident", "30000", "--mmap-experts", "--rope-scaling", "linear", "--adapt-swaps", "12",
                 "--resident-budget-gib", "8.5", "--expert-profile-save", "p.json"]
        running = supervisor.settings_info(a, words, [])["running"]
        self.assertEqual((running["engine.kv-resident"], running["engine.mmap-experts"], running["engine.rope-scaling"],
                          running["engine.adapt-swaps"], running["engine.resident-budget-gib"],
                          running["engine.expert-profile-save"]), (30000, True, "linear", 12, 8.5, "p.json"))
        self.assertIsNone(running["engine.prompt-cache"])
        self.assertEqual((running["server.grammar"], running["server.reasoning-budget-tokens"]), (False, 2048))


def settings_checked(values):
    return {k: settings.check_value(*k.split(".", 1), v) for k, v in values.items()}


class GpuNames(unittest.TestCase):
    GPUS = [{"index": 0, "name": "NVIDIA GeForce RTX 5090"}, {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti"}]

    def setUp(self):
        hardware._gpu_list_cache.clear()

    def test_the_list_is_read_once_in_a_while(self):
        with mock.patch.object(hardware, "_read_gpu_list", return_value=self.GPUS) as read:
            self.assertEqual(hardware.gpu_list(), self.GPUS)
            self.assertEqual(hardware.gpu_list(), self.GPUS)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(hardware.gpu_name("1"), "NVIDIA GeForce RTX 5060 Ti")
            self.assertIsNone(hardware.gpu_name(7))
            self.assertIsNone(hardware.gpu_name("dynamic"))

    def test_no_gpu_is_an_empty_list(self):
        with mock.patch.object(hardware, "_read_gpu_list", return_value=[]):
            self.assertEqual(hardware.gpu_list(), [])
            self.assertIsNone(hardware.gpu_name(0))

    def test_the_fake_gpu_of_the_mock_server(self):
        env = {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_GPU": json.dumps({"name": "Fake 1", "cc": [12, 0]})}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(hardware.gpu_list(), [{"index": 0, "name": "Fake 1"}])
        with mock.patch.dict(os.environ, {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_GPU": "null"}):
            self.assertEqual(hardware.gpu_list(), [])

    def test_the_comparison_names_a_gpu_numbered_encoder(self):
        with mock.patch.object(hardware, "_read_gpu_list", return_value=self.GPUS):
            d = profiles.compare({"server.vision": "dynamic"}, None, {"server.vision": "1"}, None)["differences"][0]
        self.assertEqual((d["running"], d["requested"], d["requested_name"]), ("dynamic", "1", "NVIDIA GeForce RTX 5060 Ti"))
        self.assertNotIn("running_name", d)

    def test_the_loader_payload_lists_the_gpus_and_names_the_vision_card(self):
        dirs = tempfile.TemporaryDirectory()
        path = os.path.join(dirs.name, "basalt_config.json")
        Path(path).write_text(json.dumps({"server": {"port": 7232, "vision": "1"}, "engine": {"kv": "int8"}}), encoding="utf-8")
        svc = make_service()
        svc.settings_info = {"config": path, "model": None, "cli": [], "running": {}}
        svc.supervisor = supervisor.Supervisor(svc, ["--mock-engine", "--config", path], mock=True)
        try:
            with mock.patch.dict(os.environ, {"BASALT_CACHE": dirs.name, "BASALT_DATA": dirs.name}), \
                    mock.patch.object(hardware, "_read_gpu_list", return_value=self.GPUS), \
                    mock.patch.object(loader, "system_now", return_value=dict(ROOMY)):
                body = loader_api.info(svc)
                self.assertEqual(body["limits"]["gpus"], self.GPUS)
                self.assertEqual(body["limits"]["vision"], ["off", "dynamic", "cpu"])
                self.assertEqual(body["limits"]["windows"], os.name == "nt")
                self.assertEqual(body["plan"]["vision_gpu"], {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti"})
                self.assertEqual(body["unsupported"], [])
                self.assertIn("engine.prompt-cache", body["fields"])
                self.assertEqual(body["defaults"]["server.grammar"], True)
                self.assertIsNone(loader_api.evaluate(svc, {"server.vision": "dynamic"})["vision_gpu"])
            hardware._gpu_list_cache.clear()
            with mock.patch.dict(os.environ, {"BASALT_CACHE": dirs.name, "BASALT_DATA": dirs.name}), \
                    mock.patch.object(hardware, "_read_gpu_list", return_value=[]), \
                    mock.patch.object(loader, "system_now", return_value=dict(ROOMY)):
                self.assertEqual(loader_api.info(svc)["limits"]["gpus"], [])
                self.assertEqual(loader_api.evaluate(svc, {"server.vision": "3"})["vision_gpu"], {"index": 3, "name": None})
        finally:
            dirs.cleanup()


# ------------------------------------------------------------------------------------------------ the page's strings
import re  # noqa: E402

from serve.tests import test_web_static as web  # noqa: E402

LOADER_JS = (web.WEB / "loader.js").read_text(encoding="utf-8")


@unittest.skipUnless(web.NODE, "node is needed to read the translations")
class LoaderStrings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = web.strings()

    def fields(self):
        return re.findall(r'name: "([a-z.-]+)", kind: "(\w+)"', LOADER_JS)

    def test_every_key_loader_js_uses_is_in_both_languages(self):
        keys = {k for k in re.findall(r'\bT\(\s*"([\w.]+)"', LOADER_JS) if not k.endswith(".")}      # "rx.no." + x is dynamic
        self.assertTrue(keys)
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in keys if k not in self.s[lang]), [], lang)

    def test_every_new_field_is_explained_in_both_languages_and_the_placeholders_agree(self):
        names = {n for n, _ in self.fields()}
        for name in NEW:
            self.assertIn(name, names, name)
            for lang in ("en", "it"):
                self.assertIn(f"f.{name}", self.s[lang], name)
                self.assertGreater(len(self.s[lang][f"h.{name}"]), 40, name)       # a real sentence, not a placeholder
        for key, en in self.s["en"].items():
            if key.startswith(("f.", "h.", "o.", "pn.", "it.", "ld.")) and key in self.s["it"]:
                self.assertEqual(sorted(re.findall(r"\{\d+\}", en)), sorted(re.findall(r"\{\d+\}", self.s["it"][key])), key)
        for choice in ("none", "linear", "yarn"):
            for lang in ("en", "it"):
                self.assertIn(f"o.engine.rope-scaling.{choice}", self.s[lang])

    def test_no_loader_string_is_defined_and_never_used(self):
        names = {n for n, _ in self.fields()} | {"mtp"}
        for lang in ("en", "it"):
            for key in self.s[lang]:
                for prefix in ("f.", "h."):
                    if key.startswith(prefix + "engine.") or key.startswith(prefix + "server."):
                        self.assertIn(key[2:], names | self.other_users(), f"{lang} {key}")
        block = LOADER_JS[LOADER_JS.index("const noteText"):LOADER_JS.index("const pnotes")]
        used_codes = set(re.findall(r"\b(\w+): \[", block))
        self.assertEqual(used_codes, set(re.findall(r'notes\.append\(\{"code": "(\w+)"', (ROOT / "serve" / "loader.py").read_text(encoding="utf-8"))))
        for lang in ("en", "it"):
            defined = {k[3:] for k in self.s[lang] if k.startswith("pn.")}
            self.assertEqual(defined, used_codes, lang)
        plan = loader.plan({**BASE, **NEW, "engine.resident-experts": True, "engine.mmap-experts": False,
                            "engine.kv-resident": 30000}, F, ROOMY)
        keys = {i["key"] for bar in ("vram", "ram") for i in plan[bar]["items"]}
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in keys if "it." + ("experts_ram" if k == "experts" else k) not in self.s[lang]), [])
            self.assertIn("it.kv_host", self.s[lang])
            self.assertIn("it.conversation", self.s[lang])

    @staticmethod
    def other_users():
        """Loader field strings the other views read too (the safe-values list and the Settings summary use f.<name>)."""
        return {"engine.vram-reserve-mib"}

    def test_the_gpu_option_is_one_key_with_the_number_and_the_name(self):
        for lang in ("en", "it"):
            text = self.s[lang]["o.server.vision.gpu"]
            self.assertTrue(re.search(r"\{0\}.*\{1\}", text), text)
            self.assertNotIn("o.server.vision.0", self.s[lang])
        self.assertIn("GPU {0} ({1})", self.s["en"]["o.server.vision.gpu"])
        self.assertIn("GPU {0} ({1})", self.s["it"]["o.server.vision.gpu"])
        self.assertIn("gpuLabel", LOADER_JS)
        self.assertIn("visionChoices();", LOADER_JS)

    def test_the_new_fields_sit_in_groups_the_page_has_strings_for(self):
        groups = set(re.findall(r'\{ group: "(\w+)"', LOADER_JS))
        for lang in ("en", "it"):
            for g in groups:
                self.assertIn(f"ld.g.{g}", self.s[lang], g)
        self.assertEqual(dict(self.fields())["engine.expert-profile-save"], "path")

    def test_loader_js_still_parses(self):
        out = web.subprocess.run([web.NODE, "--check", str(web.WEB / "loader.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
