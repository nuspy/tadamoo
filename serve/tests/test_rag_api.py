"""The Knowledge tab's server side: the archives of a project over the API, the routes (this PC only), and the harness's flags."""
import json
import re
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.projects import ProjectStore
from serve.plans import PlanService
from serve.rag_api import RagApi, upload_limit
from serve.service import Service
from serve.changes import ChangeJournal
from serve.util.text import ByteTokenizer
from tadamoo.rag import RagError, RagStore, project_key

ROOT = Path(__file__).resolve().parents[2]
BOATS = b"A sloop has one mast and two sails. The keel keeps the boat upright and stops it sliding sideways. To tack, turn the bow through the wind."
RICE = b"Risotto needs short grain rice, hot stock and patience. Stir often and add the stock a ladle at a time."


PROJ = project_key("project-one")
OTHER = project_key("project-two")


def make(tmp_path, embedder=None):
    folders = [{"name": "Chat", "path": str(tmp_path / "chat"), "chat": True}, {"name": "Work", "path": str(tmp_path / "work"), "chat": False}]
    return RagApi(RagStore(tmp_path / "rag", embedder=embedder or (lambda: None), forbidden=[tmp_path / "personas"]), lambda: folders)


def up(api, aid, name, data):
    """Adds a document and waits for the job: its result (`document`, `embedded`, `note`)."""
    job = api.upload(aid, name, data)["job"]
    done = api.jobs.wait(job["id"])
    assert done["state"] == "done", done
    return done["result"]


def new(api, name, project=PROJ, **more):
    return api.create({"name": name, "project": project, **more})


def test_an_archive_is_made_filled_asked_and_deleted(tmp_path):
    api = make(tmp_path)
    made = new(api, "Boats", description="About sailing")["archive"]
    aid = made["id"]
    assert made["documents"] == 0 and made["mode"] == "text" and made["project"] == PROJ and api.overview(PROJ)["embedding"] == {"available": False, "model": ""}
    added = up(api, aid, "boats.txt", BOATS)
    assert added["document"]["name"] == "boats.txt" and added["embedded"] is False
    up(api, aid, "rice.md", RICE)
    detail = api.get(aid)
    assert [d["name"] for d in detail["archive"]["document_list"]] == ["boats.txt", "rice.md"] and detail["archive"]["chunks"] == 2
    found = api.search({"query": "what keeps the boat upright", "archives": [aid]})
    assert found["mode"] == "lexical" and found["hits"][0]["document"] == "boats.txt" and found["searched"] == 1
    assert api.search({"query": "stock for the risotto", "project": PROJ})["hits"][0]["document"] == "rice.md"        # no archive named: all of the project's
    assert api.jobs.wait(api.reindex(aid)["job"]["id"])["result"]["chunks"] == 2
    assert api.remove_document(aid, detail["archive"]["document_list"][0]["id"])["removed"] is True
    assert api.get(aid)["archive"]["documents"] == 1
    assert api.update(aid, {"name": "Sailing"})["archive"]["name"] == "Sailing"
    with pytest.raises(RagError, match="confirmation"):
        api.delete(aid, {})
    assert api.delete(aid, {"confirm": True}) == {"deleted": True, "documents": 1}
    assert api.overview(PROJ)["archives"] == []


def test_the_overview_says_what_is_supported(tmp_path):
    o = make(tmp_path).overview(PROJ)
    assert ".pdf" in o["formats"] and ".epub" in o["formats"] and o["limits"]["documents"] > 0 and isinstance(o["pdf"], bool)


def test_a_document_can_be_added_by_its_path(tmp_path):
    api = make(tmp_path)
    aid = new(api, "One")["archive"]["id"]
    doc = tmp_path / "docs" / "boats.txt"
    doc.parent.mkdir()
    doc.write_bytes(BOATS)
    job = api.add_path(aid, {"path": str(doc)})["job"]
    assert api.jobs.wait(job["id"])["result"]["document"]["name"] == "boats.txt"
    for bad in ({}, {"path": "boats.txt"}, {"path": str(tmp_path / "docs" / ".." / "docs" / "boats.txt")}, {"path": str(tmp_path / "personas" / "x.txt")}):
        with pytest.raises(RagError):
            api.add_path(aid, bad)


