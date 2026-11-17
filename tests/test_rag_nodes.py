"""What the person does on the tree: switch a node off and on, give it a schedule, remove it, read it again, and read what is due. Real sources over real folders."""
import time

import pytest

from sources_support import body
from test_rag_sources_local import folder_archive, put, states
from tadamoo.rag import RagError
from tadamoo.rag.sources import nodes, tree


def ids(view):
    return [n["id"] for n, _ in tree.walk(view["tree"])]


def node_of(view, label):
    return next(n for n, _ in tree.walk(view["tree"]) if n["label"] == label)


def test_a_directory_switched_off_is_neither_checked_nor_updated_and_its_documents_come_back_when_it_is_switched_on(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    view = nodes.view(sync, aid)
    assert ids(view)[:2] == ["k:folder", f"s:{sid}"] and f"d:{sid}:sub" in ids(view) and all(n["state"] == "on" for n, _ in tree.walk(view["tree"]))
    sub = f"d:{sid}:sub"
    off = nodes.set_follow(sync, aid, sub, False)
    assert node_of(off, "sub")["state"] == "off" and node_of(off, "sub - gamma.txt")["state"] == "off" and node_of(off, "docs")["state"] == "mixed" and node_of(off, "alpha.md")["state"] == "on"
    put(src, "sub/gamma.txt", "Gamma changed. " * 40, 1_700_005_000)
    put(src, "alpha.md", body(7, "alpha"), 1_700_005_000)
    states_now = states(sync.check(aid))
    assert states_now["sub - gamma.txt"] == "unfollowed" and states_now["alpha.md"] == "changed"
    done = sync.update(aid)
    assert done["counts"].get("updated") == 1 and "unfollowed" in done["counts"]
    nodes.set_follow(sync, aid, sub, True)
    assert states(sync.check(aid))["sub - gamma.txt"] == "changed"
    with pytest.raises(RagError):
        nodes.set_follow(sync, aid, sub, "yes")
    with pytest.raises(RagError, match="no such node"):
        nodes.set_follow(sync, aid, "d:nothing", False)


def test_removing_a_directory_or_a_document_takes_its_documents_out_and_the_next_check_does_not_bring_them_back(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    sub = f"d:{sid}:sub"
    with pytest.raises(RagError, match="confirmation"):
        nodes.remove_node(sync, aid, sub, False)
    got = nodes.remove_node(sync, aid, sub, True)
    assert got["removed_documents"] == 1 and "sub - gamma.txt" not in [n["label"] for n, _ in tree.walk(got["tree"])] and store.get(aid)["documents"] == 2
    put(src, "sub/gamma.txt", "Gamma is new again. " * 30, 1_700_006_000)
    put(src, "sub/delta.txt", "Delta is new too. " * 30, 1_700_006_000)
    assert "sub - gamma.txt" not in states(sync.check(aid)) and "sub - delta.txt" not in states(sync.check(aid))                     # nothing below a removed directory is read again
    alpha = next(n for n, _ in tree.walk(nodes.view(sync, aid)["tree"]) if n["label"] == "alpha.md")
    nodes.remove_node(sync, aid, alpha["id"], True)
    assert store.get(aid)["documents"] == 1 and "alpha.md" not in states(sync.check(aid))
    only = nodes.remove_node(sync, aid, f"s:{sid}", True)                                                                  # the source with its last document
    assert only["removed_documents"] == 1 and store.get(aid)["documents"] == 0 and sync.sources(aid)["sources"] == []


def test_reading_a_node_again_reads_only_what_is_below_it(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    put(src, "sub/gamma.txt", "Gamma changed. " * 40, 1_700_005_000)
    put(src, "alpha.md", body(7, "alpha"), 1_700_005_000)
    report = nodes.refresh_node(sync, aid, f"d:{sid}:sub")
    assert report["counts"].get("updated") == 1
    now = states(sync.check(aid))
    assert now["alpha.md"] == "changed" and now.get("sub - gamma.txt") == "unchanged"                                            # alpha was left for later
    assert node_of(nodes.view(sync, aid), "sub")["last"] > 0                                      # the node remembers when it was read
    put(src, "sub/fresh.txt", "A new file in the directory. " * 30, 1_700_007_000)
    assert nodes.refresh_node(sync, aid, f"s:{sid}")["counts"].get("added") == 1


def test_a_schedule_is_set_with_checks_and_what_is_due_is_read_once(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    day = {"mode": "interval", "every": 1, "unit": "days"}
    source = f"s:{sid}"
    got = nodes.set_frequency(sync, aid, source, day)
    n = node_of(got, "docs")
    assert n["freq"] == day and n["since"] > 0
    for bad in ({"mode": "interval", "every": 0, "unit": "days"}, {"mode": "sometimes"}, "daily"):
        with pytest.raises(RagError):
            nodes.set_frequency(sync, aid, source, bad)
    put(src, "alpha.md", body(8, "alpha"), 1_700_008_000)
    start = time.time()
    assert nodes.run_due(sync, aid, now=start + 3600)["nodes"] == []                                                         # not yet: a day has not passed
    ran = nodes.run_due(sync, aid, now=start + 90_000)
    assert ran["nodes"] == [source] and ran["counts"].get("updated") == 1 and "1 node(s) read again" in ran["summary"]
    assert nodes.run_due(sync, aid, now=start + 90_100)["nodes"] == []                                                        # read once: the clock starts again from then
    nodes.set_follow(sync, aid, source, False)
    assert nodes.run_due(sync, aid, now=start + 400_000)["nodes"] == []                                                       # a node that is off is never due
    assert nodes.set_frequency(sync, aid, source, {"mode": "manual"})["tree"][0]["children"][0]["freq"] == {"mode": "manual"}


def test_documents_added_by_hand_sit_apart_and_have_no_switch_schedule_or_source_to_read_again(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    p = tmp_path / "hand.txt"
    p.write_text("A note added by hand. " * 30, encoding="utf-8")
    store.add_file(aid, "hand.txt", p)
    view = nodes.view(sync, aid)
    hand = node_of(view, "hand.txt")
    assert hand["hand"] and view["tree"][-1]["id"] == tree.HAND and view["tree"][-1]["children"][0]["id"] == hand["id"]
    for call in (lambda: nodes.set_follow(sync, aid, hand["id"], False), lambda: nodes.set_frequency(sync, aid, hand["id"], {"mode": "manual"}), lambda: nodes.refresh_node(sync, aid, hand["id"])):
        with pytest.raises(RagError):
            call()
    assert nodes.remove_node(sync, aid, hand["id"], True)["removed_documents"] == 1 and store.get(aid)["documents"] == 3
