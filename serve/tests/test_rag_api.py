"""The Knowledge tab's server side: the archives API, the routes (this PC only), the link to personalities, and the harness's flag."""
import json
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
from serve.personas_api import PersonasApi
from serve.plans import PlanService
from serve.rag_api import RagApi, upload_limit
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer
from tadamoo import personas as pe
from tadamoo.rag import RagError, RagStore

ROOT = Path(__file__).resolve().parents[2]
BOATS = b"A sloop has one mast and two sails. The keel keeps the boat upright and stops it sliding sideways. To tack, turn the bow through the wind."
RICE = b"Risotto needs short grain rice, hot stock and patience. Stir often and add the stock a ladle at a time."


def make(tmp_path, embedder=None):
    personas = pe.PersonaStore(tmp_path / "personas", key=bytes(range(32)))
    return RagApi(RagStore(tmp_path / "rag", embedder=embedder or (lambda: None), forbidden=[tmp_path / "personas"]), personas)


def test_an_archive_is_made_filled_asked_and_deleted(tmp_path):
    api = make(tmp_path)
    made = api.create({"name": "Boats", "description": "About sailing"})["archive"]
    aid = made["id"]
    assert made["documents"] == 0 and made["mode"] == "text" and api.overview()["embedding"] == {"available": False, "model": ""}
    added = api.upload(aid, "boats.txt", BOATS)
    assert added["document"]["name"] == "boats.txt" and added["embedded"] is False
    api.upload(aid, "rice.md", RICE)
    detail = api.get(aid)
    assert [d["name"] for d in detail["archive"]["document_list"]] == ["boats.txt", "rice.md"] and detail["archive"]["chunks"] == 2
    found = api.search({"query": "what keeps the boat upright", "archives": [aid]})
    assert found["mode"] == "lexical" and found["hits"][0]["document"] == "boats.txt" and found["searched"] == 1
    assert api.search({"query": "stock for the risotto"})["hits"][0]["document"] == "rice.md"           # no archive named: all of them
    assert api.reindex(aid)["chunks"] == 2
    assert api.remove_document(aid, detail["archive"]["document_list"][0]["id"])["removed"] is True
    assert api.get(aid)["archive"]["documents"] == 1
    assert api.update(aid, {"name": "Sailing"})["archive"]["name"] == "Sailing"
    with pytest.raises(RagError, match="confirmation"):
        api.delete(aid, {})
    assert api.delete(aid, {"confirm": True}) == {"deleted": True, "documents": 1, "unlinked": 0}
    assert api.overview()["archives"] == []


def test_the_overview_says_what_is_supported(tmp_path):
    o = make(tmp_path).overview()
    assert ".pdf" in o["formats"] and ".epub" in o["formats"] and o["limits"]["documents"] > 0 and isinstance(o["pdf"], bool)


def test_a_document_can_be_added_by_its_path(tmp_path):
    api = make(tmp_path)
    aid = api.create({"name": "One"})["archive"]["id"]
    doc = tmp_path / "docs" / "boats.txt"
    doc.parent.mkdir()
    doc.write_bytes(BOATS)
    assert api.add_path(aid, {"path": str(doc)})["document"]["name"] == "boats.txt"
    for bad in ({}, {"path": "boats.txt"}, {"path": str(tmp_path / "docs" / ".." / "docs" / "boats.txt")}, {"path": str(tmp_path / "personas" / "x.txt")}):
        with pytest.raises(RagError):
            api.add_path(aid, bad)


