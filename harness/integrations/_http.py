"""Small JSON-over-HTTP helper shared by the integration clients.

Redirects are never followed, so a credential in a header cannot be forwarded to
another host, and every failure is reported as a ServiceError with a kind.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class ServiceError(Exception):
    """kind is one of: offline (never reached), unauthorized, not_found, bad_response, http_error,
    timeout (sent, no answer in time)."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


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
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ServiceError("unauthorized", f"{url}: HTTP {e.code}", e.code) from None
        if e.code == 404:
            raise ServiceError("not_found", f"{url}: HTTP 404", 404) from None
        raise ServiceError("http_error", f"{url}: HTTP {e.code}", e.code) from None
    except TimeoutError:
        raise ServiceError("timeout", f"{url}: timed out") from None
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        # A timeout while connecting arrives wrapped here: the request was never sent.
        raise ServiceError("offline", f"{url}: {e}") from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ServiceError("bad_response", f"{url}: the answer is not JSON") from None
