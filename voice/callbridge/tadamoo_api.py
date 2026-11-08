"""TaDaMoo's HTTP API as the voice service uses it: loopback only, with the service's token.

The server accepts the voice service's token (`voice-service.token`, the one the service checks on its own routes) on loopback for the routes
the brains need, and for nothing else. The service never imports `serve` or `tadamoo`."""
from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from urllib.parse import urlsplit

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ApiError(Exception):
    """A request to TaDaMoo failed. `status` is the HTTP status, 0 when the server cannot be reached."""

    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status, self.code = status, code


def error_text(body) -> tuple[str, str]:
    """(message, code) from a TaDaMoo error body."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or ""), str(err.get("code") or err.get("type") or "")
    return (str(err) if err else ""), ""


class TadamooApi:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        parts = urlsplit(base_url if "//" in base_url else "//" + base_url)
        host = parts.hostname or ""
        if parts.scheme not in ("", "http") or host not in LOOPBACK:
            raise ValueError("TaDaMoo's address must be http on this PC (127.0.0.1)")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = parts.port or 80
        self.token = token
        self.timeout = timeout

    def _connect(self, timeout: float | None = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)

    def _headers(self, body: bool) -> dict:
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        conn = self._connect()
        try:
            conn.request(method, path, body=payload, headers=self._headers(payload is not None))
            r = conn.getresponse()
            raw = r.read(8 << 20)
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(f"TaDaMoo does not answer ({e.__class__.__name__})") from None
        finally:
            conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return r.status, data if isinstance(data, dict) else None

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        """The JSON object of a successful answer; ApiError carrying TaDaMoo's own message otherwise."""
        status, data = self.request(method, path, body)
        if status >= 400 or data is None:
            message, code = error_text(data)
            raise ApiError(message or f"TaDaMoo answered {status}", status, code)
        return data

    def get(self, path: str) -> dict:
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self.call("POST", path, body if body is not None else {})

    def chat_stream(self, messages: list[dict], max_tokens: int = 400, timeout: float = 120.0) -> Iterator[str]:
        """The `content` pieces of a streamed /v1/chat/completions answer. Reasoning pieces are not returned."""
        payload = json.dumps({"messages": messages, "stream": True, "max_tokens": max_tokens}).encode("utf-8")
        conn = self._connect(timeout)
        try:
            try:
                conn.request("POST", "/v1/chat/completions", body=payload, headers=self._headers(True))
                r = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                raise ApiError(f"TaDaMoo does not answer ({e.__class__.__name__})") from None
            if r.status >= 400:
                try:
                    data = json.loads(r.read(1 << 16))
                except ValueError:
                    data = None
                message, code = error_text(data)
                raise ApiError(message or f"TaDaMoo answered {r.status}", r.status, code)
            while True:
                try:
                    line = r.readline()
                except (OSError, http.client.HTTPException) as e:
                    raise ApiError(f"the answer was cut ({e.__class__.__name__})") from None
                if not line:
                    return
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    yield piece
        finally:
            conn.close()