def test_linking_changes_the_personalitys_own_list_and_deleting_an_archive_unlinks_it(tmp_path):
    api = make(tmp_path)
    api.personas.create({"name": "Maria"})
    api.personas.create({"name": "Gino"})
    a, b = (api.create({"name": n})["archive"]["id"] for n in ("Boats", "Kitchen"))
    assert api.link(a, {"persona": "maria", "linked": True})["personas"] == [{"slug": "gino", "name": "Gino", "linked": False}, {"slug": "maria", "name": "Maria", "linked": True}]
    api.link(b, {"persona": "maria", "linked": True})
    api.link(a, {"persona": "gino", "linked": True})
    api.link(a, {"persona": "gino", "linked": True})                                                     # twice: still once
    assert api.personas.get("maria")["knowledge"] == [a, b] and api.personas.get("gino")["knowledge"] == [a]
    assert [p["slug"] for p in api.get(a)["personas"] if p["linked"]] == ["gino", "maria"]
    api.link(a, {"persona": "maria", "linked": False})
    assert api.personas.get("maria")["knowledge"] == [b]
    api.link(a, {"persona": "maria", "linked": True})
    assert api.delete(a, {"confirm": True})["unlinked"] == 2
    assert api.personas.get("maria")["knowledge"] == [b] and api.personas.get("gino")["knowledge"] == []
    for bad in ({"persona": "maria"}, {"persona": "maria", "linked": "yes"}):
        with pytest.raises(RagError):
            api.link(b, bad)
    with pytest.raises(RagError) as e:
        api.link(b, {"persona": "nobody", "linked": True})
    assert e.value.code == "not_found"
    with pytest.raises(RagError) as e:
        api.link("gone-123456", {"persona": "maria", "linked": True})
    assert e.value.code == "not_found"


def test_a_search_can_be_for_one_personalitys_archives(tmp_path):
    api = make(tmp_path)
    api.personas.create({"name": "Maria"})
    a, b = (api.create({"name": n})["archive"]["id"] for n in ("Boats", "Kitchen"))
    api.upload(a, "boats.txt", BOATS)
    api.upload(b, "rice.txt", RICE)
    api.link(b, {"persona": "maria", "linked": True})
    assert api.search({"query": "keel upright boat", "persona": "maria"})["hits"] == []
    assert api.search({"query": "stock for risotto", "persona": "maria"})["hits"][0]["archive"] == "Kitchen"
    for bad in ({}, {"query": " "}, {"query": 5}, {"query": "x", "top_k": 0}, {"query": "x", "top_k": 99}, {"query": "x", "top_k": True}, {"query": "x", "archives": "a"}, {"query": "x", "archives": [1]}):
        with pytest.raises(RagError):
            api.search(bad)
    with pytest.raises(RagError) as e:
        api.search({"query": "x", "archives": ["gone-123456"]})
    assert e.value.code == "not_found"
    with pytest.raises(RagError):
        api.search({"query": "x", "persona": "nobody"})


def test_the_route_parts_are_dispatched_and_unknown_ones_are_not_found(tmp_path):
    api = make(tmp_path)
    created = api.route_post(["archives"], {}, b'{"name": "Boats"}')["archive"]
    aid = created["id"]
    assert api.route_get([])["archives"][0]["id"] == aid and api.route_get(["archives", aid])["archive"]["name"] == "Boats"
    assert api.route_post(["archives", aid, "upload"], {"name": ["boats.txt"]}, BOATS)["document"]["name"] == "boats.txt"
    assert api.route_post(["search"], {}, json.dumps({"query": "keel"}).encode())["hits"]
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
        api.route_post(["archives"], {}, b'{"name": "x", "colour": "red"}')


class Embedder:
    model = "fake-1"

    def embed(self, texts):
        import numpy as np
        return np.array([[t.lower().count("boat") + 1, t.lower().count("rice") + 1] for t in texts], dtype="float32")


def test_hybrid_search_switches_on_when_an_embedder_appears(tmp_path):
    holder = {"e": None}
    api = make(tmp_path, lambda: holder["e"])
    aid = api.create({"name": "Boats"})["archive"]["id"]
    api.upload(aid, "boats.txt", BOATS)
    assert api.embedding()["available"] is False and api.get(aid)["archive"]["mode"] == "text"
    holder["e"] = Embedder()
    assert api.embedding() == {"available": True, "model": "fake-1"} and api.get(aid)["archive"]["reindex"] is True
    assert api.reindex(aid)["embedded"] is True
    assert api.get(aid)["archive"]["mode"] == "hybrid" and api.search({"query": "boat keel"})["mode"] == "hybrid"


