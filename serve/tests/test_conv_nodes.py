"""Conversations as immutable nodes: a fork shares its start, nothing is copied, two branches merge into a synthesis."""
import json
import sys
import time
from pathlib import Path

import pytest

from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from serve.conv_nodes import NodeStore

STUB = str(Path(__file__).with_name("session_stub.py"))


def stub_command():
    return lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"]


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**kw):
        kw.setdefault("command", stub_command())
        kw.setdefault("tick_s", 0.1)
        mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", **kw)
        made.append(mgr)
        return mgr

    yield build
    for mgr in made:
        mgr.shutdown()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


def until(predicate, seconds=15, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def said(mgr, sid, *texts):
    """Writes turns straight into the history (no process): user and echo, as the stub would."""
    folder = mgr.history_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    data = mgr._read_history(sid) or {"session": sid, "turns": 0, "messages": [], "pending": []}
    for text in texts:
        data["messages"] += [{"role": "user", "content": text}, {"role": "assistant", "content": "echo: " + text}]
        data["turns"] += 1
    (folder / "history.json").write_text(json.dumps(data), encoding="utf-8")
    mgr._record(sid)["turns"] = data["turns"]


# -- the store ---------------------------------------------------------------------------------------------------

def test_the_same_message_after_the_same_history_is_one_node_and_a_chain_comes_back_whole(tmp_path):
    store = NodeStore(tmp_path / "n.jsonl")
    a = store.ingest([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    b = store.ingest([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}, {"role": "user", "content": "z"}])
    assert len(store) == 3 and store.path_to(b)[:2] == store.path_to(a)
    assert [m["content"] for m in store.chain(b)] == ["x", "y", "z"]
    other = store.ingest([{"role": "user", "content": "x"}, {"role": "assistant", "content": "different"}])
    assert store.common_ancestor(b, other) == store.path_to(a)[0] and len(store) == 4
    assert NodeStore(tmp_path / "n.jsonl").chain(b) == store.chain(b)                  # it survives a restart
    with pytest.raises(KeyError):
        store.add({"role": "user", "content": "q"}, ["nope"])


def test_a_torn_last_line_is_skipped_and_keep_drops_what_no_leaf_reaches(tmp_path):
    path = tmp_path / "n.jsonl"
    store = NodeStore(path)
    keep = store.ingest([{"role": "user", "content": "1"}, {"role": "user", "content": "2"}])
    lost = store.ingest([{"role": "user", "content": "1"}, {"role": "user", "content": "3"}])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"id": "half')
    assert len(NodeStore(path)) == 3
    assert store.keep([keep]) == 1 and store.get(lost) is None and len(NodeStore(path)) == 2


# -- a fork ---------------------------------------------------------------------------------------------------------

def test_a_branch_writes_no_copy_of_the_history_and_reads_the_same_messages(make, ws):
    mgr = make()
    sid = mgr.create(ws, title="plan")["id"]
    said(mgr, sid, "uno", "due", "tre")
    before = len(mgr.nodes)
    child = mgr.branch(sid, 2)
    assert not (mgr.history_dir(child["id"]) / "history.json").exists()
    assert [m["content"] for m in mgr.history(child["id"])["messages"]] == ["uno", "echo: uno", "due", "echo: due"]
    assert len(mgr.nodes) == before + 4                                                  # only the four shared messages, once
    again = mgr.branch(sid, 2)
    assert len(mgr.nodes) == before + 4 and again["node"] == child["node"]               # a second fork of the same point adds nothing


def test_a_branch_gets_its_file_when_it_first_runs_and_goes_on_from_the_shared_start(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    said(mgr, sid, "uno", "due")
    child = mgr.branch(sid, 1)["id"]
    mgr.send(child, "altro")
    until(lambda: mgr.get(child)["state"] == "idle")
    assert [m["content"] for m in mgr.history(child)["messages"]] == ["uno", "echo: uno", "altro", "echo: altro"]
    assert [m["content"] for m in mgr.history(sid)["messages"]][-1] == "echo: due"       # the first is untouched
    mgr.sync_node(child)
    assert mgr.nodes.common_ancestor(mgr.get(child)["node"], mgr.sync_node(sid)) == mgr.nodes.path_to(mgr.branch(sid, 1)["node"])[-1]


def test_deleting_a_conversation_drops_its_own_nodes_and_keeps_those_a_branch_reaches(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    said(mgr, sid, "uno", "due", "tre")
    child = mgr.branch(sid, 1)["id"]
    mgr.sync_node(sid)
    mgr.delete(sid)
    assert [m["content"] for m in mgr.history(child)["messages"]] == ["uno", "echo: uno"]
    assert len(mgr.nodes) == 2 and all("due" not in json.dumps(n) for n in mgr.nodes.all())


# -- the conversations that were there before -----------------------------------------------------------------------

def old_style(tmp_path, ws, order):
    """A manager folder as it was: a branch is a full copy of its parent's history."""
    mgr = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    root = mgr.create(ws, title="main")["id"]
    said(mgr, root, "uno", "due", "tre")
    kid = mgr.create(ws, title="kid")["id"]
    messages = json.loads((mgr.history_dir(root) / "history.json").read_text(encoding="utf-8"))["messages"][:4]
    (mgr.history_dir(kid)).mkdir(parents=True, exist_ok=True)
    (mgr.history_dir(kid) / "history.json").write_text(json.dumps({"session": kid, "turns": 2, "saved": 1, "messages": messages, "pending": []}), encoding="utf-8")
    mgr._record(kid).update(branched_from={"session": root, "turn": 2}, turns=2, state="dormant")
    mgr._save()
    mgr.shutdown()
    (mgr.directory / "nodes.jsonl").unlink(missing_ok=True)
    (mgr.directory / "nodes.migrated").unlink(missing_ok=True)
    return root, kid


def test_old_branches_are_moved_into_the_node_tree_and_their_copy_is_dropped(make, tmp_path, ws):
    root, kid = old_style(tmp_path, ws, None)
    mgr = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    try:
        assert not (mgr.history_dir(kid) / "history.json").exists() and (mgr.history_dir(root) / "history.json").exists()
        assert [m["content"] for m in mgr.history(kid)["messages"]] == ["uno", "echo: uno", "due", "echo: due"]
        assert len(mgr.nodes) == 6                                                      # the branch's four were the root's first four
        assert mgr.get(kid)["node"] in mgr.nodes.path_to(mgr.get(root)["node"])
        assert (mgr.directory / "nodes.migrated").exists()
    finally:
        mgr.shutdown()


def test_the_migration_gives_the_same_ids_whatever_the_order_and_keeps_a_branch_that_went_on(make, tmp_path, ws):
    root, kid = old_style(tmp_path, ws, None)
    # the branch has run since: its file holds more than the store would rebuild, so it is kept
    extra = json.loads((tmp_path / "old" / "sessions" / kid / "history.json").read_text(encoding="utf-8"))
    extra["messages"].append({"role": "system", "content": "prompt"})
    (tmp_path / "old" / "sessions" / kid / "history.json").write_text(json.dumps(extra), encoding="utf-8")
    first = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    ids = sorted(n["id"] for n in first.nodes.all())
    first.shutdown()
    assert (tmp_path / "old" / "sessions" / kid / "history.json").exists()
    (tmp_path / "old" / "sessions" / "nodes.jsonl").unlink()
    (tmp_path / "old" / "sessions" / "nodes.migrated").unlink()
    first = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    first.records.reverse()
    try:
        assert sorted(n["id"] for n in first.nodes.all()) == ids
    finally:
        first.shutdown()


# -- changing branch, merging, the picture --------------------------------------------------------------------------

def test_the_cost_of_changing_branch_counts_what_the_two_share(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    said(mgr, sid, "a" * 350, "b" * 350)
    child = mgr.branch(sid, 1)["id"]
    cold = mgr.switch_cost(child)
    assert cold["tokens"] == int(350 / 3.5) + int(356 / 3.5) and cold["shared_with_current"] == 0 and cold["if_cache_cold"] == cold["tokens"]
    warm = mgr.switch_cost(child, sid)
    assert warm["shared_with_current"] == warm["tokens"] and warm["if_cache_warm"] == 0
    back = mgr.switch_cost(sid, child)
    assert back["tokens"] > back["shared_with_current"] > 0 and back["if_cache_warm"] == back["tokens"] - back["shared_with_current"]
    with pytest.raises(RunError):
        mgr.switch_cost("ghost")


class FakeClient:
    def __init__(self, text="Both agree on X; A also found Y."):
        self.calls, self.text = [], text

    def chat_completion(self, **kw):
        self.calls.append(kw)
        return {"choices": [{"message": {"content": self.text}}]}


def fork_two(mgr, ws):
    sid = mgr.create(ws, title="main")["id"]
    said(mgr, sid, "uno")
    other = mgr.branch(sid, 1)["id"]
    said(mgr, sid, "path A")
    said(mgr, other, "path B")
    return sid, other


def test_merging_two_branches_writes_a_synthesis_after_both_and_changes_neither(make, ws):
    mgr = make()
    sid, other = fork_two(mgr, ws)
    fake = FakeClient()
    mgr._skill_client = lambda: fake
    out = mgr.merge(sid, other, "Which is better?")
    prompt = fake.calls[0]["messages"][1]["content"]
    assert "path A" in prompt and "path B" in prompt and "Which is better?" in prompt and prompt.count("uno") >= 1
    msgs = mgr.history(out["id"])["messages"]
    assert [m["role"] for m in msgs][-2:] == ["user", "assistant"] and msgs[-1]["content"] == fake.text and msgs[-2]["content"] == "Which is better?"
    assert [m["content"] for m in msgs[:2]] == ["uno", "echo: uno"] and "path A" in [m["content"] for m in msgs]      # it continues the first
    node = mgr.nodes.get(mgr.nodes.path_to(out["node"])[-2])
    assert node["parents"] == [mgr.get(sid)["node"], mgr.get(other)["node"]]
    assert out["merged_from"] == [sid, other] and out["branched_from"]["session"] == sid
    assert [m["content"] for m in mgr.history(other)["messages"]][-1] == "echo: path B"
    assert out["turns"] == 3


def test_a_merge_needs_two_different_idle_conversations_with_something_to_merge_and_a_model(make, ws):
    mgr = make()
    sid, other = fork_two(mgr, ws)
    for a, b in ((sid, sid), (sid, 5), ("ghost", sid)):
        with pytest.raises(RunError):
            mgr.merge(a, b)
    twin = mgr.branch(sid, 1)["id"]
    same = mgr.branch(sid, 1)["id"]
    with pytest.raises(RunError, match="nothing to merge"):
        mgr.merge(twin, same)
    mgr._skill_client = lambda: FakeClient("  ")
    with pytest.raises(RunError, match="no synthesis"):
        mgr.merge(sid, other)
    mgr._record(sid)["state"] = "busy"
    with pytest.raises(RunError, match="Wait"):
        mgr.merge(sid, other)


def test_the_node_tree_has_one_entry_per_run_and_marks_forks_leaves_and_merges(make, ws):
    mgr = make()
    sid, other = fork_two(mgr, ws)
    mgr._skill_client = lambda: FakeClient()
    merged = mgr.merge(sid, other)["id"]
    mgr.go_back(sid, 1)
    tree = mgr.node_tree()
    assert tree["nodes"] == len(mgr.nodes) and len(tree["roots"]) == 1
    top = tree["roots"][0]
    assert top["messages"] == 2 and top["request"] == "uno" and len(top["children"]) == 2          # one shared start, two ways on
    assert sid in [s["id"] for s in top["sessions"]]                                               # the first went back to this leaf
    by = {s["id"]: run for run in _walk(top) for s in run["sessions"]}
    assert other in by and merged in by
    assert any(run["merges"] for run in _walk(top))
    assert any(not run["sessions"] and run["children"] == [] for run in _walk(top))               # what going back left has no conversation


def _walk(run):
    yield run
    for c in run["children"]:
        yield from _walk(c)


def test_the_page_draws_the_nodes_and_every_text_it_uses_is_in_english_and_italian():
    import re
    web = Path(__file__).resolve().parents[1] / "web"
    js = (web / "branches.js").read_text(encoding="utf-8")
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'T\("(br\.(?:nodes|cost|merge)\.[a-z.]+)"', js)) | set(re.findall(r'data-t="(br\.(?:nodes|merge)\.[a-z.]+)"', (web / "index.html").read_text(encoding="utf-8")))
    assert len(keys) >= 20
    for key in keys:
        assert i18n.count(f'"{key}":') == 2, key
    assert '"/nodes"' in js and "/switch-cost?current=" in js and '"/merge"' in js
