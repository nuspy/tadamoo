"""The Knowledge tab's server side: the archives of a project over the API, the routes (this PC only), and the harness's flags."""
import json
import re
import tempfile
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
from serve.tasks import ChangeJournal, TaskJournal
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


def new(api, name, project=PROJ, **more):
    return api.create({"name": name, "project": project, **more})


def test_an_archive_is_made_filled_asked_and_deleted(tmp_path):
    api = make(tmp_path)
    made = new(api, "Boats", description="About sailing")["archive"]
    aid = made["id"]
    assert made["documents"] == 0 and made["mode"] == "text" and made["project"] == PROJ and api.overview(PROJ)["embedding"] == {"available": False, "model": ""}
    added = api.upload(aid, "boats.txt", BOATS)
    assert added["document"]["name"] == "boats.txt" and added["embedded"] is False
    api.upload(aid, "rice.md", RICE)
    detail = api.get(aid)
    assert [d["name"] for d in detail["archive"]["document_list"]] == ["boats.txt", "rice.md"] and detail["archive"]["chunks"] == 2
    found = api.search({"query": "what keeps the boat upright", "archives": [aid]})
    assert found["mode"] == "lexical" and found["hits"][0]["document"] == "boats.txt" and found["searched"] == 1
    assert api.search({"query": "stock for the risotto", "project": PROJ})["hits"][0]["document"] == "rice.md"        # no archive named: all of the project's
    assert api.reindex(aid)["chunks"] == 2
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
    assert api.add_path(aid, {"path": str(doc)})["document"]["name"] == "boats.txt"
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
    api.upload(a, "boats.txt", BOATS)
    api.upload(b, "rice.txt", RICE)
    assert api.search({"query": "keel upright boat", "project": OTHER})["hits"] == []
    assert api.search({"query": "stock for risotto", "project": OTHER})["hits"][0]["archive"] == "Kitchen"
    for bad in ({}, {"query": " "}, {"query": 5}, {"query": "x", "project": PROJ, "top_k": 0}, {"query": "x", "project": PROJ, "top_k": 99}, {"query": "x", "project": PROJ, "top_k": True},
                {"query": "x"}, {"query": "x", "project": "nope"}, {"query": "x", "archives": "a"}, {"query": "x", "archives": [1]}):
        with pytest.raises(RagError):
            api.search(bad)
    with pytest.raises(RagError) as e:
        api.search({"query": "x", "archives": ["gone-123456"]})
    assert e.value.code == "not_found"


def test_recall_gives_the_data_block_of_the_projects_passages(tmp_path):
    api = make(tmp_path)
    a, b = new(api, "Boats")["archive"]["id"], new(api, "Kitchen", OTHER)["archive"]["id"]
    api.upload(a, "boats.txt", BOATS)
    api.upload(b, "rice.txt", RICE)
    block = api.recall({"project": PROJ, "goal": "what keeps the boat upright, the keel?"})["block"]
    assert "[Boats / boats.txt]" in block and block.startswith("<untrusted_data source=\"knowledge-archive\"") and "Kitchen" not in block
    assert api.recall({"project": PROJ, "goal": "stock for risotto"})["block"] == ""                  # the other project's passages stay out
    assert api.recall({"project": PROJ, "goal": ""})["block"] == ""
    for bad in ({"goal": "x"}, {"project": "nope", "goal": "x"}, {"project": PROJ, "goal": 5}):
        with pytest.raises(RagError):
            api.recall(bad)


def test_the_route_parts_are_dispatched_and_unknown_ones_are_not_found(tmp_path):
    api = make(tmp_path)
    created = api.route_post(["archives"], {}, json.dumps({"name": "Boats", "project": PROJ}).encode())["archive"]
    aid = created["id"]
    assert api.route_get([], {"project": [PROJ]})["archives"][0]["id"] == aid and api.route_get(["archives", aid])["archive"]["name"] == "Boats"
    assert api.route_get(["projects"])["projects"][0]["chat"] is True and api.route_get([], {"project": [OTHER]})["archives"] == []
    assert api.route_post(["archives", aid, "upload"], {"name": ["boats.txt"]}, BOATS)["document"]["name"] == "boats.txt"
    assert api.route_post(["search"], {}, json.dumps({"query": "keel", "project": PROJ}).encode())["hits"]
    assert api.route_post(["recall"], {}, json.dumps({"project": PROJ, "goal": "what keeps the boat upright"}).encode())["block"]
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
    api.upload(aid, "boats.txt", BOATS)
    assert api.embedding()["available"] is False and api.get(aid)["archive"]["mode"] == "text"
    holder["e"] = Embedder()
    assert api.embedding() == {"available": True, "model": "fake-1"} and api.get(aid)["archive"]["reindex"] is True
    assert api.reindex(aid)["embedded"] is True
    assert api.get(aid)["archive"]["mode"] == "hybrid" and api.search({"query": "boat keel", "project": PROJ})["mode"] == "hybrid"


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
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
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

    def test_the_whole_cycle_over_http(self):
        status, body = self.call("POST", "/api/rag/archives", {"name": "Route Test", "description": "d", "project": PROJ})
        self.assertEqual(status, 200)
        aid = body["archive"]["id"]
        self.assertEqual(self.call("POST", "/api/rag/archives", {"name": "route test", "project": PROJ})[0], 400)
        self.assertEqual(self.call("POST", "/api/rag/archives", {"name": "no project"})[0], 400)
        status, up = self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)
        self.assertEqual((status, up["document"]["name"]), (200, "boats.txt"))
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

    def test_the_chat_text_for_a_project_and_the_personality_text_without_passages(self):
        aid = self.call("POST", "/api/rag/archives", {"name": "Chat Boats", "project": PROJ})[1]["archive"]["id"]
        self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)
        status, recalled = self.call("POST", "/api/rag/recall", {"project": PROJ, "goal": "what keeps the boat upright, the keel?"})
        self.assertEqual(status, 200)
        self.assertIn("[Chat Boats / boats.txt]", recalled["block"])
        self.assertIn('<untrusted_data source="knowledge-archive"', recalled["block"])
        self.assertEqual(self.call("POST", "/api/rag/recall", {"project": OTHER, "goal": "what keeps the boat upright, the keel?"})[1], {"block": ""})
        self.assertEqual(self.call("POST", "/api/rag/recall", {"goal": "x"})[0], 400)
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
            for method, path in (("GET", "/api/rag"), ("GET", "/api/rag/projects"), ("GET", "/api/rag/archives/x"), ("POST", "/api/rag/archives"), ("POST", "/api/rag/search"), ("POST", "/api/rag/recall"),
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
