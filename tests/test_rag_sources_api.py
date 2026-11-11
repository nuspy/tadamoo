"""The refresh over the API: the sources, a check, a preview, an update, the verify and the versions as jobs, the settings, the routine that looks after an archive, and the
attention mark."""
import threading
import time

import pytest

from serve.rag_api import RagApi
from serve.rag_sync_api import attention
from sources_support import Counting, body, doc_of, make
from tadamoo.rag import RagError
from tadamoo.scheduler import RoutineError, Scheduler, SessionRoutineRunner


def put(folder, name, text, mtime):
    import os
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(text, encoding="utf-8")
    os.utime(p, (mtime, mtime))


@pytest.fixture
def api(tmp_path):
    store, sync, aid = make(tmp_path, Counting())
    src = tmp_path / "docs"
    put(src, "alpha.md", body(1, "alpha"), 1_700_000_000)
    put(src, "beta.md", body(1, "beta"), 1_700_000_000)
    return RagApi(store, lambda: [], sync=sync), aid, src, store, sync


def run(api, route, parts, req=None):
    """A POST that answers a job: the job's result once it has ended."""
    out = api.route_post(parts, {}, __import__("json").dumps(req or {}).encode())
    job = api.jobs.wait(out["job"]["id"])
    assert job["state"] == "done", job
    return job["result"]


def test_the_whole_cycle_over_the_routes(api):
    api, aid, src, store, sync = api
    got = api.route_post(["archives", aid, "sources"], {}, b'{"kind": "folder", "path": "%s", "label": "Docs"}' % str(src).replace("\\", "\\\\").encode())
    assert got["source"]["label"] == "Docs" and got["source"]["config"]["recursive"] is True
    check = run(api, "check", ["archives", aid, "check"])
    assert check["counts"] == {"new": 2}
    preview = run(api, "refresh", ["archives", aid, "refresh"], {"dry_run": True})
    assert preview["mode"] == "preview" and preview["counts"] == {"would add": 2} and store.get(aid)["documents"] == 0
    done = run(api, "refresh", ["archives", aid, "refresh"])
    assert done["counts"] == {"added": 2}
    view = api.route_get(["archives", aid, "sync"])
    assert len(view["links"]) == 2 and view["unlinked"] == 0 and view["last"]["summary"] == "2 added" and view["coverage"]["ok"] == 2 and view["settings"]["keep_versions"] == 3
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_900)
    only = run(api, "refresh", ["archives", aid, "refresh"], {"only": [doc_of(store, aid, "beta")["id"]]})
    assert only["counts"] == {"changed": 1, "unchanged": 1}                    # alpha changed but was not chosen: it waits
    assert run(api, "refresh", ["archives", aid, "refresh"])["counts"] == {"updated": 1, "unchanged": 1}
    alpha = doc_of(store, aid, "alpha")["id"]
    assert [v["n"] for v in api.route_get(["archives", aid, "documents", alpha, "versions"])["versions"]] == [1]
    back = run(api, "restore", ["archives", aid, "documents", alpha, "rollback"], {"version": 1})
    assert back["restored"] == 1
    ok = run(api, "verify", ["archives", aid, "verify"], {"deep": True})
    assert ok["ok"] and ok["deep"] and ok["coverage"]["status"] == "ok"


def test_bad_requests_are_refused_with_the_reason(api):
    api, aid, src, store, sync = api
    post = lambda parts, req: api.route_post(parts, {}, __import__("json").dumps(req).encode())
    for parts, req, why in ((["archives", aid, "sources"], {"kind": "ftp"}, "kind"), (["archives", aid, "documents", "dabcdef01", "rollback"], {"version": "x"}, "version"),
                            (["sync", "settings"], {"keep_versions": 99}, "keep_versions"), (["sync", "settings"], {"nonsense": 1}, "unknown field"),
                            (["archives", aid, "refresh"], {"dry_run": "yes"}, "dry_run"), (["archives", aid, "refresh"], {"only": "all"}, "only")):
        with pytest.raises(RagError, match=why):
            post(parts, req)
    out = post(["archives", aid, "check"], {})
    job = api.jobs.wait(out["job"]["id"])
    assert job["state"] == "failed" and "no source" in job["error"]
    with pytest.raises(RagError, match="no such archive"):
        api.route_get(["archives", "nope", "sync"])


def test_the_settings_are_read_and_written(api):
    api, aid, src, store, sync = api
    assert api.route_get(["sync", "settings"])["settings"]["respect_robots"] is True
    got = api.route_post(["sync", "settings"], {}, b'{"keep_versions": 5, "contact": "https://example.org/about", "respect_robots": true}')
    assert got["settings"]["keep_versions"] == 5 and got["settings"]["contact"] == "https://example.org/about"


