"""Events that come from this machine and from the person's own hooks, and the interface of the sources that need a service (a modem, a calendar)."""
import datetime
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.hooks import HookConfigError, HookManager, parse_hooks
from harness.integrations.planner_client import PlannerClient
from harness.planner import sources
from harness.planner.local_sources import CalendarSource, DeadlineSource, SmsSource, SystemSource
from harness.planner.sources import SourceError, SourceRunner, build_source, clean_source, clean_sources
from serve.plans import PlanService
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_planner import node
from test_planner_events import SNOW, service, wait_for, _close_services  # noqa: F401  (fixtures)
from test_session_tasks import Model, make


# -- settings ------------------------------------------------------------------------------------------------------

def test_the_settings_of_the_local_sources_are_checked_and_hold_no_secret():
    assert clean_source({"kind": "system", "disk_path": "C:\\"}) == {"id": "system-1", "kind": "system", "enabled": True, "disk_path": "C:\\",
                                                                   "min_free_gb": 5.0, "interval_seconds": 600.0}
    assert clean_source({"kind": "deadlines", "lead_days": [1, 7, 7]})["lead_days"] == [7, 1]
    assert clean_source({"kind": "deadlines"})["lead_days"] == [7, 3, 1, 0]
    for bad in ({"kind": "system"}, {"kind": "system", "disk_path": ""}, {"kind": "system", "disk_path": "C:\\", "min_free_gb": 0},
                {"kind": "system", "disk_path": "C:\\", "min_free_gb": "5"}, {"kind": "system", "disk_path": "a" * 300},
                {"kind": "deadlines", "lead_days": []}, {"kind": "deadlines", "lead_days": [-1]}, {"kind": "deadlines", "lead_days": [True]},
                {"kind": "deadlines", "lead_days": list(range(9))}, {"kind": "deadlines", "interval_seconds": 0}):
        with pytest.raises(ValueError):
            clean_source(bad)
    assert [c["kind"] for c in clean_sources([{"kind": "system", "disk_path": "/"}, {"kind": "deadlines", "plan_id": "p1"}])] == ["system", "deadlines"]


def test_sms_and_calendar_are_not_kinds_a_setting_can_name():
    assert "sms" not in sources.KINDS and "calendar" not in sources.KINDS
    with pytest.raises(ValueError, match="kind"):
        clean_source({"kind": "sms"})


# -- the machine's own state -----------------------------------------------------------------------------------------

def usage(free_gb):
    return lambda path: SimpleNamespace(free=int(free_gb[0] * 2 ** 30), total=500 * 2 ** 30)


def test_a_low_disk_is_reported_once_and_again_only_after_it_recovered():
    free = [50.0]
    src = SystemSource("s", "C:\\", 5.0, usage=usage(free))
    assert src.poll() == []
    free[0] = 3.2
    first = src.poll()
    assert len(first) == 1 and "free space on C:\\ is low: 3.2 GB left of 500 GB" in first[0].text and first[0].source == "system:disk"
    free[0] = 2.0
    assert src.poll() == []                                  # still low: not reported again
    free[0] = 40.0
    assert src.poll() == []
    free[0] = 1.0
    assert len(src.poll()) == 1                              # low again after recovering


def test_a_disk_that_cannot_be_read_is_an_error_not_an_event():
    def broken(path):
        raise FileNotFoundError(path)
    with pytest.raises(SourceError, match="could not be read"):
        SystemSource("s", "Z:\\", 5.0, usage=broken).poll()
    real = SystemSource("s", str(Path(__file__).parent), 0.0001)
    assert real.poll() == [] and real.state["low"] is False            # the real disk of this machine is read, and it has room


# -- the plans' own dates -------------------------------------------------------------------------------------------

def dated(starts="2026-10-24", ends=""):
    return lambda: [{"plan_id": "cervinia", "title": "Weekend a Cervinia", "starts_on": starts, "ends_on": ends}]


def deadlines(today, plans=None, state=None):
    return DeadlineSource("d", plans or dated(), (7, 3, 1, 0), state=state if state is not None else {}, today=lambda: datetime.date.fromisoformat(today))


