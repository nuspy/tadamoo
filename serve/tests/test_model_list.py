"""GET /v1/models lists the served model and the folders' other models (serve/model_list.py), in the OpenAI and the
Anthropic shapes; a request that names an unloaded listed model is refused with a 404; the Loader's profile summaries
are planned with the running engine's memory given back."""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve.tests.test_models import ROOMY, tiny_basalt  # noqa: E402  (also puts tools/ on the path)
from model_file import ModelWriter  # noqa: E402
from serve import loader, model_list, models, profiles  # noqa: E402
from serve.engine.mock import MockEngine, NoEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GiB = 1 << 30
SERVED = "Qwen3.8 Flash Next Uncensored"


def other(mid, name=None, ctx=262144, vision=False, path=None):
    """One folder model as model_list.folder_models() gives it."""
    path = path or f"D:/Modelli/{mid}.basalt"
    return {"id": mid, "name": name or mid, "path": path, "created": 1760000000, "context_length": ctx,
            "vision": vision, "quant": "IQ3_XXS", "size_bytes": 1,
            "names": {mid.casefold(), f"{mid}.basalt".casefold(), model_list._norm(path)}}


def basalt_with_context(path, name, ctx) -> Path:
    """A small .basalt whose meta copies a GGUF header with the context length."""
    w = ModelWriter(str(path))
    meta = {"format": "basalt", "version": 1, "name": name,
            "gguf_kv": {"general.file_type": 23, "general.architecture": "qwen4exp", "qwen4exp.context_length": ctx},
            "sources": [], "tensors": {"per_layer_token_embd.weight": {"bytes": 8192}}, "profile": {"slots": 8}}
    w.add_bytes("meta", json.dumps(meta).encode())
    w.add_bytes("dense", b"\1" * 5000)
    w.add_bytes("experts", b"\2" * 8000)
    w.add_bytes("experts.layout", b"# experts v5: layer gu_type d_type offset blob_bytes (n_expert 4, total 8000)\n"
                                  b"0 18 20 0 1000\n1 18 20 4000 1000\n")
    w.add_bytes("profile", b"\0" * 64)
    w.close()
    return Path(path)


