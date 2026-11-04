"""Small JSON-over-HTTP helper shared by the integration clients.

Redirects are never followed, so a credential in a header cannot be forwarded to
another host, and every failure is reported as a ServiceError with a kind.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Iterator, Optional


class ServiceError(Exception):
    """kind is one of: offline (never reached: nothing was sent), unauthorized, not_found, bad_response, http_error,
    timeout (sent, no answer in time), interrupted (sent, the connection broke before a whole answer came: the service may have acted)."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.detail = detail            # what the service said about the refusal, when it said anything


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _refusal_text(e: urllib.error.HTTPError) -> str:
    """The `error` field of a JSON refusal, or ""."""
    try:
        body = json.loads(e.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return ""
    return str(body.get("error", ""))[:300] if isinstance(body, dict) else ""


def _send(req: urllib.request.Request, url: str, timeout: float) -> Any:
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = _refusal_text(e)
        if e.code in (401, 403):
            raise ServiceError("unauthorized", f"{url}: HTTP {e.code}", e.code, detail) from None
        if e.code == 404:
            raise ServiceError("not_found", f"{url}: HTTP 404", 404, detail) from None
        raise ServiceError("http_error", f"{url}: HTTP {e.code}", e.code, detail) from None
    except TimeoutError:
        raise ServiceError("timeout", f"{url}: timed out") from None
    except urllib.error.URLError as e:
        # Wrapped here: a refusal or a timeout while connecting (nothing was sent), or a connection that broke while sending.
        if isinstance(e.reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            raise ServiceError("interrupted", f"{url}: the connection broke ({type(e.reason).__name__})") from None
        raise ServiceError("offline", f"{url}: {e}") from None
    except (ConnectionError, http.client.HTTPException) as e:
        # Raised after the request went out (the service closed the connection, or sent half an answer): it may have acted.
        raise ServiceError("interrupted", f"{url}: the connection broke ({type(e).__name__})") from None
    except OSError as e:
        raise ServiceError("offline", f"{url}: {e}") from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ServiceError("bad_response", f"{url}: the answer is not JSON") from None


def request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 5.0,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    return _send(urllib.request.Request(url, data=data, headers=hdrs, method=method), url, timeout)


_CHUNK = 1 << 20


def _multipart_parts(head: bytes, path: str, tail: bytes) -> Iterator[bytes]:
    yield head
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            yield chunk
    yield tail


def request_multipart(
    url: str,
    fields: Dict[str, str],
    file_field: str,
    path: str,
    filename: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Any:
    """POST of form fields and one file, sent in chunks: a long recording is never held in memory."""
    boundary = uuid.uuid4().hex
    safe_name = "".join("_" if c in '"\r\n' else c for c in filename)
    form = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
        for k, v in fields.items())
    head = form + (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
                   "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    hdrs = {"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + os.path.getsize(path) + len(tail)), **(headers or {})}
    req = urllib.request.Request(url, data=_multipart_parts(head, path, tail), headers=hdrs, method="POST")
    return _send(req, url, timeout)