def test_an_archive_needs_a_project_and_is_listed_only_in_it(tmp_path):
    api = make(tmp_path)
    for bad in ({"name": "A"}, {"name": "A", "project": "nope"}, {"name": "A", "project": PROJ.upper()}, {"name": "A", "project": PROJ, "persona": "maria"}):
        with pytest.raises(RagError):
            api.create(bad)
    a, b = new(api, "Boats")["archive"]["id"], new(api, "Boats", OTHER)["archive"]["id"]            # the same name in two projects
    assert [x["id"] for x in api.overview(PROJ)["archives"]] == [a] and [x["id"] for x in api.overview(OTHER)["archives"]] == [b]
    with pytest.raises(RagError):
        api.overview("")
    with pytest.raises(RagError):
        new(api, "boats")


def test_the_projects_are_the_folders_with_their_keys_and_archive_counts(tmp_path):
    api = make(tmp_path)
    work = project_key(tmp_path / "work")
    new(api, "One", work)
    new(api, "Two", work)
    new(api, "Elsewhere", OTHER)
    rows = api.projects()["projects"]
    assert [(r["name"], r["key"], r["archives"], r["chat"]) for r in rows] == [("Chat", project_key(tmp_path / "chat"), 0, True), ("Work", work, 2, False)]
    assert rows[1]["path"] == str(tmp_path / "work")
    twin = RagApi(api.store, lambda: [{"name": "A", "path": str(tmp_path / "work"), "chat": False}, {"name": "B", "path": str(tmp_path / "work") + "/", "chat": False}])
    assert [r["name"] for r in twin.projects()["projects"]] == ["A"]                                  # one folder, spelt twice: one project


def test_a_search_can_be_for_one_projects_archives(tmp_path):
    api = make(tmp_path)
    a, b = new(api, "Boats")["archive"]["id"], new(api, "Kitchen", OTHER)["archive"]["id"]
    up(api, a, "boats.txt", BOATS)
    up(api, b, "rice.txt", RICE)
    assert api.search({"query": "keel upright boat", "project": OTHER})["hits"] == []
    assert api.search({"query": "stock for risotto", "project": OTHER})["hits"][0]["archive"] == "Kitchen"
    for bad in ({}, {"query": " "}, {"query": 5}, {"query": "x", "project": PROJ, "top_k": 0}, {"query": "x", "project": PROJ, "top_k": 99}, {"query": "x", "project": PROJ, "top_k": True},
                {"query": "x"}, {"query": "x", "project": "nope"}, {"query": "x", "archives": "a"}, {"query": "x", "archives": [1]}):
        with pytest.raises(RagError):
            api.search(bad)
    with pytest.raises(RagError) as e:
        api.search({"query": "x", "archives": ["gone-123456"]})
    assert e.value.code == "not_found"


