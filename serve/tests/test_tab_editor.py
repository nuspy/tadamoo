"""Changing a plan or a cron job by an instruction typed in its tab: the editing conversation is made once, set to Automatic with its rules, and told what to change."""
import pytest

from serve import tab_editor
from serve.agent_runs import RunError
from serve.tests.test_agent_sessions import make, own_data_folder, until, ws  # noqa: F401  (fixtures)


def test_the_first_instruction_makes_the_conversation_and_sends_it(make, tmp_path):
    mgr = make(default_workspace=tmp_path / "chat")
    sid = tab_editor.instruct(mgr, "plan", "cervinia", "add a check every 2 hours")["session"]
    rec = mgr.get(sid)
    assert rec["alias"] == tab_editor.ALIAS and rec["mode"] == "auto" and mgr.resolve(tab_editor.ALIAS) == sid
    assert (mgr.history_dir(sid) / "rules.md").read_text(encoding="utf-8").startswith("You change plans and cron jobs")
    sent = [e for e in mgr.events(sid, 0)["events"] if e.get("event") == "user"]
    assert sent and sent[-1]["text"] == "Change the plan `cervinia`: add a check every 2 hours"


def test_the_next_instruction_uses_the_same_conversation(make, tmp_path):
    mgr = make(default_workspace=tmp_path / "chat")
    first = tab_editor.instruct(mgr, "cron", "morning", "run at 09:00")["session"]
    second = tab_editor.instruct(mgr, "plan", "cervinia", "pause it")["session"]
    assert first == second and len([r for r in mgr.records if r.get("alias") == tab_editor.ALIAS]) == 1


def test_after_it_was_closed_a_new_one_is_made(make, tmp_path):
    mgr = make(default_workspace=tmp_path / "chat")
    first = tab_editor.instruct(mgr, "cron", "morning", "run at 09:00")["session"]
    until(lambda: mgr.state_of(first) == "idle")                 # the first instruction is answered before the conversation is closed
    mgr.close(first)
    assert tab_editor.instruct(mgr, "cron", "morning", "run at 10:00")["session"] != first


@pytest.mark.parametrize("args, code", [(("trip", "x", "y"), "bad_kind"), (("plan", "", "y"), "bad_target"), (("plan", "x", "  "), "text_missing"),
                                         (("plan", "x", "y" * 4001), "too_long"), (("plan", "z" * 121, "y"), "bad_target")])
def test_a_bad_request_is_refused_before_anything_is_made(make, tmp_path, args, code):
    mgr = make(default_workspace=tmp_path / "chat")
    with pytest.raises(RunError) as e:
        tab_editor.instruct(mgr, *args)
    assert e.value.code == code and not mgr.records
