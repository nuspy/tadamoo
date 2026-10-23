"""In-process fakes of the wikibridge and callAPIcall HTTP contracts, for tests that must never dial out."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


class _FakeServer:
    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Dict[str, str], Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append((method, self.path, headers, body))
                status, payload = outer.route(method, urlparse(self.path), parse_qs(urlparse(self.path).query), headers, body)
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method: str, url: Any, query: Dict[str, List[str]], headers: Dict[str, str], body: Any) -> Tuple[int, Any]:
        raise NotImplementedError

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeWikibridge(_FakeServer):
    def __init__(self, key: Optional[str] = None) -> None:
        self.key = key
        super().__init__()

    def route(self, method, url, query, headers, body):
        if self.key and url.path != "/health" and headers.get("x-wikibridge-key") != self.key:
            return 401, {"error": "non autorizzato"}
        if url.path == "/health":
            return 200, {"ok": True}
        if url.path == "/projects":
            return 200, [{"slug": "cervinia", "name": "Cervinia"}, {"slug": "work", "name": "Work"}]
        if url.path == "/search":
            return 200, {"project": query["project"][0], "hits": [{"path": "entities/diana.md", "title": "Diana"}]}
        if url.path == "/wiki/page":
            if query["path"][0] == "entities/diana.md":
                return 200, {"path": "entities/diana.md", "frontmatter": {}, "body": "Allergica agli ovini."}
            return 404, {"error": "pagina non trovata"}
        if url.path == "/rag/context" and method == "POST":
            return 200, {"blocks": [{"path": "entities/diana.md", "title": "Diana", "text": "Allergica agli ovini."}],
                         "truncated": False, "chars": 21}
        return 404, {"error": "not found"}


class FakeCallAPICall(_FakeServer):
    """Records the calls it is asked to place; no number is ever dialled."""

    def __init__(self, token: str = "t0ken") -> None:
        self.token = token
        self.placed: List[Dict[str, Any]] = []
        self.said: List[str] = []
        self.ended = False
        super().__init__()

    def route(self, method, url, query, headers, body):
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"error": "unauthorized"}
        path = url.path
        if path == "/external/call" and method == "POST":
            if not (body or {}).get("greeting"):
                return 400, {"error": "campo 'greeting' mancante"}
            self.placed.append(body)
            return 200, {"call_id": "c1", "status": "calling", "transport": body.get("transport") or "gsm"}
        if not path.startswith("/external/"):
            return 404, {"error": "not found"}
        _, _, call_id, *rest = path.split("/")
        action = rest[0] if rest else ""
        if call_id != "c1":
            return 404, {"error": f"chiamata sconosciuta: {call_id}"}
        if action == "say":
            self.said.append(body["text"])
            return 200, {"status": "queued", "call_id": call_id}
        if action == "heard":
            return 200, {"call_id": call_id, "status": "active", "text": "Pronto?", "index": int(query["after"][0]) + 1}
        if action == "transcript":
            return 200, {"call_id": call_id, "transcript": [{"speaker": "assistant", "text": "Buongiorno"},
                                                            {"speaker": "callee", "text": "Pronto?"}]}
        if action == "state":
            return 200, {"call_id": call_id, "status": "ended" if self.ended else "active"}
        if action == "hangup":
            self.ended = True
            return 200, {"call_id": call_id, "status": "ended"}
        return 404, {"error": "not found"}