def test_the_route_parts_are_dispatched_and_unknown_ones_are_not_found(tmp_path):
    api = make(tmp_path)
    created = api.route_post(["archives"], {}, json.dumps({"name": "Boats", "project": PROJ}).encode())["archive"]
    aid = created["id"]
    assert api.route_get([], {"project": [PROJ]})["archives"][0]["id"] == aid and api.route_get(["archives", aid])["archive"]["name"] == "Boats"
    assert api.route_get(["projects"])["projects"][0]["chat"] is True and api.route_get([], {"project": [OTHER]})["archives"] == []
    job = api.route_post(["archives", aid, "upload"], {"name": ["boats.txt"]}, BOATS)["job"]
    assert api.jobs.wait(job["id"])["state"] == "done"
    assert api.route_post(["jobs", job["id"], "cancel"], {}, b"{}")["job"]["state"] == "done" and api.route_get(["jobs", job["id"]])["job"]["id"] == job["id"]      # too late to cancel
    assert api.route_post(["search"], {}, json.dumps({"query": "keel", "project": PROJ}).encode())["hits"]
    for gone in (["archives", aid, "link"],):
        with pytest.raises(RagError) as e:
            api.route_post(gone, {}, b"{}")
        assert e.value.code == "not_found"
    assert upload_limit(["archives", aid, "upload"]) > upload_limit(["archives", aid, "update"])
    for parts in (["nothing"], ["archives", aid, "frobnicate"], ["archives", aid, "documents", "x", "frobnicate"], ["upload"]):
        with pytest.raises(RagError) as e:
            api.route_post(parts, {}, b"{}")
        assert e.value.code == "not_found"
    with pytest.raises(RagError):
        api.route_post(["archives"], {}, b"[1]")
    with pytest.raises(RagError):
        api.route_get(["archives"])
    with pytest.raises(RagError):
        api.route_get([])                                                                              # the project is part of the question
    with pytest.raises(RagError):
        api.route_post(["archives"], {}, json.dumps({"name": "x", "project": PROJ, "colour": "red"}).encode())


class Embedder:
    model = "fake-1"

    def embed(self, texts):
        import numpy as np
        return np.array([[t.lower().count("boat") + 1, t.lower().count("rice") + 1] for t in texts], dtype="float32")


def test_hybrid_search_switches_on_when_an_embedder_appears(tmp_path):
    holder = {"e": None}
    api = make(tmp_path, lambda: holder["e"])
    aid = new(api, "Boats")["archive"]["id"]
    up(api, aid, "boats.txt", BOATS)
    assert api.embedding()["available"] is False and api.get(aid)["archive"]["mode"] == "text"
    holder["e"] = Embedder()
    assert api.embedding() == {"available": True, "model": "fake-1"} and api.get(aid)["archive"]["reindex"] is True
    job = api.reindex(aid)["job"]
    assert api.jobs.wait(job["id"])["result"]["embedded"] is True
    assert api.get(aid)["archive"]["mode"] == "hybrid" and api.search({"query": "boat keel", "project": PROJ})["mode"] == "hybrid"


class EmbedInfo:
    """What the embedding service tells the page, without a model."""

    def info(self):
        return {"enabled": True, "available": True, "model": "fake-1", "reason": "", "device": "cuda", "models": [{"name": "fake-1", "kind": "embed", "bytes": 5}]}

    def test(self):
        return {"ok": True, "model": "fake-1", "device": "cuda", "seconds": 0.5, "dim": 2}


def test_the_embedding_state_the_test_and_the_settings_come_through_the_service(tmp_path):
    api = make(tmp_path, lambda: Embedder())
    assert api.embedding() == {"available": True, "model": "fake-1"} and api.embedding_test()["ok"] is False                  # no service: nothing to try
    api.service = EmbedInfo()
    got = api.overview(PROJ)["embedding"]
    assert got["available"] and got["model"] == "fake-1" and got["device"] == "cuda" and got["models"][0]["name"] == "fake-1"
    assert api.route_post(["embedding", "test"], {}, b"{}")["dim"] == 2
    out = api.route_post(["settings"], {}, json.dumps({"lexical_weight": 0.1, "rerank": False, "idle_s": 30}).encode())
    assert out["settings"]["lexical_weight"] == 0.1 and out["settings"]["rerank"] is False and out["embedding"]["device"] == "cuda"
    with pytest.raises(RagError):
        api.route_post(["settings"], {}, json.dumps({"lexical_weight": 2}).encode())


