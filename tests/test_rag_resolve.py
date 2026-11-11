"""Which archives apply to a function: the resolver, its cloud and audience rules, and the cheap checks it relies on."""
import pytest

from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag.resolve import Resolution, describe, resolve, routes_outside

PROJ, OTHER = project_key("project-one"), project_key("project-two")
LOCAL = lambda function: False
CLOUD = lambda function: True


def make(tmp_path, *rows):
    """rows: (name, project, {cloud_ok, others_ok})"""
    rag = RagStore(tmp_path / "rag")
    ids = []
    for name, project, flags in rows:
        aid = rag.create(name, "about " + name, project, "Project " + ("One" if project == PROJ else "Two"))["id"]
        if flags:
            rag.update(aid, flags)
        ids.append(aid)
    return rag, ids


def test_the_agent_gets_its_projects_archives_and_the_attached_ones_by_name(tmp_path):
    rag, (a, b, c) = make(tmp_path, ("Boats", PROJ, {}), ("Kitchen", PROJ, {}), ("Forests", OTHER, {}))
    got = resolve(rag, {"function": "agent", "project": PROJ, "attached": [c]}, leaves_pc=LOCAL)
    assert isinstance(got, Resolution) and sorted(got.ids) == sorted([a, b, c]) and got.blocked == []
    forests = next(x for x in got.archives if x["id"] == c)
    assert forests["name"] == "Forests" and forests["project_name"] == "Project Two" and forests["via"] == "attached" and forests["documents"] == 0
    assert forests["cloud_ok"] is False and forests["others_ok"] == "ask_dialog" and forests["description"] == "about Forests"
    assert describe(got) == "Boats; Kitchen; Forests (Project Two)" and a not in describe(got)


def test_a_model_that_leaves_the_pc_takes_only_archives_cleared_for_cloud(tmp_path):
    rag, (a, b) = make(tmp_path, ("Open", PROJ, {"cloud_ok": True}), ("Private", PROJ, {}))
    got = resolve(rag, {"function": "agent", "project": PROJ}, leaves_pc=CLOUD)
    assert got.ids == [a] and got.routed_to_cloud
    assert got.blocked == [{"id": b, "name": "Private", "project_name": "Project One", "codes": ["cloud"], "reason": "not cleared for cloud models", "verdict": "deny"}]
    assert "Private (not cleared for cloud models)" in describe(got)
    assert sorted(resolve(rag, {"function": "agent", "project": PROJ}, leaves_pc=LOCAL).ids) == sorted([a, b])


def test_the_caller_can_make_the_cloud_rule_stricter_never_looser(tmp_path):
    rag, (a,) = make(tmp_path, ("Private", PROJ, {}))
    assert resolve(rag, {"function": "agent", "project": PROJ, "routed_to_cloud": True}, leaves_pc=LOCAL).ids == []
    assert resolve(rag, {"function": "agent", "project": PROJ, "routed_to_cloud": False}, leaves_pc=CLOUD).ids == []
    assert resolve(rag, {"function": "agent", "project": PROJ, "routed_to_cloud": False}, leaves_pc=LOCAL).ids == [a]


def test_the_session_record_decides_the_project_and_the_attached_archives(tmp_path):
    rag, (a, b, c) = make(tmp_path, ("Boats", PROJ, {}), ("Kitchen", OTHER, {}), ("Forests", OTHER, {}))
    rec = {"workspace": "project-one", "knowledge": [b]}
    got = resolve(rag, {"function": "agent", "session": "s1", "project": OTHER, "attached": [c]}, sources={"session": lambda ctx: rec}, leaves_pc=LOCAL)
    assert sorted(got.ids) == sorted([a, b]) and got.project == PROJ                  # what the caller says about project and attached is not used


def test_output_for_other_people_needs_others_ok(tmp_path):
    rag, (a, b, c, d) = make(tmp_path, ("Allowed", PROJ, {"others_ok": "allow"}), ("Asked", PROJ, {}), ("Never", PROJ, {"others_ok": "deny"}), ("Aloud", PROJ, {"others_ok": "ask_voice"}))
    got = resolve(rag, {"function": "meeting", "project": PROJ}, leaves_pc=LOCAL)
    assert got.audience == "others" and got.ids == [a] and sorted(got.ask) == sorted([b, d]) and got.ask_dialog == [b] and got.ask_voice == [d]
    assert sorted((x["id"], x["verdict"]) for x in got.archives) == sorted([(a, "allow"), (b, "ask_dialog"), (d, "ask_voice")])
    assert got.blocked[0]["id"] == c and got.blocked[0]["codes"] == ["others"] and got.blocked[0]["verdict"] == "deny"
    assert got.blocked[0]["reason"] == "not cleared to be said, written or shown to other people"
    yes = resolve(rag, {"function": "call", "project": PROJ, "granted": [b, c, d]}, leaves_pc=LOCAL)
    assert sorted(yes.ids) == sorted([a, b, d]) and yes.ask == [] and [x["id"] for x in yes.blocked] == [c]       # a yes never lifts a deny
    assert got.to_dict()["ask_dialog"] == [b] and got.to_dict()["ask_voice"] == [d]