class Http(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=8192), tok,
                           ChatTemplate(ROOT / "serve/chat_template.jinja"), model_name=SERVED)
        self.svc.settings_info = {"model": "D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt"}
        self.items = [other("Qwen3.8-Flash-Next-IQ4_NL", "Qwen3.8 Flash Next", vision=True),
                      other("Orca-IQ3_XXS", ctx=None),
                      # the served file itself, as the scan finds it: listed once, as the served model
                      other("Qwen3.8-Flash-Next-Uncensored-IQ3_XXS", SERVED,
                            path="D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt")]
        self.svc.model_catalog = model_list.Catalog(lambda: list(self.items))
        self.svc.model_catalog.refresh()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            h = dict(headers or {})
            if body is not None:
                h.setdefault("Content-Type", "application/json")
            c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def test_openai_list(self):
        code, body = self.req("GET", "/v1/models")
        self.assertEqual(code, 200)
        self.assertEqual(body["object"], "list")
        data = body["data"]
        self.assertEqual([m["id"] for m in data], [SERVED, "Orca-IQ3_XXS", "Qwen3.8-Flash-Next-IQ4_NL"])
        self.assertEqual([m["status"]["value"] for m in data], ["loaded", "unloaded", "unloaded"])
        served, orca, iq4 = data
        self.assertEqual((served["meta"]["n_ctx"], served["context_length"], served["owned_by"]), (8192, 8192, "basalt"))
        self.assertEqual(served["path"], self.svc.settings_info["model"])      # bound to 127.0.0.1: the paths
        self.assertEqual(iq4["path"], "D:/Modelli/Qwen3.8-Flash-Next-IQ4_NL.basalt")
        self.assertEqual((iq4["context_length"], iq4["meta"], iq4["display_name"]),
                         (262144, {"n_ctx_train": 262144}, "Qwen3.8 Flash Next"))
        self.assertEqual(iq4["architecture"]["input_modalities"], ["text", "image"])
        self.assertNotIn("context_length", orca)                             # unknown: left out
        self.assertNotIn("n_ctx", (orca.get("meta") or {}))                  # only the served model has n_ctx
        self.assertTrue(all(isinstance(m["created"], int) for m in data))
        self.assertEqual(self.req("GET", "/models")[1], body)

    def test_anthropic_list(self):
        for headers in ({"anthropic-version": "2023-06-01"}, {"x-api-key": "k"}):
            with self.subTest(headers=headers):
                code, body = self.req("GET", "/v1/models", headers=headers)
                self.assertEqual(code, 200)
                self.assertEqual(set(body), {"data", "has_more", "first_id", "last_id"})
                self.assertEqual([m["id"] for m in body["data"]], [SERVED, "Orca-IQ3_XXS", "Qwen3.8-Flash-Next-IQ4_NL"])
                self.assertEqual(body["data"][0], {"type": "model", "id": SERVED, "display_name": SERVED,
                                                   "created_at": body["data"][0]["created_at"]})
                self.assertEqual(body["data"][2]["created_at"], "2025-10-09T08:53:20Z")
                self.assertEqual((body["has_more"], body["first_id"], body["last_id"]),
                                 (False, SERVED, "Qwen3.8-Flash-Next-IQ4_NL"))
        # an OpenAI client that also sends x-api-key keeps the OpenAI shape
        code, body = self.req("GET", "/v1/models", headers={"x-api-key": "k", "Authorization": "Bearer k"})
        self.assertEqual(body["object"], "list")

    def test_one_model(self):
        code, m = self.req("GET", "/v1/models/" + quote(SERVED))
        self.assertEqual((code, m["id"], m["status"]["value"]), (200, SERVED, "loaded"))
        code, m = self.req("GET", "/v1/models/orca-iq3_xxs")                # case ignored when nothing matches exactly
        self.assertEqual((code, m["id"], m["status"]["value"]), (200, "Orca-IQ3_XXS", "unloaded"))
        code, m = self.req("GET", "/v1/models/Orca-IQ3_XXS", headers={"anthropic-version": "2023-06-01"})
        self.assertEqual((code, m["type"], m["id"]), (200, "model", "Orca-IQ3_XXS"))
        code, e = self.req("GET", "/v1/models/nope")
        self.assertEqual((code, e["error"]["code"]), (404, "model_not_found"))
        code, e = self.req("GET", "/v1/models/nope", headers={"anthropic-version": "2023-06-01"})
        self.assertEqual((code, e["type"], e["error"]["type"]), (404, "error", "not_found_error"))

    def test_paths_only_on_a_loopback_server(self):
        self.assertTrue(model_list.is_loopback("127.0.0.1") and model_list.is_loopback("::1"))
        self.assertFalse(model_list.is_loopback("0.0.0.0") or model_list.is_loopback("192.168.1.5"))
        data = model_list.listing(self.svc, local=False)
        self.assertFalse(any("path" in m for m in data))
        message = model_list.refusal(self.svc, "Orca-IQ3_XXS", local=False)
        self.assertNotIn("D:/Modelli", message)
        self.assertIn("Avvia-Basalt.ps1 --model", message)

    def test_an_unloaded_model_is_refused(self):
        msgs = [{"role": "user", "content": "hi"}]
        for asked in ("Orca-IQ3_XXS", "orca-iq3_xxs", "Orca-IQ3_XXS.basalt", "D:/Modelli/Orca-IQ3_XXS.basalt"):
            with self.subTest(asked=asked):
                code, e = self.req("POST", "/v1/chat/completions", {"model": asked, "messages": msgs, "max_tokens": 4})
                self.assertEqual((code, e["error"]["code"], e["error"]["type"]), (404, "model_not_found",
                                                                                 "invalid_request_error"))
                self.assertIn("not loaded", e["error"]["message"])
                self.assertIn("Loader", e["error"]["message"])
                self.assertIn('Avvia-Basalt.ps1 --model "D:/Modelli/Orca-IQ3_XXS.basalt"', e["error"]["message"])
        code, e = self.req("POST", "/v1/messages", {"model": "Orca-IQ3_XXS", "messages": msgs, "max_tokens": 4})
        self.assertEqual((code, e["type"], e["error"]["type"]), (404, "error", "not_found_error"))
        code, e = self.req("POST", "/v1/responses", {"model": "Orca-IQ3_XXS", "input": "hi", "max_output_tokens": 4})
        self.assertEqual((code, e["error"]["code"]), (404, "model_not_found"))

    def test_other_names_are_served_as_before(self):
        msgs = [{"role": "user", "content": "hi"}]
        for asked in (SERVED, SERVED.lower(), "x", "default", "", None, "gpt-4o", "Qwen3.8 Flash Next",
                      "Qwen3.8-Flash-Next-Uncensored-IQ3_XXS"):                # its own file: the served model
            with self.subTest(asked=asked):
                body = {"messages": msgs, "max_tokens": 4}
                if asked is not None:
                    body["model"] = asked
                code, out = self.req("POST", "/v1/chat/completions", body)
                self.assertEqual(code, 200, out)
                self.assertEqual(out["model"], SERVED)
                code, out = self.req("POST", "/v1/messages", body)
                self.assertEqual(code, 200, out)

    def test_before_the_first_scan_nothing_is_refused_and_nothing_waits(self):
        gate = threading.Event()

        def slow():
            gate.wait(10)
            return list(self.items)
        self.svc.model_catalog = model_list.Catalog(slow)
        try:
            t0 = time.monotonic()
            code, _ = self.req("POST", "/v1/chat/completions", {"model": "Orca-IQ3_XXS", "max_tokens": 4,
                                                                 "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(code, 200)
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            gate.set()

    def test_a_standby_server_without_a_model_lists_the_others(self):
        tok = ByteTokenizer()
        svc = Service(NoEngine(), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"), model_name="no model loaded")
        svc.standby = True
        svc.model_catalog = model_list.Catalog(lambda: list(self.items))
        svc.model_catalog.refresh()
        httpd = serve(svc, port=0)
        port = httpd.server_address[1]
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", "/v1/models")
            data = json.loads(c.getresponse().read())["data"]
            c.close()
            self.assertEqual([m["status"]["value"] for m in data], ["unloaded"] * 3)
            self.assertNotIn("no model loaded", [m["id"] for m in data])
            self.assertIn("no model is loaded", model_list.refusal(svc, "Orca-IQ3_XXS", True))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_without_a_catalog_only_the_served_model(self):
        del self.svc.model_catalog
        code, body = self.req("GET", "/v1/models")
        self.assertEqual([m["id"] for m in body["data"]], [SERVED])


class Folders(unittest.TestCase):
    """folder_models(): the Loader's scan, its ready .basalt files only, with stable ids."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": str(root / "cache"), "BASALT_DATA": str(root / "data")})
        self.env.start()
        models._cache = None
        self.a, self.b = root / "a", root / "b"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self):
        self.env.stop()
        models._cache = None
        self.dir.cleanup()

    def test_ready_files_with_their_context(self):
        basalt_with_context(self.a / "Flash-IQ3_XXS.basalt", "Flash Next", 262144)
        tiny_basalt(self.a / "Tiny-IQ4_NL.basalt", "Tiny", vision=False)
        tiny_basalt(self.b / "Tiny-IQ4_NL.basalt", "Tiny copy", vision=False)       # the same file name elsewhere
        (self.a / "broken.basalt").write_bytes(b"NOTBASALT" + b"\0" * 64)           # not compatible: not listed
        self.assertEqual(model_list.folder_models(), [])                           # no folders yet
        models.set_dirs([str(self.a), str(self.b)])
        got = model_list.folder_models()
        self.assertEqual(len(got), 3)
        flash = next(e for e in got if e["name"] == "Flash Next")
        self.assertEqual((flash["id"], flash["context_length"], flash["quant"]), ("Flash-IQ3_XXS", 262144, "IQ3_XXS"))
        tiny = [e for e in got if e["name"].startswith("Tiny")]
        self.assertEqual(len({e["id"] for e in tiny}), 2)                          # told apart by a hash
        self.assertTrue(all(e["id"].startswith("Tiny-IQ4_NL-") for e in tiny))
        self.assertTrue(all("tiny-iq4_nl.basalt" in e["names"] for e in tiny))
        self.assertEqual(model_list.folder_models(), got)                          # stable


class ProfileSummaries(unittest.TestCase):
    """The Loader's load-profile summaries while a model is loaded: planned with the engine's memory given back, as
    the Loader's own plan, not against the little the running engine leaves free."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        # the PC with the model loaded: the engine holds 29 GiB of VRAM and 60 GiB of RAM and commit
        busy = {**ROOMY, "vram_free": 2 * GiB, "ram_available": 60 * GiB, "commit_used": 70 * GiB}
        self.patches = [mock.patch.object(loader, "system_now", return_value=dict(busy)),
                        mock.patch.object(loader, "engine_holds", return_value={"ram": 60 * GiB, "commit": 60 * GiB})]
        for p in self.patches:
            p.start()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.dir.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"} if body is not None else {})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def test_summaries_match_the_loaders_plan(self):
        values = {"engine.max-context": 131072, "engine.kv": "q4_0", "engine.spec": 7}
        profiles.change({"action": "save", "kind": "load", "name": "Machine Default", "values": values})
        code, lst = self.req("GET", "/api/profiles")
        self.assertEqual(code, 200)
        s = next(p for p in lst["load"] if p["name"] == "Machine Default")["summary"]
        code, plan = self.req("POST", "/api/loader/plan", {"values": values})
        self.assertEqual(code, 200)
        self.assertEqual(s["vram_gb"], round(plan["vram"]["need"] / 1e9, 1))
        self.assertEqual(s["tok_s"], round(plan["speed"]["decode"]))
        # without the engine's memory given back the cache would get almost nothing of the GPU
        holds_free = profiles.summary(values, loader.DEMO_FACTS, loader.system_now())
        self.assertGreater(s["vram_gb"], holds_free["vram_gb"])
        self.assertGreater(s["tok_s"], holds_free["tok_s"])
        safe = lst["load"][0]
        self.assertEqual(safe["name"], profiles.SAFE_NAME)
        self.assertGreater(safe["summary"]["vram_gb"], holds_free["vram_gb"])


if __name__ == "__main__":
    unittest.main()
