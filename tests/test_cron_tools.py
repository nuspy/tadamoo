"""cron_list, cron_create, cron_update, cron_pause, cron_resume, cron_delete, cron_fire_now over a fake client."""
from tadamoo.cron_tools import register_cron_tools
from tadamoo.integrations._http import ServiceError
from tadamoo.tools.registry import ToolRegistry


class FakeRoutines:
    def __init__(self):
        self.created, self.acted = [], []
        self.routines = []
        self.fail = None

    def list(self):
        if self.fail:
            raise self.fail
        return self.routines

    def create(self, fields):
        if self.fail:
            raise self.fail
        self.created.append(fields)
        routine = {"id": "r1", "name": fields["name"], "schedule": fields["schedule"], "enabled": True,
                   "deliver_to": fields.get("deliver_to") or {"kind": "new_session", "ref": ""}}
        self.routines.append(routine)
        return {"routine": routine}

    def act(self, routine_id, action, fields=None):
        if self.fail:
            raise self.fail
        self.acted.append((routine_id, action, fields))
        routine = next((r for r in self.routines if r["id"] == routine_id), {"id": routine_id, "name": "x", "schedule": "every 1h", "enabled": True,
                                                                              "deliver_to": {"kind": "new_session", "ref": ""}})
        if action == "enable":
            routine["enabled"] = True
        if action == "disable":
            routine["enabled"] = False
        if action == "update":
            routine.update(fields or {})
        return {"routine": routine}


def make():
    reg, client = ToolRegistry(), FakeRoutines()
    register_cron_tools(reg, client)
    return reg, client


def test_cron_list_shows_each_schedule_and_where_it_fires():
    reg, client = make()
    assert reg.get("cron_list").handler() == "There are no schedules."
    client.routines = [{"id": "r1", "name": "watch", "schedule": "every 1h", "enabled": True, "deliver_to": {"kind": "new_session", "ref": ""}},
                       {"id": "r2", "name": "tell", "schedule": "daily 08:30", "enabled": False, "deliver_to": {"kind": "session", "ref": "night"}}]
    out = reg.get("cron_list").handler()
    assert "r1 \"watch\" [on] every 1h -> a new conversation" in out
    assert "r2 \"tell\" [off] daily 08:30 -> session 'night'" in out


def test_cron_create_delivers_to_a_session_when_asked():
    reg, client = make()
    out = reg.get("cron_create").handler(name="check", schedule="every 1h", workspace="D:/w", deliver_kind="session", deliver_ref="night",
                                         message="hi {date}")
    assert client.created == [{"name": "check", "schedule": "every 1h", "workspace": "D:/w", "prompt": "check",
                               "deliver_to": {"kind": "session", "ref": "night"}, "message": "hi {date}"}]
    assert "Created r1" in out and "session 'night'" in out


def test_cron_create_names_what_is_wrong_without_calling_the_client():
    reg, client = make()
    assert "Give the schedule a name" in reg.get("cron_create").handler(name="", schedule="every 1h", workspace="D:/w")
    assert "Name the conversation" in reg.get("cron_create").handler(name="x", schedule="every 1h", workspace="D:/w", deliver_kind="session")
    assert "Write what it should do" in reg.get("cron_create").handler(name="x", schedule="every 1h", workspace="D:/w")
    assert client.created == []


def test_cron_pause_resume_fire_now_and_delete():
    reg, client = make()
    reg.get("cron_create").handler(name="check", schedule="every 1h", workspace="D:/w", prompt="watch it")
    assert "[off]" in reg.get("cron_pause").handler("r1") and client.acted[-1] == ("r1", "disable", None)
    assert "[on]" in reg.get("cron_resume").handler("r1") and client.acted[-1] == ("r1", "enable", None)
    assert "Queued r1" in reg.get("cron_fire_now").handler("r1") and client.acted[-1] == ("r1", "run", None)
    assert reg.get("cron_delete").handler("r1") == "Removed r1." and client.acted[-1] == ("r1", "remove", None)


def test_cron_update_passes_the_given_fields_through():
    reg, client = make()
    reg.get("cron_create").handler(name="check", schedule="every 1h", workspace="D:/w", prompt="watch it")
    out = reg.get("cron_update").handler("r1", schedule="every 2h")
    assert client.acted[-1] == ("r1", "update", {"schedule": "every 2h"}) and "every 2h" in out


def test_a_service_failure_is_reported_not_raised():
    reg, client = make()
    client.fail = ServiceError("offline", "x")
    assert "offline" in reg.get("cron_list").handler()
    assert "offline" in reg.get("cron_pause").handler("r1")
