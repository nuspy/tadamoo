"""Settings > Voice over HTTP: the catalog with what is installed, the choices, the folder, and downloads on explicit request. A local server stands
in for Hugging Face and for a file host; nothing reaches the network."""
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

from serve import downloads, voice_catalog as vc
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
REV = "b" * 40
GOOD = "hf_" + "g" * 20
JS = {"Content-Type": "application/json"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


class Stand:
    """Hugging Face (revision, tree, resolve with ranges) for `repos` and a file host for `urls`, on one port."""

    def __init__(self, repos=None, urls=None, no_length=False):
        self.repos, self.urls, self.no_length = repos or {}, urls or {}, no_length
        self.seen: list[dict] = []
        stand = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None, length=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if length is not False:
                    self.send_header("Content-Length", str(len(body) if length is None else length))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                stand.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "range": self.headers.get("Range")})
                path = self.path.partition("?")[0]
                parts = path.strip("/").split("/")
                if path == "/api/whoami-v2":
                    return self._send(200, b'{"name": "tester"}')
                if len(parts) >= 5 and parts[0] == "api" and parts[1] == "models":
                    repo = "/".join(parts[2:4])
                    files = stand.repos.get(repo)
                    if files is None:
                        return self._send(404)
                    if parts[4] == "revision":
                        return self._send(200, json.dumps({"sha": REV}).encode())
                    if parts[4] == "tree" and parts[5] == REV:
                        items = [{"type": "file", "path": p, "size": len(b), "oid": "git" + sha(b)[:37], "lfs": {"oid": sha(b), "size": len(b)}}
                                 for p, b in sorted(files.items())]
                        return self._send(200, json.dumps(items).encode(), {"Content-Type": "application/json"})
                    return self._send(404)
                body = None
                if len(parts) > 3 and parts[2] == "resolve" and parts[3] == REV:
                    body = (stand.repos.get("/".join(parts[:2])) or {}).get("/".join(parts[4:]))
                elif parts[0] == "files":
                    body = stand.urls.get("/".join(parts[1:]))
                if body is None:
                    return self._send(404)
                start, headers, code = 0, {}, 200
                if self.headers.get("Range"):
                    start = int(self.headers["Range"].split("=")[1].rstrip("-"))
                    code = 206
                    headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
                data = body[start:]
                if self.command == "HEAD" and stand.no_length:
                    return self._send(200, length=False)
                self._send(code, data, headers)

        self.httpd = Server(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


XTTS = {"model.pth": os.urandom(90_000), "config.json": b'{"a": 1}', "vocab/vocab.json": os.urandom(5_000), ".gitattributes": b"*.pth lfs", "README.md": b"# r"}
BASE = {"weights.bin": os.urandom(40_000), "config.json": b"{}"}
CUSTOM = {"weights.bin": os.urandom(30_000), "speech_tokenizer/model.bin": os.urandom(8_000)}
URL_BYTES = os.urandom(70_000)


def make_catalog(urls_base: str, sha_url=None):
    return [
        vc._entry("xtts-v2", "tts", "xtts", "XTTS stand-in", "xtts", "cpml-1.0", [vc._hf("c/xtts")], noncommercial=True, size=1000),
        vc._entry("qwen3tts-0.6b", "tts", "qwen3tts", "Qwen stand-in", "qwen", "apache-2.0",
                  [vc._hf("c/base", subdir="base"), vc._hf("c/custom", subdir="custom-voice")]),
        vc._entry("whisper-small", "stt", "whisper", "Whisper stand-in", "whisper", "verify",
                  [vc._url(urls_base + "/files/small.pt", "small.pt", sha_url or sha(URL_BYTES))], probe=["small.pt"], verify=("license",)),
        vc._entry("kokoro", "tts", "kokoro", "Kokoro stand-in", "kokoro", "verify", [vc._url(urls_base + "/files/k.onnx", "k.onnx")], verify=("sha256",)),
        vc._entry("piper", "tts", "piper", "Piper stand-in", "piper", "verify", [], verify=("source",)),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hf = Stand({"c/xtts": XTTS, "c/base": BASE, "c/custom": CUSTOM})
        self.files = Stand(urls={"small.pt": URL_BYTES, "k.onnx": b"k" * 100})
        self.addCleanup(self.hf.close)
        self.addCleanup(self.files.close)
        catalog = make_catalog(self.files.url)
        patches = [
            mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.root / "data"), "TADAMOO_CACHE": str(self.root / "cache"), "HF_TOKEN": "",
                                         "HUGGING_FACE_HUB_TOKEN": ""}),
            mock.patch.object(downloads, "_token_files", return_value=[]),
            mock.patch.object(downloads, "_session_token", None),
            mock.patch.object(downloads, "DISK_MARGIN", 0),
            mock.patch.object(downloads, "ENDPOINT", self.hf.url),
            mock.patch.object(downloads, "DOWNLOADS", downloads.Downloader()),
            mock.patch.object(vc, "CATALOG", catalog),
            mock.patch.dict(vc.BY_ID, {e["id"]: e for e in catalog}, clear=True),
            mock.patch.object(vc, "DEFAULTS", {"stt": "whisper-small", "tts": "kokoro", "avatar": "", "diarization": "", "vad": "", "embedding": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        downloads._whoami_cache.clear()
        self.folder = self.root / "voice"
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.post("/api/voice/models-dir", {"path": str(self.folder)})

    def req(self, method, path, body=None):
        for attempt in range(3):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            try:
                c.connect()
            except OSError:
                c.close()
                if attempt == 2:
                    raise
                continue
            try:
                c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=JS if body is not None else {})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"null")
            finally:
                c.close()

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, body=None):
        return self.req("POST", path, body if body is not None else {})

    def wait(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            s = self.get("/api/voice/download/status")[1]
            if s["phase"] in ("done", "failed", "cancelled"):
                return s
            time.sleep(0.03)
        raise AssertionError("the download did not finish")

    def start(self, model, **extra):
        code, plan = self.get(f"/api/voice/download/plan?id={model}")
        self.assertEqual(code, 200, plan)
        body = {"id": model, "token": plan["token"], "confirm": True, "ack": True, **extra}
        return plan, self.post("/api/voice/download", body)


class OverviewTest(Base):
    def test_the_catalog_with_what_is_chosen_and_installed(self):
        code, body = self.get("/api/voice/models")
        self.assertEqual(code, 200)
        self.assertEqual(body["models_dir"], str(self.folder))
        self.assertEqual([s["id"] for s in body["services"]], list(vc.SERVICES))
        by = {s["id"]: s for s in body["services"]}
        self.assertEqual((by["stt"]["selected"], by["tts"]["selected"]), ("whisper-small", "kokoro"))
        self.assertEqual(by["tts"]["placement"], {"device": "auto", "memory": "vram"})
        self.assertEqual([m["id"] for m in by["tts"]["models"]], ["xtts-v2", "qwen3tts-0.6b", "kokoro", "piper"])
        xtts = by["tts"]["models"][0]
        self.assertTrue(xtts["noncommercial"] and not xtts["installed"] and xtts["downloadable"])
        self.assertEqual(xtts["license"]["id"], "cpml-1.0")
        kokoro = by["tts"]["models"][2]
        self.assertTrue(kokoro["verify"] and not kokoro["downloadable"])
        self.assertEqual(body["download"]["phase"], "idle")
        self.assertNotIn("http://", json.dumps(body))

    def test_the_routes_are_for_this_pc(self):
        with mock.patch("serve.settings.is_loopback", return_value=False):
            for path in ("/api/voice/models", "/api/voice/download/plan?id=xtts-v2", "/api/voice/download/status"):
                self.assertEqual(self.get(path)[0], 403, path)
            for path in ("select", "placement", "models-dir", "download", "download/cancel"):
                self.assertEqual(self.post("/api/voice/" + path)[0], 403, path)

    def test_an_unknown_route_is_a_404(self):
        self.assertEqual(self.post("/api/voice/nope")[0], 404)


class ChoiceTest(Base):
    def test_select_and_placement_are_kept_and_shown(self):
        code, body = self.post("/api/voice/select", {"service": "tts", "model": "xtts-v2"})
        self.assertEqual(code, 200)
        self.assertEqual({s["id"]: s["selected"] for s in body["services"]}["tts"], "xtts-v2")
        code, body = self.post("/api/voice/placement", {"service": "tts", "device": "cuda:1", "memory": "vram"})
        self.assertEqual({s["id"]: s["placement"] for s in body["services"]}["tts"], {"device": "cuda:1", "memory": "vram"})
        self.assertEqual({s["id"]: s["selected"] for s in self.get("/api/voice/models")[1]["services"]}["tts"], "xtts-v2")

    def test_a_refusal_is_a_400_with_the_code_and_both_texts(self):
        for path, body, code in (("select", {"service": "stt", "model": "xtts-v2"}, "model"), ("select", {"service": "x", "model": "a"}, "service"),
                                 ("select", {"service": "avatar", "model": "wav2lip-int8"}, "model"),          # a file for the phones, not a model of the function
                                 ("placement", {"service": "tts", "device": "cpu", "memory": "vram"}, "cpu_vram"),
                                 ("placement", {"service": "tts", "device": "tpu", "memory": "vram"}, "device"),
                                 ("models-dir", {"path": "relative"}, "path_relative")):
            status, out = self.post("/api/voice/" + path, body)
            self.assertEqual((status, out["error"]["code"]), (400, code), (path, out))
            self.assertTrue(out["error"]["message"] and out["error"]["message_it"])

    def test_a_body_that_is_not_json_is_a_400(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/voice/select", body=b"{nope", headers=JS)
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_changing_the_folder_moves_nothing_and_installed_follows_the_folder(self):
        _, (code, out) = self.start("xtts-v2", noncommercial=True)
        self.assertEqual(code, 202, out)
        self.assertEqual(self.wait()["phase"], "done")
        self.assertTrue({m["id"]: m for m in self.get("/api/voice/models")[1]["services"][1]["models"]}["xtts-v2"]["installed"])
        other = self.root / "elsewhere"
        code, body = self.post("/api/voice/models-dir", {"path": str(other)})
        self.assertEqual((code, body["models_dir"]), (200, str(other)))
        self.assertTrue((self.folder / "tts" / "xtts-v2" / "model.pth").is_file())
        self.assertFalse(other.exists())
        self.assertFalse({m["id"]: m for m in body["services"][1]["models"]}["xtts-v2"]["installed"])
        self.post("/api/voice/models-dir", {"path": str(self.folder)})
        self.assertTrue({m["id"]: m for m in self.get("/api/voice/models")[1]["services"][1]["models"]}["xtts-v2"]["installed"])


class PlanTest(Base):
    def test_a_huggingface_plan_resolves_the_commit_and_lists_whole_repository_files(self):
        code, plan = self.get("/api/voice/download/plan?id=xtts-v2")
        self.assertEqual(code, 200, plan)
        self.assertTrue(plan["ok"], plan["problems"])
        self.assertEqual({f["name"] for f in plan["files"]}, {"model.pth", "config.json", "vocab/vocab.json", "README.md"})        # every file but the hidden ones
        self.assertEqual(plan["total_bytes"], 90_000 + 8 + 5_000 + 3)
        self.assertEqual(plan["folder"], str(self.folder / "tts" / "xtts-v2"))
        self.assertEqual({f["sha256"] for f in plan["files"]}, {sha(XTTS[n]) for n in ("model.pth", "config.json", "vocab/vocab.json", "README.md")})
        self.assertTrue(plan["noncommercial"])
        self.assertIn("Non-commercial", plan["license"]["en"])
        self.assertTrue(plan["license"]["it"])
        self.assertIn(("GET", "/api/models/c/xtts/revision/main"), [(s["method"], s["path"]) for s in self.hf.seen])
        self.assertTrue(all(f["url"].endswith(f"/c/xtts/resolve/{REV}/{f['path']}") for f in plan["files"]))
        self.assertIsNotNone(plan["free_bytes"])
        self.assertTrue(self.folder.is_dir())

    def test_two_repositories_go_to_their_own_subfolders(self):
        plan = self.get("/api/voice/download/plan?id=qwen3tts-0.6b")[1]
        self.assertTrue(plan["ok"], plan["problems"])
        self.assertEqual(sorted(f["name"] for f in plan["files"]),
                         ["base/config.json", "base/weights.bin", "custom-voice/speech_tokenizer/model.bin", "custom-voice/weights.bin"])
        self.assertFalse(plan["noncommercial"])

    def test_a_url_plan_takes_the_size_from_the_server_and_needs_no_hugging_face(self):
        plan = self.get("/api/voice/download/plan?id=whisper-small")[1]
        self.assertTrue(plan["ok"], plan["problems"])
        (f,) = plan["files"]
        self.assertEqual((f["name"], f["size"], f["sha256"]), ("small.pt", len(URL_BYTES), sha(URL_BYTES)))
        self.assertEqual(plan["access"], "ok")
        self.assertEqual([s for s in self.hf.seen if "small.pt" in s["path"]], [])
        self.assertIn("not been checked", plan["license"]["en"])

    def test_a_url_whose_server_gives_no_size_is_a_problem(self):
        self.files.no_length = True
        plan = self.get("/api/voice/download/plan?id=whisper-small")[1]
        self.assertFalse(plan["ok"])
        self.assertIn("size", [p["code"] for p in plan["problems"]])

    def test_an_entry_the_code_does_not_complete_gives_a_verify_problem(self):
        for model in ("kokoro", "piper"):
            plan = self.get(f"/api/voice/download/plan?id={model}")[1]
            self.assertFalse(plan["ok"], model)
            self.assertEqual([p["code"] for p in plan["problems"]], ["verify"])
            self.assertTrue(plan["problems"][0]["text"] and plan["problems"][0]["text_it"])
        self.assertEqual([s for s in self.files.seen if "k.onnx" in s["path"]], [])        # not even a request for a file with no SHA-256

    def test_an_unknown_model_is_a_404(self):
        self.assertEqual(self.get("/api/voice/download/plan?id=nope")[0], 404)
        self.assertEqual(self.get("/api/voice/download/plan")[0], 404)

    def test_a_dead_hugging_face_is_a_problem_not_a_crash(self):
        self.hf.close()
        plan = self.get("/api/voice/download/plan?id=xtts-v2")[1]
        self.assertFalse(plan["ok"])
        self.assertIn("network", [p["code"] for p in plan["problems"]])

    def test_the_voice_catalog_does_not_shadow_the_models_catalog(self):
        from serve import catalog
        real = catalog.CATALOG[0]["id"]
        self.assertIsNone(vc.find(real))
        self.assertEqual(downloads.find_entry(real)["id"], real)
        self.assertEqual(downloads.find_entry("xtts-v2")["kind"], "voice")


class DownloadTest(Base):
    def test_nothing_starts_without_the_plan_confirm_and_license(self):
        plan = self.get("/api/voice/download/plan?id=qwen3tts-0.6b")[1]
        for body, code in (({"id": "qwen3tts-0.6b"}, "confirm"),
                           ({"id": "qwen3tts-0.6b", "token": plan["token"], "confirm": True}, "confirm"),
                           ({"id": "qwen3tts-0.6b", "token": plan["token"], "ack": True}, "confirm"),
                           ({"id": "qwen3tts-0.6b", "token": "stale", "confirm": True, "ack": True}, "plan_changed")):
            status, out = self.post("/api/voice/download", body)
            self.assertEqual((status, out["error"]["code"]), (409, code), body)
        self.assertEqual(self.post("/api/voice/download", {"id": "nope", "confirm": True, "ack": True, "token": "x"})[0], 404)
        self.assertFalse((self.folder / "tts").exists())
        self.assertEqual(self.get("/api/voice/download/status")[1]["phase"], "idle")

    def test_a_non_commercial_model_needs_its_own_confirmation(self):
        plan = self.get("/api/voice/download/plan?id=xtts-v2")[1]
        body = {"id": "xtts-v2", "token": plan["token"], "confirm": True, "ack": True}
        for extra in ({}, {"noncommercial": False}, {"noncommercial": "yes"}):
            status, out = self.post("/api/voice/download", {**body, **extra})
            self.assertEqual((status, out["error"]["code"]), (409, "noncommercial"), extra)
        self.assertFalse((self.folder / "tts" / "xtts-v2").exists())
        status, out = self.post("/api/voice/download", {**body, "noncommercial": True})
        self.assertEqual((status, out["phase"]), (202, "running"))
        self.assertEqual(self.wait()["phase"], "done")

    def test_a_download_keeps_the_folders_skips_hidden_files_and_records_a_marker(self):
        _, (code, out) = self.start("xtts-v2", noncommercial=True)
        self.assertEqual(code, 202, out)
        status = self.wait()
        self.assertEqual(status["phase"], "done", status)
        d = self.folder / "tts" / "xtts-v2"
        for name, body in XTTS.items():
            if name != ".gitattributes":
                self.assertEqual((d / name).read_bytes(), body, name)
        self.assertFalse((d / ".gitattributes").exists())
        self.assertEqual([p for p in d.rglob("*.part")], [])
        marker = json.loads((d / ".basalt-model.json").read_text(encoding="utf-8"))
        self.assertEqual({f["name"] for f in marker["files"]}, {"model.pth", "config.json", "vocab/vocab.json", "README.md"})
        self.assertIn("installed", json.dumps(self.get("/api/voice/models")[1]))
        self.assertNotIn("hf_", json.dumps(status))

    def test_two_repositories_make_one_model(self):
        _, (code, out) = self.start("qwen3tts-0.6b")
        self.assertEqual(code, 202, out)
        self.assertEqual(self.wait()["phase"], "done")
        d = self.folder / "tts" / "qwen3tts-0.6b"
        self.assertEqual((d / "base" / "weights.bin").read_bytes(), BASE["weights.bin"])
        self.assertEqual((d / "custom-voice" / "speech_tokenizer" / "model.bin").read_bytes(), CUSTOM["speech_tokenizer/model.bin"])
        self.assertTrue({m["id"]: m for m in self.get("/api/voice/models")[1]["services"][1]["models"]}["qwen3tts-0.6b"]["installed"])

    def test_a_url_file_is_checked_against_its_sha256_and_never_gets_the_token(self):
        with mock.patch.dict(os.environ, {"HF_TOKEN": GOOD}):
            _, (code, out) = self.start("whisper-small")
            self.assertEqual(code, 202, out)
            self.assertEqual(self.wait()["phase"], "done")
        self.assertEqual((self.folder / "stt" / "whisper-small" / "small.pt").read_bytes(), URL_BYTES)
        asked = [s for s in self.files.seen if s["path"].startswith("/files/")]
        self.assertTrue(asked)
        self.assertEqual({s["auth"] for s in asked}, {None})

    def test_a_url_file_that_does_not_match_its_sha256_is_discarded(self):
        wrong = [e for e in vc.CATALOG if e["id"] == "whisper-small"][0]
        wrong["sources"][0]["sha256"] = "0" * 64
        _, (code, _) = self.start("whisper-small")
        self.assertEqual(code, 202)
        status = self.wait()
        self.assertEqual((status["phase"], status["error"]["code"]), ("failed", "hash"))
        self.assertEqual(list((self.folder / "stt" / "whisper-small").glob("*")), [])

    def test_a_refused_entry_is_a_422_and_nothing_is_fetched(self):
        plan = self.get("/api/voice/download/plan?id=kokoro")[1]
        status, out = self.post("/api/voice/download", {"id": "kokoro", "token": plan["token"], "confirm": True, "ack": True})
        self.assertEqual((status, out["error"]["code"]), (422, "problems"))
        self.assertFalse((self.folder / "tts" / "kokoro").exists())
        self.assertEqual([s for s in self.files.seen if "k.onnx" in s["path"]], [])

    def test_a_part_file_resumes_with_a_range_request(self):
        part_dir = self.folder / "stt" / "whisper-small"
        part_dir.mkdir(parents=True)
        (part_dir / "small.pt.part").write_bytes(URL_BYTES[:30_000])
        plan, (code, _) = self.start("whisper-small")
        self.assertEqual(plan["files"][0]["part"], 30_000)
        self.assertEqual(self.wait()["phase"], "done")
        self.assertEqual((part_dir / "small.pt").read_bytes(), URL_BYTES)
        self.assertIn("bytes=30000-", [s["range"] for s in self.files.seen])

    def test_status_and_cancel_when_nothing_runs(self):
        self.assertEqual(self.get("/api/voice/download/status")[1]["phase"], "idle")
        code, out = self.post("/api/voice/download/cancel")
        self.assertEqual((code, out["cancelled"]), (200, False))

    def test_an_installed_model_is_planned_as_already_here(self):
        self.start("qwen3tts-0.6b")
        self.wait()
        plan = self.get("/api/voice/download/plan?id=qwen3tts-0.6b")[1]
        self.assertTrue(all(f["have"] for f in plan["files"]))
        self.assertEqual(plan["remaining_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
