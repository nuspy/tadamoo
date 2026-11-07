"""session_spawn's server side: parent, depth, the cap on live background children, and the rules file it writes."""
import pytest

from serve.agent_runs import RunError
from serve.tests.test_agent_sessions import make, own_data_folder, ws  # noqa: F401  (fixtures)


def test_a_spawned_conversation_knows_its_parent_and_depth(make, ws):
    mgr = make()
    a = mgr.create(ws)["id"]
    b = mgr.create(ws, parent=a)["id"]
    c = mgr.create(ws, parent=b)["id"]
    assert mgr.get(a)["depth"] == 0 and mgr.get(b)["depth"] == 1 and mgr.get(c)["depth"] == 2
    assert mgr.get(b)["parent"] == a


def test_a_chain_of_background_conversations_cannot_go_past_the_depth_limit(make, ws):
    mgr = make()
    a = mgr.create(ws)["id"]
    b = mgr.create(ws, parent=a)["id"]
    c = mgr.create(ws, parent=b)["id"]
    with pytest.raises(RunError) as e:
        mgr.create(ws, parent=c)
    assert e.value.code == "too_deep"


def test_at_most_a_few_live_background_children_per_parent(make, ws):
    mgr = make()
    a = mgr.create(ws)["id"]
    kids = [mgr.create(ws, parent=a, unattended=True)["id"] for _ in range(3)]
    with pytest.raises(RunError) as e:
        mgr.create(ws, parent=a, unattended=True)
    assert e.value.code == "too_many_children"
    mgr.close(kids[0])
    assert mgr.create(ws, parent=a, unattended=True)["id"]                     # closing one frees a slot
    non_background = mgr.create(ws, parent=a)["id"]                           # an attended one does not count against the cap
    assert non_background


def test_an_unknown_parent_is_refused(make, ws):
    mgr = make()
    with pytest.raises(RunError) as e:
        mgr.create(ws, parent="deadbeef")
    assert e.value.code == "gone"


def test_rules_given_at_creation_are_written_to_a_file_the_command_line_passes(make, ws):
    mgr = make()
    sid = mgr.create(ws, operating_rules="Never write the shared log directly.")["id"]
    rec = mgr.get(sid)
    path = rec["rules_file"]
    assert path and "Never write the shared log directly." in open(path, encoding="utf-8").read()
    cmd = mgr._command(sid, rec, mgr.history_dir(sid))
    assert "--rules-file" in cmd and cmd[cmd.index("--rules-file") + 1] == path


def test_without_rules_there_is_no_rules_file_or_flag(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    rec = mgr.get(sid)
    assert "rules_file" not in rec
    assert "--rules-file" not in mgr._command(sid, rec, mgr.history_dir(sid))


def test_the_orchestration_flag_follows_alias_unattended_or_being_spawned(make, ws):
    mgr = make()
    parent = mgr.create(ws)["id"]
    aliased = mgr.create(ws, alias="night")["id"]
    unattended = mgr.create(ws, unattended=True)["id"]
    spawned = mgr.create(ws, parent=parent)["id"]
    for sid in (aliased, unattended, spawned):
        assert "--orchestration" in mgr._command(sid, mgr.get(sid), mgr.history_dir(sid))


def test_a_plain_conversation_gets_no_orchestration_flag(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    assert "--orchestration" not in mgr._command(sid, mgr.get(sid), mgr.history_dir(sid))