def test_a_plan_gets_one_event_for_each_lead_time_it_reaches():
    state = {}
    got = []
    for today in ("2026-10-10", "2026-10-17", "2026-10-17", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"):
        got += [(today, e.text) for e in deadlines(today, state=state).poll()]
    assert [g[0] for g in got] == ["2026-10-17", "2026-10-21", "2026-10-23", "2026-10-24"]
    assert got[0][1] == 'The plan "Weekend a Cervinia" starts in 7 days (2026-10-24)'
    assert got[2][1].endswith("starts in 1 day (2026-10-24)") and got[3][1] == 'The plan "Weekend a Cervinia" starts today (2026-10-24)'


def test_a_server_that_was_off_on_the_day_tells_the_nearest_lead_once():
    state = {}
    first = deadlines("2026-10-22", state=state).poll()
    assert len(first) == 1 and "in 2 days" in first[0].text
    assert deadlines("2026-10-22", state=state).poll() == [] and deadlines("2026-10-21", state=state).poll() == []        # 7 and 3 were counted too


def test_the_end_of_a_plan_and_the_plan_the_event_is_for():
    ev = deadlines("2026-10-25", dated("", "2026-10-26")).poll()
    assert [e.plan_id for e in ev] == ["cervinia"] and "ends in 1 day" in ev[0].text


def test_dates_that_are_not_dates_or_are_past_give_nothing():
    for bad in ("", "domani", "2026-13-40"):
        assert deadlines("2026-10-20", dated(bad)).poll() == []
    assert deadlines("2026-11-20", dated("2026-10-24")).poll() == []


def test_the_deadlines_of_the_plans_of_a_service_become_events_for_their_own_plan(tmp_path):
    svc = service(tmp_path)
    soon = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    sensor = {"plan_id": "gita", "title": "Gita", "target_type": "occasional", "nodes": {
        "avviso": {"id": "avviso", "type": "sensor", "title": "Avviso", "description": "", "assignee": "agent", "dependencies": [], "outputs": [],
                   "conditions": {"when": "'in 3 days' in text", "set": {"vicino": True}}}}}
    other = {**SNOW, "plan_id": "altro"}
    svc.create({"plan": sensor, "profile": {"starts_on": soon, "ends_on": soon}})
    svc.create({"plan": other})
    svc.update_settings({"sources": [{"id": "scadenze", "kind": "deadlines", "lead_days": [3], "interval_seconds": 3600}]})
    assert wait_for(lambda: svc.snapshot("gita")["plan"]["shared_state"].get("vicino") is True)       # the event reached its own plan's sensor
    assert svc.snapshot("altro")["plan"]["shared_state"] == {}                                        # and not the plan without dates
    status = svc.events_status()["sources"][0]
    assert status["kind"] == "deadlines" and status["last_error"] is None


def test_a_finished_plan_is_not_reminded(tmp_path):
    svc = service(tmp_path)
    soon = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    svc.create({"plan": {"plan_id": "fatto", "title": "Fatto", "target_type": "occasional", "nodes": {
        "a": {"id": "a", "type": "action", "title": "A", "description": "", "assignee": "agent", "dependencies": [], "outputs": [], "status": "completed"}}},
        "profile": {"starts_on": soon}})
    assert svc._plan_dates() == []


# -- the providers (no real modem or calendar behind them) -------------------------------------------------------------

def test_sms_from_a_provider_become_events_once_each():
    box = [{"id": "1", "from": "+39 333", "text": "Il treno è soppresso"}, {"id": "2", "from": "Gianni", "text": "  "}]
    src = SmsSource("sms", lambda: box)
    first = src.poll()
    assert [e.text for e in first] == ["SMS from +39 333: Il treno è soppresso"] and first[0].source == "sms"
    assert src.poll() == []
    box.append({"id": "3", "text": "Arrivo tardi"})
    assert [e.text for e in src.poll()] == ["SMS from unknown sender: Arrivo tardi"]
    assert src.state["seen"] == ["1", "2", "3"]                           # the empty one is not told again either


def test_a_provider_that_fails_or_sends_junk_is_an_error_or_ignored():
    def broken():
        raise RuntimeError("modem unplugged secret-token")
    with pytest.raises(SourceError) as err:
        SmsSource("sms", broken).poll()
    assert "RuntimeError" in str(err.value) and "secret-token" not in str(err.value)
    assert SmsSource("sms", lambda: ["junk", {"text": "no id"}, {"id": "", "text": "x"}]).poll() == []


def test_a_calendar_tells_of_appointments_inside_its_lead_time_only():
    now = 1_800_000_000.0
    items = [{"id": "a", "title": "Dentista", "start": now + 3600}, {"id": "b", "title": "Lontano", "start": now + 3 * 86400},
             {"id": "c", "title": "Passato", "start": now - 60}, {"id": "d", "title": "Senza ora"}, {"id": "e", "title": "Falso", "start": True}]
    src = CalendarSource("cal", lambda: items, lead_hours=24, now=lambda: now)
    out = src.poll()
    assert len(out) == 1 and out[0].text.startswith("Calendar: Dentista at ") and out[0].source == "calendar"
    assert src.poll() == []
    later = CalendarSource("cal", lambda: items, lead_hours=24, now=lambda: now + 3 * 86400 - 3600, state=src.state)
    assert [e.text.split(" at ")[0] for e in later.poll()] == ["Calendar: Lontano"]


def test_a_runner_delivers_what_a_provider_source_makes():
    got = []
    src = SmsSource("sms", lambda: [{"id": "1", "from": "Anna", "text": "ciao"}])
    runner = SourceRunner([src], lambda s, events: got.extend(events))
    assert runner.poll_once(src) == 1 and got[0].text == "SMS from Anna: ciao" and runner.status()[0]["events"] == 1


# -- the person's hooks ----------------------------------------------------------------------------------------------

def hook(**kw):
    return {"event": "post_tool", "action": "plan_event", "text": "File changed by {tool}", **kw}


def test_a_plan_event_hook_is_read_and_a_wrong_one_is_refused():
    spec = parse_hooks([hook(tool="write_file", name="watch")], "t")[0]
    assert (spec.action, spec.text, spec.tools) == ("plan_event", "File changed by {tool}", ["write_file"])
    assert parse_hooks([{"event": "turn_end", "action": "plan_event", "text": "Un turno è finito"}], "t")[0].event == "turn_end"
    for bad in ({"event": "pre_tool", "action": "plan_event", "text": "x"}, hook(text=""), hook(text="x" * 301), {"event": "post_tool", "action": "plan_event"},
                hook(command="echo"), {"event": "post_tool", "command": "echo", "text": "x"}, hook(action="notify")):
        with pytest.raises(HookConfigError):
            parse_hooks([bad], "t")


def test_a_post_tool_hook_tells_the_planner_with_only_the_event_and_the_tool_in_the_text(tmp_path):
    told = []
    mgr = HookManager()
    mgr.plan_event_sink = lambda text, source: told.append((text, source))
    mgr.add_specs(parse_hooks([hook(tool="write_file", name="watch", text="{tool} done at {event}; {arguments} {result}")], "t"), tmp_path)
    mgr.trigger_post_tool("read_file", {"path": "x"}, "r")
    assert told == []                                                      # only the tools it names
    mgr.trigger_post_tool("write_file", {"path": "secret.txt", "content": "TOKEN"}, "TOKEN written")
    assert told == [("write_file done at post_tool; {arguments} {result}", "hook:watch")]
    assert "TOKEN" not in told[0][0] and mgr.errors == []


def test_a_lifecycle_hook_tells_the_planner_and_without_a_planner_it_says_so(tmp_path):
    told = []
    mgr = HookManager()
    mgr.add_specs(parse_hooks([{"event": "session_end", "action": "plan_event", "text": "La conversazione è finita", "name": "fine"}], "t"), tmp_path)
    mgr.trigger_event("session_end", {})
    assert any("there is no planner to tell" in e for e in mgr.errors)
    mgr.plan_event_sink = lambda text, source: told.append((text, source))
    mgr.trigger_event("session_end", {})
    assert told == [("La conversazione è finita", "hook:fine")]


def test_a_planner_that_cannot_be_reached_does_not_stop_the_tool(tmp_path):
    mgr = HookManager()

    def down(text, source):
        raise OSError("connection refused")
    mgr.plan_event_sink = down
    mgr.add_specs(parse_hooks([hook(name="watch")], "t"), tmp_path)
    mgr.trigger_post_tool("write_file", {}, "ok")                          # does not raise
    assert any("OSError" in e for e in mgr.errors)


def test_the_conversation_gives_its_hooks_the_planner(tmp_path):
    session, ev, store, built = make(tmp_path, Model())
    session.agent.hooks = HookManager()
    told = []
    session.enable_planner(SimpleNamespace(ingest=lambda text, source: told.append((text, source)), list=lambda archived=False: [], snapshot=lambda pid: {}))
    session.agent.hooks.plan_event_sink("hello", "hook:x")
    assert told == [("hello", "hook:x")]


def test_a_hook_event_reaches_a_plans_sensor_through_the_server(start_server):
    url = start_server([answer("x")])
    client = PlannerClient(url)
    pid = f"hook-{uuid.uuid4().hex[:6]}"
    client.create({"plan_id": pid, "title": "Hook", "target_type": "occasional", "nodes": {
        "avviso": {"id": "avviso", "type": "sensor", "title": "Avviso", "description": "", "assignee": "agent", "dependencies": [], "outputs": [],
                   "conditions": {"when": "'bonifico' in text", "set": {"pagato": True}}}}})
    mgr = HookManager()
    mgr.plan_event_sink = lambda text, source: client.ingest(text, source)
    mgr.add_specs(parse_hooks([hook(tool="request_payment", name="pagamenti", text="Bonifico richiesto da {tool}")], "t"), Path("."))
    mgr.trigger_post_tool("request_payment", {}, "ok")
    assert mgr.errors == []
    snap = client.snapshot(pid)
    assert snap["plan"]["shared_state"] == {"pagato": True} and snap["plan"]["nodes"]["avviso"]["state_variables"]["matched"]["source"] == "hook:pagamenti"
