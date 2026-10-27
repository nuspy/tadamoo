"""The planner's event pump and sources start with the server when planner settings are saved, and stay off when the person turned them off."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import handler
from serve.plans import PlanService


class FakeSvc:
    api_key = ""


def pump_alive():
    return any(t.name == "inbox-pump" and t.is_alive() for t in threading.enumerate())


class PlannerBootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "planner"
        self.base.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def boot(self):
        with mock.patch.object(handler, "_PLANS", None), mock.patch.object(handler.userdata, "path", lambda name: Path(self.tmp.name) / name):
            handler._start_planner(FakeSvc(), 1)
            made = handler._PLANS
        self.addCleanup(lambda: made and made.close())
        return made

    def test_saved_settings_bring_the_pump_up_at_start(self):
        (self.base / "planner.json").write_text(json.dumps({"event_pump": True}), encoding="utf-8")
        made = self.boot()
        self.assertIsNotNone(made)
        self.assertTrue(made.events_status()["pump"])
        self.assertTrue(pump_alive())

    def test_a_pump_the_person_turned_off_stays_off(self):
        (self.base / "planner.json").write_text(json.dumps({"event_pump": False}), encoding="utf-8")
        made = self.boot()
        self.assertFalse(made.events_status()["pump"])

    def test_without_saved_settings_nothing_is_made_at_start(self):
        self.assertIsNone(self.boot())

    def test_a_planner_that_cannot_start_does_not_stop_the_server(self):
        (self.base / "planner.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(PlanService, "start", side_effect=RuntimeError("boom")):
            self.boot()                                          # must not raise


if __name__ == "__main__":
    unittest.main()