def test_the_audience_of_a_meeting_or_a_call_cannot_be_declared_private(tmp_path):
    rag, _ = make(tmp_path, ("Asked", PROJ, {}))
    for function in ("meeting", "call"):
        assert resolve(rag, {"function": function, "project": PROJ, "audience": "me"}, leaves_pc=LOCAL).audience == "others"
    assert resolve(rag, {"function": "copilot", "project": PROJ}, leaves_pc=LOCAL).audience == "me"
    assert resolve(rag, {"function": "copilot", "project": PROJ, "audience": "others"}, leaves_pc=LOCAL).audience == "me"     # the private window needs no consent
    assert resolve(rag, {"function": "avatar", "project": PROJ, "audience": "others"}, leaves_pc=LOCAL).ask != []
    assert resolve(rag, {"function": "avatar", "project": PROJ}, leaves_pc=LOCAL).ids != []


def test_both_reasons_are_given_when_both_apply(tmp_path):
    rag, _ = make(tmp_path, ("Never", PROJ, {"others_ok": "deny"}))
    got = resolve(rag, {"function": "meeting", "project": PROJ, "routed_to_cloud": True}, leaves_pc=LOCAL)
    assert got.blocked[0]["codes"] == ["cloud", "others"] and "cloud models" in got.blocked[0]["reason"] and "other people" in got.blocked[0]["reason"]


def test_a_missing_archive_is_reported_and_other_sources_add_their_ids(tmp_path):
    rag, (a,) = make(tmp_path, ("Boats", OTHER, {}))
    got = resolve(rag, {"function": "plan_node", "plan": "p1", "node": "n1"}, sources={"plan_node": lambda ctx: [a, "gone-123456"]}, leaves_pc=LOCAL)
    assert got.ids == [a] and got.archives[0]["via"] == "plan_node"
    assert got.blocked == [{"id": "gone-123456", "name": "gone-123456", "project_name": "", "codes": ["missing"], "reason": "no longer exists", "verdict": "deny"}]
    assert "gone" not in describe(got)
    routine = resolve(rag, {"function": "routine", "routine": "r1"}, sources={"routine": lambda ctx: [a]}, leaves_pc=LOCAL)
    assert routine.ids == [a]


def test_an_unknown_function_is_refused(tmp_path):
    rag, _ = make(tmp_path)
    with pytest.raises(RagError):
        resolve(rag, {"function": "chat"}, leaves_pc=LOCAL)


def test_the_route_of_a_function_comes_from_the_registry(tmp_path):
    class Reg:
        def __init__(self, chain, kinds):
            self.chain, self.kinds = chain, kinds

        def load(self):
            return {}

        def _find(self, data, pid):
            return {"type": self.kinds[pid]}

        def fallback_chain(self, function):
            assert function == "executor"
            return self.chain
    assert routes_outside("agent", Reg([None], {})) is False
    assert routes_outside("agent", Reg(["p"], {"p": "anthropic"})) is True
    assert routes_outside("meeting", Reg(["p"], {"p": "anthropic"})) is False          # the voice functions use the loaded model
    assert routes_outside("agent", object()) is True                                   # a registry that cannot be read counts as leaving


def test_the_remote_search_only_checks_that_archives_exist(tmp_path):
    rag, (a,) = make(tmp_path, ("Boats", PROJ, {}))
    seen = []
    rag._cache.clear()
    rag.remote = lambda ids, query, top_k, share: seen.append(ids) or {"hits": []}
    rag.search([a], "sloop", strict=True)
    assert seen == [[a]] and rag._cache == {}                                           # nothing was loaded
    with pytest.raises(RagError):
        rag.search(["gone-123456"], "sloop")
    assert rag.search([a, "gone-123456"], "sloop", strict=False) == {"hits": []} and seen[-1] == [a]


def test_others_ok_is_kept_in_meta_and_checked(tmp_path):
    rag, (a,) = make(tmp_path, ("Boats", PROJ, {}))
    assert rag.get(a)["others_ok"] == "ask_dialog" and rag.list()[0]["others_ok"] == "ask_dialog"
    assert rag.update(a, {"others_ok": "ask_voice"})["others_ok"] == "ask_voice" and rag.meta(a)["others_ok"] == "ask_voice"
    with pytest.raises(RagError):
        rag.update(a, {"others_ok": "yes"})