def test_indexing_again_is_a_job_that_tells_its_passages_and_search_takes_a_share_and_a_strictness(tmp_path):
    api = make(tmp_path, lambda: Embedder())
    aid = new(api, "Boats")["archive"]["id"]
    up(api, aid, "boats.txt", BOATS)
    job = api.reindex(aid)["job"]
    assert job["label"] == "index Boats"
    done = api.jobs.wait(job["id"])
    assert done["state"] == "done" and done["result"]["embedded"] is True and done["pages"] == done["result"]["chunks"] and done["stage"] == "indexing"
    assert api.search({"query": "boat keel", "archives": [aid, "gone-000000"], "strict": False})["searched"] == 2
    with pytest.raises(RagError):
        api.search({"query": "boat keel", "archives": [aid, "gone-000000"]})
    with pytest.raises(RagError, match="min_share"):
        api.search({"query": "boat", "archives": [aid], "min_share": 2})
    assert api.search({"query": "boat keel", "archives": [aid], "min_share": 0.5})["hits"]


class Routes(unittest.TestCase):
    """The routes, on a server with this PC's own page."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.rag = RagStore(base / "rag")
        (base / "work").mkdir()
        cls.projects = ProjectStore(base / "journal")
        cls.work = cls.projects.add(str(base / "work"))["path"]
        cls.patches = [mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_PROJECTS", cls.projects),
                       mock.patch.object(handler, "_RAG", cls.rag)]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.rag.router.close()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None, raw=None):
        h = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"} if method == "POST" else {}
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode("utf-8"))
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=h, method=method), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] == b"{" else body)

    def wait_job(self, jid):
        for _ in range(100):
            job = self.call("GET", f"/api/rag/jobs/{jid}")[1]["job"]
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        self.fail("the job did not end")

    def test_the_whole_cycle_over_http(self):
        status, body = self.call("POST", "/api/rag/archives", {"name": "Route Test", "description": "d", "project": PROJ})
        self.assertEqual(status, 200)
        aid = body["archive"]["id"]
        self.assertEqual(self.call("POST", "/api/rag/archives", {"name": "route test", "project": PROJ})[0], 400)
        self.assertEqual(self.call("POST", "/api/rag/archives", {"name": "no project"})[0], 400)
        status, started = self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)
        self.assertEqual(status, 200)
        job = self.wait_job(started["job"]["id"])
        self.assertEqual((job["state"], job["result"]["document"]["name"]), ("done", "boats.txt"))
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)[0], 400)           # the same file twice
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=..%2Fx.txt", raw=BOATS)[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=x.exe", raw=BOATS)[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/documents", {"path": "relative.txt"})[0], 400)
        status, listing = self.call("GET", f"/api/rag?project={PROJ}")
        self.assertEqual((status, [a["id"] for a in listing["archives"]]), (200, [aid]))
        self.assertEqual(self.call("GET", f"/api/rag?project={OTHER}")[1]["archives"], [])
        self.assertEqual(self.call("GET", "/api/rag")[0], 400)
        self.assertFalse(listing["embedding"]["available"])
        status, found = self.call("POST", "/api/rag/search", {"query": "keel upright", "project": PROJ})
        self.assertEqual((status, found["hits"][0]["document"], found["mode"]), (200, "boats.txt", "lexical"))
        status, view = self.call("GET", f"/api/rag/archives/{aid}")
        self.assertEqual((status, view["archive"]["documents"]), (200, 1))
        doc = view["archive"]["document_list"][0]["id"]
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/reindex", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/documents/{doc}/remove", {})[1]["removed"], True)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/delete", {})[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/delete", {"confirm": True})[1]["deleted"], True)
        self.assertEqual(self.call("GET", f"/api/rag/archives/{aid}")[0], 404)

    def test_the_projects_the_page_can_pick(self):
        status, body = self.call("GET", "/api/rag/projects")
        self.assertEqual(status, 200)
        chat, work = body["projects"][0], [p for p in body["projects"] if not p["chat"]]
        self.assertTrue(chat["chat"] and re.fullmatch(r"[0-9a-f]{12}", chat["key"]))
        self.assertEqual([(p["key"], p["path"]) for p in work], [(project_key(self.work), self.work)])

    def test_resolve_is_served_and_the_removed_chat_routes_are_gone(self):
        aid = self.call("POST", "/api/rag/archives", {"name": "Chat Boats", "project": PROJ})[1]["archive"]["id"]
        status, got = self.call("POST", "/api/rag/resolve", {"function": "agent", "project": PROJ})
        self.assertEqual((status, got["ids"], got["archives"][0]["name"]), (200, [aid], "Chat Boats"))
        self.assertEqual(self.call("GET", f"/api/rag/resolve?function=meeting&project={PROJ}")[1]["ask_dialog"], [aid])
        self.assertEqual(self.call("POST", "/api/rag/resolve", {"function": "chat"})[0], 400)
        for gone in ("recall", "choose"):
            self.assertEqual(self.call("POST", f"/api/rag/{gone}", {"project": PROJ, "goal": "x"})[0], 404)
        self.call("POST", "/api/personas", {"name": "Compose Test"})
        status, composed = self.call("POST", "/api/personas/compose-test/compose", {"goal": "what keeps the boat upright, the keel?"})
        self.assertEqual(status, 200)
        self.assertNotIn("Chat Boats", composed["memory"])
        self.assertNotIn("knowledge", self.call("GET", "/api/personas/compose-test")[1]["persona"])
        self.assertEqual(self.call("POST", "/api/personas/compose-test/update", {"knowledge": []})[0], 400)
        self.call("POST", "/api/personas/compose-test/delete", {"confirm": True})
        self.call("POST", f"/api/rag/archives/{aid}/delete", {"confirm": True})

    def test_unknown_things_and_bad_bodies(self):
        self.assertEqual(self.call("GET", "/api/rag/archives/..%2F..%2Fx")[0], 404)
        self.assertEqual(self.call("GET", "/api/rag/archives/nobody")[0], 404)
        self.assertEqual(self.call("GET", "/api/rag/frob")[0], 404)
        self.assertEqual(self.call("POST", "/api/rag/archives/nobody/update", {"name": "A"})[0], 404)
        self.assertEqual(self.call("POST", "/api/rag/archives", raw=b"not json")[0], 400)

    def test_only_this_pc_reaches_them(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            for method, path in (("GET", "/api/rag"), ("GET", "/api/rag/projects"), ("GET", "/api/rag/archives/x"), ("POST", "/api/rag/archives"), ("POST", "/api/rag/search"), ("POST", "/api/rag/resolve"),
                                 ("POST", "/api/rag/archives/x/upload?name=a.txt")):
                status, _ = self.call(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 403, path)


def test_a_conversation_hands_its_process_the_archive_folder_and_its_projects_key(tmp_path):
    from serve.agent_sessions import SessionManager
    real = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", personas_dir=tmp_path / "personas", rag_dir=tmp_path / "rag", personal_keys=tmp_path / "keys", tick_s=0.1)
    try:
        (tmp_path / "w").mkdir()
        sid = real.create(str(tmp_path / "w"))["id"]
        cmd = real._command(sid, real._record(sid), tmp_path / "h")
        assert cmd[cmd.index("--rag-dir") + 1] == str(tmp_path / "rag")
        assert cmd[cmd.index("--rag-project") + 1] == project_key(tmp_path / "w")                       # the key of the conversation's folder
    finally:
        real.shutdown()


def test_the_chats_own_folder_is_a_project_too_and_no_personalities_are_needed(tmp_path):
    from serve.agent_sessions import SessionManager
    real = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", rag_dir=tmp_path / "rag", default_workspace=tmp_path / "chat-workspace", tick_s=0.1)
    try:
        sid = real.create(None)["id"]
        cmd = real._command(sid, real._record(sid), tmp_path / "h")
        assert cmd[cmd.index("--rag-project") + 1] == project_key(tmp_path / "chat-workspace") and "--personas-dir" not in cmd
    finally:
        real.shutdown()


# -- choosing the archives of a conversation ---------------------------------------------------------------------------

def test_the_catalog_lists_every_archive_with_the_name_of_its_project(tmp_path):
    api = make(tmp_path)
    work = project_key(tmp_path / "work")
    new(api, "Plans", work)
    new(api, "Notes", OTHER)
    names = {a["name"]: a["project_name"] for a in api.catalog()["archives"]}
    assert names == {"Plans": "Work", "Notes": ""}                                                    # a project the page does not know has no name
    assert api.store.list(work)[0]["project_name"] == "Work"                                          # the name is kept when the archive is made


# -- the resolver over the API, and the search that obeys it -----------------------------------------------------------

def test_resolve_names_the_archives_and_says_why_one_is_left_out(tmp_path):
    api = RagApi(RagStore(tmp_path / "rag", embedder=lambda: None), lambda: [{"name": "Work", "path": str(tmp_path / "work"), "chat": False}],
                 sources=lambda: {"session": lambda ctx: {"workspace": str(tmp_path / "work"), "knowledge": []}}, leaves_pc=lambda fn: fn == "agent")
    work = project_key(str(tmp_path / "work"))
    open_, private = new(api, "Open", work)["archive"]["id"], new(api, "Private", work)["archive"]["id"]
    api.update(open_, {"cloud_ok": True})
    got = api.route_post(["resolve"], {}, json.dumps({"function": "agent", "session": "s1", "project": PROJ}).encode())
    assert got["routed_to_cloud"] is True and got["ids"] == [open_] and got["project"] == work
    assert got["archives"][0]["name"] == "Open" and got["archives"][0]["project_name"] == "Work"
    assert got["blocked"][0]["name"] == "Private" and got["blocked"][0]["reason"] == "not cleared for cloud models" and private == got["blocked"][0]["id"]
    local = api.route_get(["resolve"], {"function": ["meeting"], "project": [work]})
    assert local["audience"] == "others" and local["ids"] == [] and sorted(local["ask_dialog"]) == sorted([open_, private])
    for bad in ({"function": "chat"}, {"function": "agent", "attached": "x"}, {"function": "agent", "routed_to_cloud": "yes"}):
        with pytest.raises(RagError):
            api.resolve(bad)


def test_search_with_a_context_leaves_out_what_the_route_may_not_carry(tmp_path):
    api = RagApi(RagStore(tmp_path / "rag", embedder=lambda: None), leaves_pc=lambda fn: True)
    a, b = new(api, "Open")["archive"]["id"], new(api, "Private")["archive"]["id"]
    api.update(a, {"cloud_ok": True})
    up(api, a, "boats.txt", BOATS)
    up(api, b, "rice.txt", RICE)
    free = api.search({"query": "sloop keel risotto stock", "archives": [a, b]})
    assert {h["archive_id"] for h in free["hits"]} == {a, b} and "blocked" not in free                 # the person's own view in the tab
    held = api.search({"query": "sloop keel risotto stock", "archives": [a, b], "context": {"function": "agent"}})
    assert {h["archive_id"] for h in held["hits"]} == {a} and held["searched"] == 1 and held["blocked"][0]["id"] == b
    by_project = api.search({"query": "sloop keel risotto stock", "project": PROJ, "context": {"function": "agent", "routed_to_cloud": True}})
    assert {h["archive_id"] for h in by_project["hits"]} == {a}
    with pytest.raises(RagError):
        api.search({"query": "sloop", "archives": [a], "context": "agent"})
    def test_the_original_comes_back_as_an_attachment_that_never_runs_as_a_page_and_an_unknown_one_is_404(self):
        status, body = self.call("POST", "/api/rag/archives", {"name": "Originals Test", "project": PROJ})
        aid = body["archive"]["id"]
        status, started = self.call("POST", f"/api/rag/archives/{aid}/upload?name=keep-me.txt", raw=BOATS)
        self.assertEqual(self.wait_job(started["job"]["id"])["state"], "done")
        view = self.call("GET", f"/api/rag/archives/{aid}")[1]["archive"]
        doc = view["document_list"][0]
        self.assertTrue(doc["has_original"])
        with urllib.request.urlopen(self.base + f"/api/rag/archives/{aid}/documents/{doc['id']}/original", timeout=10) as r:
            data, head = r.read(), r.headers
        self.assertEqual(data, BOATS)
        self.assertTrue(head["Content-Disposition"].startswith("attachment;") and "keep-me.txt" in head["Content-Disposition"])
        self.assertEqual((head["X-Content-Type-Options"], head["Content-Security-Policy"]), ("nosniff", "sandbox"))
        self.assertEqual(self.call("GET", f"/api/rag/archives/{aid}/documents/d00000000/original")[0], 404)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/delete", {"confirm": True})[1]["deleted"], True)



class EnrichIsWired(unittest.TestCase):
    def test_the_server_gives_the_archive_api_a_model_to_describe_with(self):
        source = (Path(__file__).resolve().parents[1] / "handler.py").read_text(encoding="utf-8")
        self.assertIn('describer=describer', source)
        self.assertIn('llm.client_for("rag_enrich"', source)


def test_the_figures_of_a_document_are_listed_with_thumbnails_and_one_is_given_whole_with_everything_kept_with_it(tmp_path):
    import base64
    import io
    from PIL import Image
    api = make(tmp_path)
    aid = api.store.create("Delta", project=PROJ)["id"]
    img = Image.new("RGB", (300, 200), (200, 220, 250))
    for x in range(0, 300, 6):
        for y in range(0, 200, 4):
            img.putpixel((x, y), (x % 255, y % 255, 90))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "map.png").write_bytes(buf.getvalue())
    text = "The delta is wide and the wind carries the boat along the shore, where the reeds grow thick and the water is shallow for the whole afternoon."
    (folder / "page.html").write_text(f'<html><body><p>{text}</p><figure><img src="map.png" alt="Map of the delta"><figcaption>Figure 1. Map of the delta</figcaption></figure></body></html>', encoding="utf-8")
    doc = api.store.add_file(aid, "page.html", folder / "page.html")["document"]
    listed = api.route_get(["archives", aid, "documents", doc["id"], "figures"], {})
    assert listed["total"] == 1 and listed["offset"] == 0 and listed["figures"][0]["label"] == "Figure 1" and listed["figures"][0]["caption"] == "Figure 1. Map of the delta"
    assert listed["figures"][0]["thumb"].startswith("data:image/webp;base64,") and Image.open(io.BytesIO(base64.b64decode(listed["figures"][0]["thumb"].split(",", 1)[1]))).width <= 256
    whole = api.route_get(["archives", aid, "documents", doc["id"], "figures", "1"], None)
    assert whole["alt"] == "Map of the delta" and whole["source"] == "web" and whole["document"] == "page.html" and whole["archive"] == "Delta" and whole["width"] == 300
    assert Image.open(io.BytesIO(base64.b64decode(whole["image"].split(",", 1)[1]))).format == "WEBP"
    assert api.route_get(["archives", aid, "documents", doc["id"], "figures"], {"offset": ["5"]})["figures"] == []
    for bad in (["archives", aid, "documents", doc["id"], "figures", "9"], ["archives", aid, "documents", doc["id"], "figures", "x"], ["archives", aid, "documents", "d00000000", "figures", "1"]):
        with pytest.raises(RagError):
            api.route_get(bad, None)


def test_a_project_is_made_renamed_and_deleted_with_its_archives_only_after_its_name_is_typed_and_its_files_stay(tmp_path):
    chat, work = tmp_path / "chat", tmp_path / "work"
    chat.mkdir()
    work.mkdir()
    (work / "keep.txt").write_text("my file", encoding="utf-8")
    projects = ProjectStore(tmp_path / "journal")

    def folders():
        return [{"name": "Chat", "path": str(chat), "chat": True}] + [{"name": p["name"], "path": p["path"], "chat": False} for p in projects.list() if p.get("exists")]
    api = RagApi(RagStore(tmp_path / "rag", embedder=lambda: None), folders, projects=projects)
    post = lambda parts, body: api.route_post(parts, {}, json.dumps(body).encode())          # noqa: E731
    made = post(["projects"], {"path": str(work), "name": "Delta notes"})
    key = made["created"]
    assert [p["name"] for p in made["projects"]] == ["Chat", "Delta notes"] and made["projects"][1]["archives"] == 0
    with pytest.raises(RagError):
        post(["projects"], {"path": str(tmp_path / "nowhere")})
    assert post(["projects", "rename"], {"key": key, "name": "Delta"})["projects"][1]["name"] == "Delta"
    a1 = api.store.create("One", project=key)["id"]
    api.store.create("Two", project=key)
    api.store.add_bytes(a1, "n.txt", b"The wind carries the boat along the shore, and the delta is wide. " * 6)
    assert [p["archives"] for p in api.projects()["projects"]] == [0, 2]
    for bad in ({"key": key, "name": "wrong", "confirm": True}, {"key": key, "name": "Delta"}, {"key": key, "confirm": True}):
        with pytest.raises(RagError, match="exactly"):
            post(["projects", "delete"], bad)
    assert len(api.store.list(key)) == 2                                                    # nothing was deleted by the refused requests
    gone = post(["projects", "delete"], {"key": key, "name": " Delta ", "confirm": True})
    assert (gone["deleted_archives"], gone["deleted_documents"]) == (2, 1) and [p["name"] for p in gone["projects"]] == ["Chat"]
    assert api.store.list(key) == [] and (work / "keep.txt").read_text(encoding="utf-8") == "my file"
    chat_key = api.projects()["projects"][0]["key"]
    for parts in (["projects", "delete"], ["projects", "rename"]):
        with pytest.raises(RagError, match="Chat"):
            post(parts, {"key": chat_key, "name": "Chat", "confirm": True})
    with pytest.raises(RagError, match="no such project"):
        post(["projects", "delete"], {"key": "p" * 12, "name": "x", "confirm": True})


def test_browsing_this_pc_lists_folders_then_readable_files_leaves_out_hidden_and_forbidden_ones_and_refuses_anything_but_a_full_folder_path(tmp_path):
    root = tmp_path / "files"
    (root / "sub").mkdir(parents=True)
    (root / "Zeta").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("a note", encoding="utf-8")
    (root / "photo.xyz").write_text("not a readable format", encoding="utf-8")
    secret = tmp_path / "personas"
    secret.mkdir()
    api = make(tmp_path)
    api.store.forbidden = [secret.resolve()]
    shown = api.route_get(["browse"], {"path": [str(root)]})
    assert shown["path"] == str(root) and shown["parent"] == str(tmp_path)
    assert [(e["name"], e["dir"]) for e in shown["entries"]] == [("sub", True), ("Zeta", True), ("notes.txt", False)]                 # folders first, the readable file, nothing hidden or unreadable
    assert shown["entries"][2]["path"] == str(root / "notes.txt") and shown["entries"][2]["size"] == 6
    up = api.route_get(["browse"], {"path": [str(tmp_path)]})
    assert "personas" not in [e["name"] for e in up["entries"]] and "files" in [e["name"] for e in up["entries"]]                   # the app's own data is not offered
    start = api.route_get(["browse"], {})
    assert start["path"] == "" and start["parent"] is None and start["entries"] and all(e["dir"] for e in start["entries"])
    for bad in ("relative/folder", str(root / "notes.txt"), str(tmp_path / "nowhere"), str(root / ".." / "files"), "x\x00y"):
        with pytest.raises(RagError):
            api.route_get(["browse"], {"path": [bad]})