def test_a_second_run_on_the_same_archive_is_refused_while_one_goes(api):
    api, aid, src, store, sync = api
    api.route_post(["archives", aid, "sources"], {}, __import__("json").dumps({"kind": "folder", "path": str(src)}).encode())
    gate = threading.Event()
    sync.busy = lambda: "" if gate.is_set() else "busy"
    sync.settings.write({"wait_minutes": 20})
    sync.sleep = lambda s: time.sleep(0.01)
    out = api.route_post(["archives", aid, "refresh"], {}, b"{}")
    time.sleep(0.2)
    with pytest.raises(RagError) as e:
        api.route_post(["archives", aid, "check"], {}, b"{}")
    assert e.value.code == "busy" and api.route_get(["archives", aid, "sync"])["running"]
    gate.set()
    assert api.jobs.wait(out["job"]["id"], 20)["state"] == "done"


def test_a_scheduled_check_reports_in_the_words_of_a_run(api):
    api, aid, src, store, sync = api
    sync.add_source(aid, "folder", {"path": str(src)})
    s = api.syncing
    first = s.scheduled(aid, "check", threading.Event())
    assert first["status"] == "done" and first["outcome"] == "progress" and "2 new" in first["summary"]
    assert store.get(aid)["documents"] == 0                                    # a check changes nothing
    updated = s.scheduled(store.get(aid)["name"].upper(), "update", threading.Event())          # by name, in any case
    assert updated["outcome"] == "progress" and "2 added" in updated["summary"] and store.get(aid)["documents"] == 2
    quiet = s.scheduled(aid, "update", threading.Event())
    assert quiet["status"] == "done" and quiet["outcome"] == "nothing_new" and "nothing to update" in quiet["summary"]
    assert s.scheduled("no such archive", "check", threading.Event())["status"] == "failed"
    sync.busy = lambda: "a heavy job has the local model out"
    sync.settings.write({"wait_minutes": 0})
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_900)
    skipped = s.scheduled(aid, "update", threading.Event())
    assert skipped["status"] == "skipped" and "heavy job" in skipped["summary"] and "next time" in skipped["summary"]
    import shutil
    shutil.rmtree(src)
    broken = s.scheduled(aid, "check", threading.Event())
    assert broken["status"] == "failed" and "folder" in broken["summary"]


def test_attention_names_the_latest_news(api, tmp_path):
    api, aid, src, store, sync = api
    assert attention(store.root)["t"] == 0
    sync.add_source(aid, "folder", {"path": str(src)})
    sync.check(aid)
    got = attention(store.root)
    assert got["t"] > 0 and got["archive"] == aid and got["counts"] == {"new": 2}


# -- the routine ---------------------------------------------------------------------------------

def test_a_knowledge_routine_needs_an_archive_and_check_or_update(tmp_path):
    sched = Scheduler(tmp_path / "j", SessionRoutineRunner(None), tick_s=3600)
    ok = sched.add("Wiki nightly", "check the company wiki", "", "daily 03:00", deliver_to={"kind": "knowledge", "ref": "wiki-1a2b3c"}, message="update")
    assert ok["deliver_to"] == {"kind": "knowledge", "ref": "wiki-1a2b3c"} and ok["message"] == "update"
    for bad in ({"deliver_to": {"kind": "knowledge", "ref": ""}, "message": "check"}, {"deliver_to": {"kind": "knowledge", "ref": "x"}, "message": "delete everything"},
                {"deliver_to": {"kind": "knowledge", "ref": "x"}, "message": ""}):
        with pytest.raises(RoutineError):
            sched.add("x", "x", "", "daily 03:00", **bad)


def test_a_knowledge_routine_runs_without_a_model_and_leaves_its_outcome(tmp_path):
    calls = []
    runner = SessionRoutineRunner(None)
    runner.knowledge = lambda ref, action, cancel: calls.append((ref, action)) or {"status": "done", "outcome": "progress", "summary": "3 updated"}
    sched = Scheduler(tmp_path / "j", runner, tick_s=3600, poll_s=0.02)
    rid = sched.add("Nightly", "update the wiki archive", "", "every 1h", deliver_to={"kind": "knowledge", "ref": "wiki"}, message="update")["id"]
    sched.start()
    sched.check(sched.clock() + 4000)
    end = time.time() + 10
    while time.time() < end and not sched.runs(rid):
        time.sleep(0.02)
    sched.shutdown()
    run_ = sched.runs(rid)[0]
    assert calls == [("wiki", "update")] and run_["status"] == "done" and run_["outcome"] == "progress" and run_["summary"] == "3 updated" and not run_["session"]
    bare = SessionRoutineRunner(None)
    out = bare.run({"id": "r", "deliver_to": {"kind": "knowledge", "ref": "wiki"}, "message": "check", "prompt": "x"}, threading.Event())
    assert out["status"] == "failed" and "not available" in out["summary"]
