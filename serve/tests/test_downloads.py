"""The Download panel's backend: the plan, the download with its checks, the token's limits and the HTTP routes. A local
server stands in for Hugging Face; no test reads the real token or reaches the real network."""
from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from serve import catalog, downloads, models
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
REV = "a" * 40
GOOD = "hf_" + "g" * 20             # a token the fake accepts
TERMS = "hf_" + "t" * 20            # a token whose account has not accepted the terms


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128          # the default backlog of 5 drops connections of a burst


class FakeHF:
    """What the downloader talks to: the tree API, resolve with ranges, the account check, and optionally a redirect
    to a second server standing for the CDN."""

    def __init__(self, files, gated=None, lie=False, delay=0.0, redirect=None, page=0, repo="o/r", cut_after=0):
        self.files, self.gated, self.lie, self.delay, self.redirect, self.page, self.repo = \
            files, gated, lie, delay, redirect, page, repo
        self.cut_after, self.cuts = cut_after, 0
        self.seen: list[dict] = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                fake.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                  "range": self.headers.get("Range")})
                path, _, query = self.path.partition("?")
                auth = self.headers.get("Authorization") or ""
                if path == "/api/whoami-v2":
                    return self._send(200, json.dumps({"name": "tester"}).encode()) if auth == f"Bearer {GOOD}" \
                        else self._send(401)
                tree = f"/api/models/{fake.repo}/tree/{REV}"
                if path == tree:
                    items = [{"type": "file", "path": p, "size": len(b), "oid": "git" + sha(b)[:37],
                              "lfs": {"oid": ("0" * 64 if fake.lie else sha(b)), "size": len(b)}}
                             for p, b in sorted(fake.files.items())]
                    headers = {"Content-Type": "application/json"}
                    if fake.page:
                        n = int(dict(q.split("=") for q in query.split("&") if "=" in q).get("page", 0))
                        chunk = items[n * fake.page:(n + 1) * fake.page]
                        if (n + 1) * fake.page < len(items):
                            headers["Link"] = f'<http://{self.headers["Host"]}{tree}?recursive=true&page={n + 1}>; rel="next"'
                        items = chunk
                    return self._send(200, json.dumps(items).encode(), headers)
                prefix = f"/{fake.repo}/resolve/{REV}/"
                if path.startswith(prefix) or path.startswith("/cdn/"):
                    name = path[len(prefix):] if path.startswith(prefix) else path[len("/cdn/"):]
                    if path.startswith(prefix):
                        if fake.gated == "token" and auth not in (f"Bearer {GOOD}", f"Bearer {TERMS}"):
                            return self._send(401)
                        if fake.gated == "token" and auth == f"Bearer {TERMS}":
                            return self._send(403)
                        if fake.redirect:
                            return self._send(302, headers={"Location": fake.redirect + "/cdn/" + name})
                    body = fake.files.get(name)
                    if body is None:
                        return self._send(404)
                    start, headers, code = 0, {}, 200
                    rng = self.headers.get("Range")
                    if rng:
                        start = int(rng.split("=")[1].rstrip("-"))
                        if start >= len(body):
                            return self._send(416, headers={"Content-Range": f"bytes */{len(body)}"})
                        code = 206
                        headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
                    data = body[start:]
                    self.send_response(code)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command == "HEAD":
                        return
                    try:
                        for i in range(0, len(data), 16384):
                            if fake.cut_after and not fake.cuts and i >= fake.cut_after:
                                fake.cuts += 1
                                self.close_connection = True       # the connection drops mid-body, once
                                return
                            self.wfile.write(data[i:i + 16384])
                            self.wfile.flush()
                            if fake.delay:
                                time.sleep(fake.delay)
                    except OSError:
                        pass
                    return
                self._send(404)

        self.httpd = Server(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def entry(files, gated=False, **kw):
    e = {"id": "t-1", "name": "Test model", "family": "base", "quant": "Q", "kind": "gguf", "convert": "ok",
         "uncensored": False, "gated": gated, "license": "qwen", "approx_bytes": 0,
         "short": {"en": "s", "it": "s"}, "long": {"en": "l", "it": "l"}, "fidelity": {"en": "f", "it": "f"},
         "notes": [], "repo": "o/r", "revision": REV, "folder": "t1", "files": list(files),
         "match": {"all": [], "none": []}}
    e.update(kw)
    return e


def wait(dl, timeout=30, phases=("done", "failed", "cancelled")):
    end = time.time() + timeout
    while time.time() < end:
        s = dl.status()
        if s["phase"] in phases:
            return s
        time.sleep(0.02)
    raise AssertionError(f"still {dl.status()['phase']} after {timeout}s")


class Base(unittest.TestCase):
    FILES = {"a-00001-of-00002.gguf": os.urandom(200_000), "a-00002-of-00002.gguf": os.urandom(150_000),
             "mmproj.gguf": os.urandom(30_000)}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "models"
        self.dest.mkdir()
        self.patches = [
            mock.patch.dict(os.environ, {"BASALT_CACHE": str(self.root / "cache"), "BASALT_DATA": str(self.root / "data"),
                                         "HF_TOKEN": "", "HUGGING_FACE_HUB_TOKEN": ""}),
            mock.patch.object(downloads, "_token_files", return_value=[]),
            mock.patch.object(downloads, "_session_token", None),
            mock.patch.object(downloads, "DISK_MARGIN", 0),
        ]
        for p in self.patches:
            p.start()
        downloads._whoami_cache.clear()
        self.servers: list[FakeHF] = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def hf(self, files=None, **kw) -> FakeHF:
        s = FakeHF(self.FILES if files is None else files, **kw)
        self.servers.append(s)
        return s

    def use(self, server, ent=None, **ekw):
        ent = ent or entry(self.FILES if server.files is self.FILES else server.files, **ekw)
        p1 = mock.patch.object(downloads, "ENDPOINT", server.url)
        p2 = mock.patch.object(catalog, "CATALOG", [ent])
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        return ent


class Plan(Base):
    def test_the_plan_reads_sizes_and_hashes_from_the_tree(self):
        self.use(self.hf())
        p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertTrue(p["ok"], p["problems"])
        self.assertEqual({f["name"]: f["size"] for f in p["files"]}, {n: len(b) for n, b in self.FILES.items()})
        self.assertEqual({f["name"]: f["sha256"] for f in p["files"]}, {n: sha(b) for n, b in self.FILES.items()})
        self.assertEqual(p["total_bytes"], sum(len(b) for b in self.FILES.values()))
        self.assertEqual(p["folder"], str(self.dest / "t1"))
        self.assertFalse(p["in_models_dirs"])
        self.assertTrue(downloads.plan("t-1", str(self.dest), folders=[str(self.dest)])["in_models_dirs"])

    def test_a_paged_tree_is_read_whole(self):
        self.use(self.hf(page=1))
        self.assertEqual(len(downloads.plan("t-1", str(self.dest), folders=[])["files"]), 3)

    def test_no_room_is_a_problem(self):
        self.use(self.hf())
        with mock.patch.object(downloads.shutil, "disk_usage", return_value=mock.Mock(free=1000)):
            p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertFalse(p["ok"])
        self.assertIn("disk", [x["code"] for x in p["problems"]])

    def test_a_bad_folder_is_a_problem(self):
        self.use(self.hf())
        for bad in ("relative/dir", str(self.root / "nope")):
            p = downloads.plan("t-1", bad, folders=[])
            self.assertFalse(p["ok"])
            self.assertIn("dest", [x["code"] for x in p["problems"]])

    def test_offline_is_a_problem_not_a_crash(self):
        s = self.hf()
        self.use(s)
        s.close()
        self.servers.remove(s)
        p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertFalse(p["ok"])
        self.assertIn("network", [x["code"] for x in p["problems"]])

    def test_what_is_not_offered_is_refused(self):
        with self.assertRaises(models.Refused) as e:
            downloads.plan("unsloth-ud-q6_k_xl", str(self.dest), folders=[])
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(models.Refused) as e:
            downloads.plan("nope", str(self.dest), folders=[])
        self.assertEqual(e.exception.status, 404)

    def test_names_cannot_leave_the_folder(self):
        for bad in ("../x.gguf/..", "a b.gguf", "CON.gguf", "nul", "x.", ".hidden", "a:b.gguf"):
            with self.assertRaises(downloads.DownloadError, msg=bad):
                downloads.safe_name("dir/" + bad)
        self.assertEqual(downloads.safe_name("IQ3_S/Qwen3.8-FN-GSQ-RCO-IQ3_S.basalt"), "Qwen3.8-FN-GSQ-RCO-IQ3_S.basalt")


class Download(Base):
    def go(self, dl, **kw):
        p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertTrue(p["ok"], p["problems"])
        return p, dl.start("t-1", str(self.dest), p["token"], True, True, folders=[])

    def test_downloads_verifies_and_renames(self):
        self.use(self.hf())
        dl = downloads.Downloader()
        p, started = self.go(dl)
        self.assertEqual(started["phase"], "running")
        s = wait(dl)
        self.assertEqual(s["phase"], "done", s)
        self.assertEqual((s["files_done"], s["files_total"]), (3, 3))
        self.assertEqual(s["bytes_done"], s["bytes_total"])
        for n, b in self.FILES.items():
            self.assertEqual((self.dest / "t1" / n).read_bytes(), b)
        self.assertEqual([x.name for x in (self.dest / "t1").glob("*.part")], [])

    def test_a_second_start_is_busy_and_a_finished_file_is_kept(self):
        srv = self.hf(delay=0.01)
        self.use(srv)
        dl = downloads.Downloader()
        p, _ = self.go(dl)
        with self.assertRaises(models.Refused) as e:
            dl.start("t-1", str(self.dest), p["token"], True, True, folders=[])
        self.assertEqual(e.exception.status, 409)
        wait(dl)
        srv.seen.clear()
        p2 = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertTrue(all(f["have"] for f in p2["files"]))
        self.assertEqual(p2["remaining_bytes"], 0)

    def test_resume_asks_for_the_rest_with_a_range(self):
        srv = self.hf()
        self.use(srv)
        name = "a-00001-of-00002.gguf"
        (self.dest / "t1").mkdir()
        (self.dest / "t1" / (name + ".part")).write_bytes(self.FILES[name][:70_000])
        dl = downloads.Downloader()
        self.go(dl)
        self.assertEqual(wait(dl)["phase"], "done")
        self.assertEqual((self.dest / "t1" / name).read_bytes(), self.FILES[name])
        ranged = [x for x in srv.seen if x["path"].endswith(name) and x["range"]]
        self.assertEqual(ranged[0]["range"], "bytes=70000-")

    def test_a_wrong_hash_discards_the_file(self):
        self.use(self.hf(lie=True))
        dl = downloads.Downloader()
        self.go(dl)
        s = wait(dl)
        self.assertEqual((s["phase"], s["error"]["code"]), ("failed", "hash"))
        self.assertEqual(list((self.dest / "t1").glob("*.gguf*")), [])

    def test_a_download_starts_only_from_its_plan(self):
        self.use(self.hf())
        dl = downloads.Downloader()
        p = downloads.plan("t-1", str(self.dest), folders=[])
        for args in ((p["token"], True, None), (p["token"], None, True), (None, True, True)):
            with self.assertRaises(models.Refused) as e:
                dl.start("t-1", str(self.dest), *args, folders=[])
            self.assertEqual((e.exception.status, e.exception.body["error"]["code"]), (409, "confirm"))
        with self.assertRaises(models.Refused) as e:
            dl.start("t-1", str(self.dest), "0" * 64, True, True, folders=[])
        self.assertEqual(e.exception.body["error"]["code"], "plan_changed")
        self.assertEqual(dl.status()["phase"], "idle")

    def test_cancel_keeps_the_part_and_the_next_start_finishes(self):
        srv = self.hf(files={"big.gguf": os.urandom(600_000)}, delay=0.02)
        self.use(srv, entry(["big.gguf"]))
        dl = downloads.Downloader()
        self.go(dl)
        end = time.time() + 20
        while dl.status()["bytes_done"] < 40_000 and time.time() < end:
            time.sleep(0.01)
        self.assertTrue(dl.cancel(), dl.status())
        s = wait(dl)
        self.assertEqual(s["phase"], "cancelled")
        part = self.dest / "t1" / "big.gguf.part"
        self.assertTrue(part.is_file() and 0 < part.stat().st_size < 600_000)
        self.assertFalse((self.dest / "t1" / "big.gguf").exists())
        srv.delay = 0
        self.go(dl)
        self.assertEqual(wait(dl)["phase"], "done")
        self.assertEqual((self.dest / "t1" / "big.gguf").read_bytes(), srv.files["big.gguf"])

    def test_a_dropped_connection_resumes_by_itself(self):
        files = {"big.gguf": os.urandom(300_000)}
        srv = self.hf(files=files, cut_after=98_304)
        self.use(srv, entry(["big.gguf"]))
        dl = downloads.Downloader()
        with mock.patch.object(dl.cancel_flag, "wait", lambda t: None):
            self.go(dl)
            s = wait(dl)
        self.assertEqual(s["phase"], "done", s)
        self.assertEqual((self.dest / "t1" / "big.gguf").read_bytes(), files["big.gguf"])
        self.assertEqual(srv.cuts, 1)
        self.assertIn("bytes=98304-", [x["range"] for x in srv.seen])

    def test_a_full_disk_stops_with_its_own_message(self):
        self.use(self.hf())
        dl = downloads.Downloader()
        real_open = open

        class Full:
            def __init__(self, f):
                self.f = f

            def write(self, b):
                raise OSError(28, "No space left on device")

            def __enter__(self):
                self.f.__enter__()
                return self

            def __exit__(self, *a):
                return self.f.__exit__(*a)

        def boom(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            return Full(f) if str(path).endswith(".part") and "b" in mode else f

        with mock.patch("builtins.open", boom):
            self.go(dl)
            s = wait(dl)
        self.assertEqual((s["phase"], s["error"]["code"]), ("failed", "disk"))


class Access(Base):
    def test_a_gated_repository_asks_for_a_token_then_the_terms(self):
        self.use(self.hf(gated="token"), gated=True)
        p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertEqual((p["ok"], p["access"], [x["code"] for x in p["problems"]]), (False, "token", ["token"]))
        with mock.patch.dict(os.environ, {"HF_TOKEN": TERMS}):
            p = downloads.plan("t-1", str(self.dest), folders=[])
            self.assertEqual((p["access"], p["token_source"]), ("terms", "env"))
            self.assertEqual([x["code"] for x in p["problems"]], ["terms"])
        with mock.patch.dict(os.environ, {"HF_TOKEN": GOOD}):
            p = downloads.plan("t-1", str(self.dest), folders=[])
            self.assertEqual((p["ok"], p["access"]), (True, "ok"))

    def test_the_token_goes_to_hugging_face_and_not_across_a_redirect(self):
        cdn = self.hf(files=self.FILES)
        origin = self.hf(redirect=cdn.url, files=self.FILES)
        self.use(origin, gated=True)
        host = "127.0.0.1"
        with mock.patch.dict(os.environ, {"HF_TOKEN": GOOD}), mock.patch.object(downloads, "TRUSTED_SUFFIXES", (host,)):
            dl = downloads.Downloader()
            p = downloads.plan("t-1", str(self.dest), folders=[])
            self.assertTrue(p["ok"], p["problems"])
            dl.start("t-1", str(self.dest), p["token"], True, True, folders=[])
            self.assertEqual(wait(dl)["phase"], "done")
        self.assertTrue(any(x["auth"] == f"Bearer {GOOD}" for x in origin.seen if "/resolve/" in x["path"]))
        cdn_gets = [x for x in cdn.seen if x["path"].startswith("/cdn/")]
        self.assertTrue(cdn_gets)
        self.assertEqual([x["auth"] for x in cdn_gets], [None] * len(cdn_gets))

    def test_a_redirect_to_another_host_is_refused_and_not_retried(self):
        origin = self.hf(redirect="http://203.0.113.9", files=self.FILES)
        self.use(origin)
        dl = downloads.Downloader()
        p = downloads.plan("t-1", str(self.dest), folders=[])
        self.assertTrue(p["ok"], p["problems"])
        dl.start("t-1", str(self.dest), p["token"], True, True, folders=[])
        s = wait(dl, timeout=10)
        self.assertEqual((s["phase"], s["error"]["code"]), ("failed", "redirect"))

    def test_token_sources_and_what_is_reported(self):
        self.use(self.hf())
        self.assertEqual(downloads.hf_token(), (None, None))
        f = self.root / "tok"
        f.write_text(GOOD + "\n", encoding="utf-8")
        with mock.patch.object(downloads, "_token_files", return_value=[f]):
            self.assertEqual(downloads.hf_token(), (GOOD, "file"))
            with mock.patch.dict(os.environ, {"HF_TOKEN": TERMS}):
                self.assertEqual(downloads.hf_token(), (TERMS, "env"))
                st = downloads.set_session_token(GOOD)
                self.assertEqual(downloads.hf_token(), (GOOD, "session"))
        self.assertEqual((st["source"], st["user"]), ("session", "tester"))
        self.assertNotIn(GOOD, json.dumps(st))
        self.assertEqual(downloads.set_session_token("")["source"], None)

    def test_a_pasted_token_is_checked_before_it_is_kept(self):
        self.use(self.hf())
        for bad in ("not-a-token", "hf_short"):
            with self.assertRaises(downloads.DownloadError) as e:
                downloads.set_session_token(bad)
            self.assertEqual(e.exception.code, "bad_token")
        with self.assertRaises(downloads.DownloadError) as e:
            downloads.set_session_token("hf_" + "x" * 20)             # the right shape, refused by Hugging Face
        self.assertEqual(e.exception.code, "bad_token")
        self.assertIsNone(downloads.hf_token()[0])


class OverHttp(Base):
    def setUp(self):
        super().setUp()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        for attempt in range(3):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            try:
                c.connect()                        # a connection that never opened sent nothing: trying again is safe
            except OSError:
                c.close()
                if attempt == 2:
                    raise
                continue
            try:
                c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers or {})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"null")
            finally:
                c.close()

    JS = {"Content-Type": "application/json"}

    def test_the_catalog_is_public_and_complete(self):
        code, body = self.req("GET", "/api/models/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["entries"]), len(catalog.CATALOG))
        self.assertTrue(all(e["short"]["en"] and e["short"]["it"] for e in body["entries"]))
        self.assertIn("qwen", body["licenses"])
        self.assertNotIn("revision", json.dumps(body["entries"][0]))

    def test_the_routes_are_for_this_pc_and_its_own_page(self):
        for path in ("download-plan", "download", "download-cancel", "hf-token"):
            self.assertEqual(self.req("POST", f"/api/models/{path}", {})[0], 415, path)
            self.assertEqual(self.req("POST", f"/api/models/{path}", {}, {**self.JS, "Origin": "http://evil.example.com"})[0],
                             403, path)
            with mock.patch("serve.settings.is_loopback", return_value=False):
                self.assertEqual(self.req("POST", f"/api/models/{path}", {}, self.JS)[0], 403, path)
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.req("GET", "/api/models/hf")[0], 403)

    def test_plan_download_status_and_cancel(self):
        srv = self.hf()
        self.use(srv)
        code, plan = self.req("POST", "/api/models/download-plan", {"id": "t-1", "dest": str(self.dest)}, self.JS)
        self.assertEqual((code, plan["ok"]), (200, True), plan)
        self.assertEqual(self.req("POST", "/api/models/download-plan", {"id": "zzz"}, self.JS)[0], 404)
        code, body = self.req("POST", "/api/models/download", {"id": "t-1", "dest": str(self.dest)}, self.JS)
        self.assertEqual((code, body["error"]["code"]), (409, "confirm"))
        code, body = self.req("POST", "/api/models/download",
                              {"id": "t-1", "dest": str(self.dest), "token": plan["token"], "confirm": True, "ack": True},
                              self.JS)
        self.assertEqual((code, body["phase"]), (202, "running"), body)
        end = time.time() + 20
        while time.time() < end and self.req("GET", "/api/models/download")[1]["phase"] != "done":
            time.sleep(0.05)
        status = self.req("GET", "/api/models/download")[1]
        self.assertEqual(status["phase"], "done")
        self.assertNotIn("token", json.dumps(status).lower().replace("files_total", ""))
        self.assertEqual(self.req("POST", "/api/models/download-cancel", {}, self.JS)[1]["cancelled"], False)

    def test_a_bad_pasted_token_is_a_400_and_a_dead_network_a_502(self):
        srv = self.hf()
        self.use(srv)
        code, body = self.req("POST", "/api/models/hf-token", {"token": "nope"}, self.JS)
        self.assertEqual((code, body["error"]["code"]), (400, "bad_token"))
        srv.close()
        self.servers.remove(srv)
        code, body = self.req("POST", "/api/models/download-plan", {"id": "t-1", "dest": str(self.dest)}, self.JS)
        self.assertEqual(code, 200)
        self.assertIn("network", [x["code"] for x in body["problems"]])
        code, body = self.req("POST", "/api/models/hf-token", {"token": "hf_" + "x" * 20}, self.JS)
        self.assertEqual((code, body["error"]["code"]), (502, "network"))


