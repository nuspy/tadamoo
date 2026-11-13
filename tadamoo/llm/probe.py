"""What the provider form asks of a provider before anything is saved: the list of its models, and one minimal request.

Both take the values as they are in the form (kind, address, key, model); a key that was left empty comes from the one already saved for
that provider. The key is only ever sent to the provider; what is said back to the page has it taken out.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import requests

from tadamoo.llmswitch import providers as _p
from tadamoo.llmswitch.providers import TYPES

TIMEOUT = 20.0


def _detail(resp: "requests.Response") -> str:
    try:
        err = resp.json().get("error", {})
        text = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        text = ""
    return f"HTTP {resp.status_code}" + (f": {text}" if text else "")


def clean(text: str, prov: Dict[str, Any]) -> str:
    """The text without the key (and the token) of the provider, whatever way the provider repeated it."""
    out = str(text)
    for secret in (prov.get("api_key"), (prov.get("oauth") or {}).get("access_token")):
        if secret and len(str(secret)) >= 6:
            out = out.replace(str(secret), "***")
    return out[:300]


def _address(prov: Dict[str, Any], chat: bool = False) -> str:
    spec = TYPES.get(prov.get("type") or "", {})
    if chat and spec.get("chat_base_url") and not (prov.get("base_url") or "").strip():
        return spec["chat_base_url"]
    return ((prov.get("base_url") or "").strip() or spec.get("base_url") or "").rstrip("/")


def list_models(prov: Dict[str, Any]) -> List[Dict[str, str]]:
    """The models the provider lists (`id` and `label`), asked in the provider's own way. Raises RuntimeError with a message fit for the person."""
    spec = TYPES.get(prov.get("type") or "", {})
    api = spec.get("api", "")
    token = ((prov.get("oauth") or {}).get("access_token") or "").strip()
    key = (prov.get("api_key") or "").strip()
    base = ""
    try:
        if api == "cli":
            return _p._cli_models(prov, spec)
        if api == "judge" or spec.get("this_server"):
            raise RuntimeError("this kind has no list of models to load")
        base = _address(prov)
        if not base:
            raise RuntimeError("enter the address first")
        if api == "anthropic":
            return _p._anthropic_models(base, key)
        if api == "gemini":
            return _p._gemini_models(base, key, token)
        return _p._openai_models(base, token or key)
    except requests.HTTPError as exc:
        raise RuntimeError(clean(_detail(exc.response) if exc.response is not None else str(exc), prov)) from None
    except requests.RequestException as exc:
        raise RuntimeError(clean(f"cannot reach {base}: {exc}", prov)) from None
    except RuntimeError as exc:
        raise RuntimeError(clean(str(exc), prov)) from None


def try_request(prov: Dict[str, Any], session: Any = None) -> Dict[str, Any]:
    """One minimal request with the form's values. {"ok": True, "ms": n} or {"ok": False, "error": the provider's own words}."""
    spec = TYPES.get(prov.get("type") or "", {})
    api = spec.get("api", "")
    http = session or requests
    key = (prov.get("api_key") or "").strip()
    token = ((prov.get("oauth") or {}).get("access_token") or "").strip()
    if spec.get("this_server"):
        return {"ok": True, "ms": 0, "note": "this server"}
    started = time.monotonic()
    try:
        if api == "judge":
            return {"ok": bool(key), "ms": 0, "error": "" if key else "no key yet"}
        if api == "cli":
            _p._cli_models(prov, spec)
            return {"ok": True, "ms": round((time.monotonic() - started) * 1000)}
        model = (prov.get("model") or "").strip()
        base = _address(prov, chat=True)
        if not base:
            return {"ok": False, "error": "enter the address first"}
        if api == "anthropic":
            resp = http.post(base + "/messages", timeout=TIMEOUT, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                             data=json.dumps({"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with OK."}]}))
        else:
            headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token or key}"} if (token or key) else {})}
            body: Dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}
            resp = http.post(base + "/chat/completions", data=json.dumps(body), headers=headers, timeout=TIMEOUT)
            if resp.status_code == 400 and "max_completion_tokens" in resp.text:
                body["max_completion_tokens"] = body.pop("max_tokens")           # a model that wants the newer name for the same limit
                resp = http.post(base + "/chat/completions", data=json.dumps(body), headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return {"ok": False, "error": clean(f"cannot reach the address: {exc}", prov)}
    except RuntimeError as exc:
        return {"ok": False, "error": clean(str(exc), prov)}
    ms = round((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        return {"ok": False, "ms": ms, "error": clean(_detail(resp), prov)}
    return {"ok": True, "ms": ms}
