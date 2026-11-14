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


class RoutinesBootTest(unittest.TestCase):
    """The clock of the routines starts with the server when routines or plans are saved, not at the first request."""

    def boot(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in files:
            path = Path(tmp.name) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        made = []

        class Handler:
            def _routines(self):
                made.append(1)

        host = mock.Mock(RequestHandlerClass=Handler)
        with mock.patch.object(handler, "_PLAN_HOST", host), mock.patch.object(handler.userdata, "path", lambda name: Path(tmp.name) / name):
            handler._start_routines()
        return made

    def test_saved_routines_start_the_clock(self):
        self.assertTrue(self.boot(["journal/routines.json"]))

    def test_saved_plans_start_the_clock(self):
        self.assertTrue(self.boot(["planner/plans/one.json"]))

    def test_nothing_saved_makes_nothing(self):
        self.assertFalse(self.boot([]))


if __name__ == "__main__":
    unittest.main()