class Catalog(unittest.TestCase):
    def test_every_entry_is_complete_and_safe(self):
        ids = [e["id"] for e in catalog.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for e in catalog.CATALOG:
            with self.subTest(e["id"]):
                for k in ("short", "long"):
                    self.assertTrue(e[k]["en"].strip() and e[k]["it"].strip())
                self.assertIn(e["license"], catalog.LICENSES)
                self.assertIn(e["convert"], ("ready", "ok", "untested", "no"))
                self.assertEqual(e["kind"] == "basalt", e["convert"] == "ready")
                self.assertIn(e["family"], catalog.FAMILIES)
                if e["convert"] == "no":
                    self.assertTrue(e["reason"]["en"] and e["reason"]["it"])
                    self.assertEqual(e["files"], [])
                else:
                    self.assertRegex(e["revision"], r"^[0-9a-f]{40}$")
                    self.assertRegex(e["repo"], r"^[\w.-]+/[\w.-]+$")
                    self.assertTrue(e["files"])
                    for f in e["files"]:
                        downloads.safe_name(f)
                        self.assertNotIn("..", f)
                        self.assertFalse(f.startswith("/"))
                    self.assertEqual(e["kind"] == "basalt", all(f.endswith(".basalt") for f in e["files"]))
                    if e["kind"] == "gguf":
                        self.assertTrue(any("mmproj" in f for f in e["files"]), "the projector comes with it")
                if e["uncensored"] and e["convert"] != "no":
                    self.assertTrue(any(n is catalog.UNCENSORED_WARNING for n in e["notes"]), "the warning")

    def test_a_local_file_finds_its_entry(self):
        d = catalog.describe
        self.assertEqual(d(r"E:\Modelli\basalt\Qwen3.8-FN-GSQ-RCO-IQ3_S.basalt")["id"], "basalt-iq3_s")
        self.assertEqual(d("Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt")["id"], "orca-iq3_xxs")
        self.assertEqual(d("Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_XXS.basalt")["id"], "sc117-iq3_xxs")
        self.assertEqual(d("Qwen3.8-FN-Uncensored-Q4_K_M-v2.basalt")["id"], "jsonai-q4_k_m")
        self.assertIsNone(d("something-else.basalt"))


if __name__ == "__main__":
    unittest.main()