class Routes(unittest.TestCase):
    """The routes, on a server with this PC's own page."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        personas = pe.PersonaStore(base / "personas", key=bytes(range(32)))
        cls.rag = RagStore(base / "rag")
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_PERSONAS", PersonasApi(personas, rag=cls.rag)),
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
        status, body = self.call("POST", "/api/rag/archives", {"name": "Route Test", "description": "d"})
        self.assertEqual(status, 200)
        aid = body["archive"]["id"]
        self.assertEqual(self.call("POST", "/api/rag/archives", {"name": "route test"})[0], 400)
        status, up = self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)
        self.assertEqual((status, up["document"]["name"]), (200, "boats.txt"))
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)[0], 400)           # the same file twice
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=..%2Fx.txt", raw=BOATS)[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/upload?name=x.exe", raw=BOATS)[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/documents", {"path": "relative.txt"})[0], 400)
        status, listing = self.call("GET", "/api/rag")
        self.assertEqual((status, [a["id"] for a in listing["archives"]]), (200, [aid]))
        self.assertFalse(listing["embedding"]["available"])
        status, found = self.call("POST", "/api/rag/search", {"query": "keel upright", "archives": [aid]})
        self.assertEqual((status, found["hits"][0]["document"], found["mode"]), (200, "boats.txt", "lexical"))
        status, view = self.call("GET", f"/api/rag/archives/{aid}")
        self.assertEqual((status, view["archive"]["documents"]), (200, 1))
        doc = view["archive"]["document_list"][0]["id"]
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/reindex", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/documents/{doc}/remove", {})[1]["removed"], True)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/delete", {})[0], 400)
        self.assertEqual(self.call("POST", f"/api/rag/archives/{aid}/delete", {"confirm": True})[1]["deleted"], True)
        self.assertEqual(self.call("GET", f"/api/rag/archives/{aid}")[0], 404)

    def test_linking_over_http_and_the_chat_text_that_follows(self):
        self.call("POST", "/api/personas", {"name": "Link Test"})
        aid = self.call("POST", "/api/rag/archives", {"name": "Linked"})[1]["archive"]["id"]
        self.call("POST", f"/api/rag/archives/{aid}/upload?name=boats.txt", raw=BOATS)
        status, linked = self.call("POST", f"/api/rag/archives/{aid}/link", {"persona": "link-test", "linked": True})
        self.assertEqual((status, [p for p in linked["personas"] if p["linked"]][0]["slug"]), (200, "link-test"))
        self.assertEqual(self.call("GET", "/api/personas/link-test")[1]["persona"]["knowledge"], [aid])
        status, composed = self.call("POST", "/api/personas/link-test/compose", {"goal": "what keeps the boat upright, the keel?"})
        self.assertEqual(status, 200)
        self.assertIn("[Linked / boats.txt]", composed["memory"])
        self.assertIn('<untrusted_data source="knowledge-archive"', composed["memory"])
        self.call("POST", f"/api/rag/archives/{aid}/delete", {"confirm": True})
        self.assertEqual(self.call("GET", "/api/personas/link-test")[1]["persona"]["knowledge"], [])
        self.call("POST", "/api/personas/link-test/delete", {"confirm": True})

    def test_unknown_things_and_bad_bodies(self):
        self.assertEqual(self.call("GET", "/api/rag/archives/..%2F..%2Fx")[0], 404)
        self.assertEqual(self.call("GET", "/api/rag/archives/nobody")[0], 404)
        self.assertEqual(self.call("GET", "/api/rag/frob")[0], 404)
        self.assertEqual(self.call("POST", "/api/rag/archives/nobody/update", {"name": "A"})[0], 404)
        self.assertEqual(self.call("POST", "/api/rag/archives", raw=b"not json")[0], 400)

    def test_only_this_pc_reaches_them(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            for method, path in (("GET", "/api/rag"), ("GET", "/api/rag/archives/x"), ("POST", "/api/rag/archives"), ("POST", "/api/rag/search"),
                                 ("POST", "/api/rag/archives/x/upload?name=a.txt")):
                status, _ = self.call(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 403, path)


def test_a_conversation_hands_its_process_the_archive_folder(tmp_path):
    from serve.agent_sessions import SessionManager
    real = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", personas_dir=tmp_path / "personas", rag_dir=tmp_path / "rag", personal_keys=tmp_path / "keys", tick_s=0.1)
    try:
        (tmp_path / "w").mkdir()
        sid = real.create(str(tmp_path / "w"))["id"]
        cmd = real._command(sid, real._record(sid), tmp_path / "h")
        assert cmd[cmd.index("--rag-dir") + 1] == str(tmp_path / "rag")
    finally:
        real.shutdown()
