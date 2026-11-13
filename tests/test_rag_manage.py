"""Building an archive from a conversation: adding files, folders and web addresses in a batch (the server's job), the state of an archive, and the four tools that ask for them."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serve" / "tests"))

from serve.rag_api import RagApi
from sources_support import PROJ, Counting, Site, body, html_page, make
from tadamoo.rag import RagError
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.manage_tools import register_manage_tools
from tadamoo.tools.registry import EffectClass, ToolRegistry


@pytest.fixture
def world(tmp_path):
    store, sync, aid = make(tmp_path, Counting())
    api = RagApi(store, sync=sync)
    site = Site()
    yield store, sync, api, aid, site, tmp_path
    site.close()
    store.router.close()


def finish(api, job, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        got = api.route_get(["jobs", job["id"]])["job"]
        if got["state"] != "running":
            return got
        time.sleep(0.02)
    raise AssertionError("the job did not end")


def add(api, aid, items):
    job = api.route_post(["archives", aid, "add"], {}, json.dumps({"items": items}).encode())["job"]
    done = finish(api, job)
    assert done["state"] == "done", done["error"]
    return done["result"]


def test_an_address_is_downloaded_into_the_archives_folder_read_and_added_with_where_it_came_from(world):
    store, sync, api, aid, site, tmp_path = world
    site.pages["/wind.html"] = html_page("Wind notes", 6, "wind")
    site.pages["/delta.md"] = body(1, "delta").encode()
    site.types["/delta.md"] = "text/markdown"
    out = add(api, aid, [site.url + "/wind.html", site.url + "/delta.md"])
    assert out["counts"] == {"added": 2} and out["documents"] == 2 and out["chunks"] > 0
    files = sorted(p.name for p in (store.root / "downloads" / aid).iterdir())
    assert len(files) == 2 and all(not f.startswith(".part") for f in files)
    docs = {d["name"]: d for d in store.get(aid)["document_list"]}
    assert any(d["origin"]["kind"] == "url" and d["origin"]["ref"].startswith(site.url) for d in docs.values())
    again = add(api, aid, [site.url + "/wind.html"])
    assert again["counts"] == {"duplicate": 1} and again["results"][0]["note"].startswith("this file is already") and store.get(aid)["documents"] == 2


def test_what_cannot_be_added_says_why_and_the_rest_goes_on(world):
    store, sync, api, aid, site, tmp_path = world
    site.pages["/ok.html"] = html_page("Fine", 5)
    site.pages["/private/secret.html"] = html_page("Secret", 5)
    site.pages["/blob.bin"] = b"\x00\x01\x02" * 100
    site.types["/blob.bin"] = "application/octet-stream"
    out = add(api, aid, [site.url + "/missing.html", site.url + "/private/secret.html", site.url + "/blob.bin", str(tmp_path / "no-such-file.pdf"), site.url + "/ok.html", "ftp://example.org/x.pdf"])
    by = {r["item"].replace("\\", "/").rsplit("/", 1)[-1]: r for r in out["results"]}
    assert out["counts"] == {"failed": 5, "added": 1} and by["ok.html"]["status"] == "added"
    assert "HTTP 404" in by["missing.html"]["note"] and "robots" in by["secret.html"]["note"] and "not a format" in by["blob.bin"]["note"] and "no such file" in by["no-such-file.pdf"]["note"].lower()
    assert store.get(aid)["documents"] == 1
    assert not [p for p in (store.root / "downloads" / aid).iterdir() if "blob" in p.name or p.name.startswith(".part")]          # a file that cannot be read is not kept


def test_a_file_on_this_pc_is_added_and_a_folder_becomes_a_source_that_can_be_updated_later(world):
    store, sync, api, aid, site, tmp_path = world
    one = tmp_path / "docs" / "alpha.md"
    one.parent.mkdir()
    one.write_text(body(1, "alpha"), encoding="utf-8")
    (tmp_path / "docs" / "sub").mkdir()
    (tmp_path / "docs" / "sub" / "beta.md").write_text(body(1, "beta"), encoding="utf-8")
    single = tmp_path / "single.md"
    single.write_text(body(1, "gamma"), encoding="utf-8")
    out = add(api, aid, [str(single), str(tmp_path / "docs")])
    assert [r["status"] for r in out["results"]] == ["added", "added"] and out["results"][1]["counts"] == {"added": 2} and out["documents"] == 3
    assert [s["kind"] for s in sync.sources(aid)["sources"]] == ["folder"] and "knowledge_update" in out["results"][1]["note"]
    (tmp_path / "docs" / "new.md").write_text(body(1, "delta"), encoding="utf-8")
    assert sync.check(aid)["counts"] == {"new": 1, "unchanged": 2}
    again = add(api, aid, [str(tmp_path / "docs")])
    assert len(sync.sources(aid)["sources"]) == 1 and store.get(aid)["documents"] == 4                 # the same folder is the same source


def test_the_items_are_checked_before_anything_starts(world):
    store, sync, api, aid, site, tmp_path = world
    for bad in ([], "x", [1], [" "], ["a"] * 0, ["x" * 1001], [f"https://e.org/{i}" for i in range(201)]):
        with pytest.raises(RagError, match="items"):
            api.route_post(["archives", aid, "add"], {}, json.dumps({"items": bad}).encode())
    with pytest.raises(RagError):
        api.route_post(["archives", "nothing-000000", "add"], {}, json.dumps({"items": ["x"]}).encode())


def test_the_status_says_what_is_in_the_archive_and_what_runs(world):
    store, sync, api, aid, site, tmp_path = world
    site.pages["/a.html"] = html_page("A page", 6)
    add(api, aid, [site.url + "/a.html"])
    s = api.route_get(["archives", aid, "status"])
    assert s["archive"]["documents"] == 1 and s["archive"]["mode"] == "hybrid" and s["documents"][0]["from"].startswith(site.url) and s["documents"][0]["passages"] > 0
    assert s["database"]["backend"] == "sqlite_vec" and s["running"] == [] and s["recent"][0]["state"] == "done" and s["sources"] == []
    store.delete(aid, True)
    assert not (store.root / "downloads" / aid).exists()                                              # what was downloaded for an archive goes with it


# -- the tools ------------------------------------------------------------------------------------------------------------------

def stand_in(api):
    """Stands in for the server for the tools: the job runs and its result comes back, like `server_call`."""
    def call(path, req):
        parts = path.split("/")
        job = api.route_post(parts, {}, json.dumps(req).encode())["job"]
        done = finish(api, job)
        if done["state"] != "done":
            raise RuntimeError(done["error"])
        return done["result"]

    def ask(method, path, body=None):
        parts = path.split("/")
        out = api.route_get(parts) if method == "GET" else api.route_post(parts, {}, json.dumps(body or {}).encode())
        return out
    call.ask = ask
    return call


def tools(world, local=True, call="stand-in"):
    store, sync, api, aid, site, tmp_path = world
    recall = ProjectRecall(store, PROJ, local=lambda: local)
    registry = ToolRegistry()
    register_manage_tools(registry, store, recall, stand_in(api) if call == "stand-in" else call)
    return registry, recall


def test_the_tools_are_described_and_have_the_right_levels(world):
    registry, _ = tools(world)
    assert registry.get("knowledge_status").effect == EffectClass.PURE
    for name in ("knowledge_create", "knowledge_add", "knowledge_index"):
        assert registry.get(name).effect == EffectClass.REVERSIBLE
    assert registry.get("knowledge_add").untrusted and "cerca tutti i brevetti" in registry.get("knowledge_add").description and "search_web" in registry.get("knowledge_add").description
    from tadamoo.roles import ALWAYS_TOOLS, BUILD_TOOLS
    assert "knowledge_status" in ALWAYS_TOOLS and {"knowledge_create", "knowledge_add", "knowledge_index"} <= set(BUILD_TOOLS) and "knowledge_add" not in ALWAYS_TOOLS


def test_a_conversation_makes_an_archive_adds_a_batch_and_reports_how_it_stands(world):
    store, sync, api, aid, site, tmp_path = world
    registry, recall = tools(world)
    made = registry.execute("knowledge_create", {"name": "Patents on delta", "description": "delta patents"})
    assert made["status"] == "created" and store.get(made["archive"]["id"])["project"] == PROJ
    assert registry.execute("knowledge_create", {"name": "patents on delta"})["status"] == "exists"
    site.pages["/p1.html"] = html_page("Patent one", 6)
    site.pages["/p2.html"] = html_page("Patent two", 6, "delta")
    out = registry.execute("knowledge_add", {"archive": "patents on delta", "items": [site.url + "/p1.html", site.url + "/p2.html", site.url + "/gone.html"]})
    assert out["status"] == "ok" and out["counts"] == {"added": 2, "failed": 1} and out["documents_in_archive"] == 2 and out["search"] == "hybrid"
    status = registry.execute("knowledge_status", {"archive": "patents on delta"})
    row = status["archives"][0]
    assert row["documents"] == 2 and row["search"] == "by meaning and words" and row["running"] == [] and row["documents_list"][0]["passages"] > 0 and row["described"] == "not described"
    everything = registry.execute("knowledge_status", {})
    assert {r["archive"] for r in everything["archives"]} == {"Sync test", "Patents on delta"}


def test_with_no_name_the_only_archive_of_the_project_is_used_and_with_several_the_person_is_asked(world):
    store, sync, api, aid, site, tmp_path = world
    registry, _ = tools(world)
    site.pages["/p.html"] = html_page("Page", 6)
    assert registry.execute("knowledge_add", {"items": [site.url + "/p.html"]})["archive"] == "Sync test"
    store.create("Second", "", PROJ)
    asked = registry.execute("knowledge_add", {"items": [site.url + "/p.html"]})
    assert asked["status"] == "ask" and len(asked["candidates"]) == 2
    assert registry.execute("knowledge_add", {"archive": "nothing like it", "items": ["x"]})["status"] == "none"
    assert registry.execute("knowledge_add", {"archive": "second", "items": []})["status"] == "error"


def test_indexing_again_runs_and_an_archive_that_may_not_leave_this_pc_is_refused(world):
    store, sync, api, aid, site, tmp_path = world
    registry, _ = tools(world)
    site.pages["/p.html"] = html_page("Page", 6)
    registry.execute("knowledge_add", {"items": [site.url + "/p.html"]})
    done = registry.execute("knowledge_index", {"archive": "sync test"})
    assert done["status"] == "ok" and done["indexed"]["passages"] == store.get(aid)["chunks"]
    cloud, _ = tools(world, local=False)
    for name, args in (("knowledge_add", {"archive": "sync test", "items": ["x"]}), ("knowledge_index", {"archive": "sync test"}), ("knowledge_status", {"archive": "sync test"})):
        assert cloud.execute(name, args)["status"] == "not_allowed"
    store.update(aid, {"cloud_ok": True})
    assert cloud.execute("knowledge_status", {"archive": "sync test"})["status"] == "ok"


def test_without_a_server_on_this_pc_the_tools_say_so(world):
    registry, _ = tools(world, call=None)
    for name, args in (("knowledge_create", {"name": "x"}), ("knowledge_status", {}), ("knowledge_add", {"archive": "sync test", "items": ["x"]})):
        assert registry.execute(name, args)["status"] == "error"


def test_a_job_that_outlives_the_wait_is_reported_as_running(world):
    registry, _ = tools(world, call=lambda path, req: {"running": "j1"})
    out = registry.execute("knowledge_add", {"archive": "sync test", "items": ["x"]})
    assert out["status"] == "running" and "knowledge_status" in out["note"]
