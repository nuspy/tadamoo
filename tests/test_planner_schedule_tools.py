"""plan_pause, plan_resume, plan_set_schedule and plan_remove_schedule: the owner's session needs no yes for its own plan; another
session's is asked, and nothing changes on a no. plan_get(view="compact") gives a short reading of a plan."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.integrations.planner_client import PlannerClient
from tadamoo.planner_tools import Confirmer, register_planner_tools
from tadamoo.tools.registry import EffectClass, ToolRegistry
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_planner_tools import AgentState, Asker, title  # noqa: E402

STEPS = [{"title": "Prenota"}, {"title": "Parti"}]


class FakeCron:
    """A stand-in for the cron_* tools plan_set_schedule and plan_remove_schedule look up by name."""

    def __init__(self):
        self.created, self.deleted = [], []

    def create(self, name, schedule, workspace, prompt="", deliver_kind="new_session", deliver_ref="", message="", ttl_min=None, dedup_key=""):
        self.created.append({"name": name, "schedule": schedule, "workspace": workspace, "deliver_kind": deliver_kind,
                             "deliver_ref": deliver_ref, "message": message, "ttl_min": ttl_min})
        return f"Created r1: r1 \"{name}\" [on] {schedule} -> {deliver_kind} '{deliver_ref}'"

    def delete(self, id):
        self.deleted.append(id)
        return f"Removed {id}."


def rig(url, session_id="", ask=None):
    client = PlannerClient(url, session_id=session_id)
    agent = AgentState()
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, ask or Asker()))
    cron = FakeCron()
    reg.register("cron_create", "stand-in", {"type": "object", "properties": {}}, EffectClass.REVERSIBLE, cron.create)
    reg.register("cron_delete", "stand-in", {"type": "object", "properties": {"id": {"type": "string"}}}, EffectClass.REVERSIBLE, cron.delete)
    return reg, agent, cron


def make_plan(reg, agent, t):
    agent.last_user_text = f"Crea il piano {t}"
    made = reg.execute("plan_create", {"title": t, "kind": "occasional", "steps": STEPS, "quote": f"Crea il piano {t}"})
    return re.search(r"^Plan (\S+):", made, re.M).group(1)


@pytest.fixture
def url(start_server):  # noqa: F811
    return start_server([answer("x")])


def test_the_owner_pauses_and_resumes_without_being_asked(url):
    reg, agent, _ = rig(url, session_id="aa11bb22")
    plan_id = make_plan(reg, agent, title())
    assert "paused (auto)" in reg.get("plan_pause").handler(plan_id)
    assert "(paused)" in reg.get("plan_get").handler(plan_id, "compact")
    assert "resumed (auto)" in reg.get("plan_resume").handler(plan_id)
    assert "(paused)" not in reg.get("plan_get").handler(plan_id, "compact")


def test_another_session_is_asked_and_a_no_changes_nothing(url):
    owner_reg, owner_agent, _ = rig(url, session_id="aa11bb22")
    plan_id = make_plan(owner_reg, owner_agent, title())
    asker = Asker("cancel")
    other_reg, other_agent, _ = rig(url, session_id="bb22cc33", ask=asker)
    out = other_reg.get("plan_pause").handler(plan_id)
    assert len(asker.asked) == 1 and "nothing was changed" in out.lower()
    assert "(paused)" not in owner_reg.get("plan_get").handler(plan_id, "compact")


def test_set_schedule_creates_a_cron_and_remove_schedule_deletes_it(url):
    reg, agent, cron = rig(url, session_id="aa11bb22")
    plan_id = make_plan(reg, agent, title())
    out = reg.get("plan_set_schedule").handler(plan_id, "night", "every 1h", "Check in on {plan_id}.")
    assert cron.created[0]["deliver_kind"] == "plan" and cron.created[0]["deliver_ref"] == "night" and cron.created[0]["workspace"] == ""
    assert "Scheduled (auto)" in out
    assert "night" in reg.get("plan_get").handler(plan_id, "compact")
    out = reg.get("plan_remove_schedule").handler(plan_id)
    assert cron.deleted == ["r1"] and "Schedule removed" in out
    assert "has no schedule" in reg.get("plan_remove_schedule").handler(plan_id)


def test_set_schedule_without_a_cron_tool_says_so(url):
    client = PlannerClient(url, session_id="aa11bb22")
    agent = AgentState()
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, Asker()))
    plan_id = make_plan(reg, agent, title())
    assert "no cron_create tool" in reg.get("plan_set_schedule").handler(plan_id, "night", "every 1h")


def test_plan_get_compact_shows_state_ready_steps_and_values(url):
    reg, agent, _ = rig(url, session_id="aa11bb22")
    plan_id = make_plan(reg, agent, title())
    out = reg.get("plan_get").handler(plan_id, "compact")
    assert plan_id in out and "pending" in out and "Ready:" in out
    full = reg.get("plan_get").handler(plan_id)
    assert len(full) > len(out)                                  # the full view says more
