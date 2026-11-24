"""The Chat page and the voice service while the local model is out for a heavy job (docs/heavy-swap.md): their requests to `/v1/chat/completions` go to the
provider the swap named, with the provider's key and model, and the answer is handed back as it arrives. Only the page of this PC asks for it (the header
`X-Tadamoo-Function: chat`) and the voice service with its token (`voice`: the copilot, the meetings, the calls and the avatar's direct brain); any other program
that calls the server still meets the local model and the 503 that says why it is out."""
from __future__ import annotations

import json
import re

import requests

from serve import settings
from tadamoo import untrusted
from tadamoo.llm.usage import counts as usage_counts
from tadamoo.llmswitch.providers import TYPES
from tadamoo.rag.recall import CLOSING, SOURCE
from tadamoo.rag.rt_block import LOCAL_BLOCK

HEADER = "X-Tadamoo-Function"
FUNCTIONS = ("chat", "voice")
KEEP = ("messages", "model", "stream", "temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice", "response_format", "presence_penalty",
        "frequency_penalty", "seed", "stream_options")        # what an OpenAI-style provider takes; the page's own fields stay here
ARCHIVE = re.compile(rf'<{untrusted.TAG} source="{re.escape(SOURCE)}"(?: id="([0-9a-f]+)")?>.*?</{untrusted.TAG}(?: id="\1")?>\n?(?:{re.escape(CLOSING)})?', re.S)
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "server", "date"}


def function_of(handler) -> str:
    """The function a request names: `chat` from the page of this PC, `voice` only from the voice service (its token); "" for any other."""
    name = handler.headers.get(HEADER)
    if name not in FUNCTIONS or handler.remote_listener or not settings.is_loopback(handler.client_address[0]):
        return ""
    return name if name == "chat" or getattr(handler, "_via_voice", False) else ""


def wants(handler, svc) -> bool:
    """Whether this request is the Chat page's or the voice service's and a swap is on."""
    if not function_of(handler):
        return False
    swap = getattr(svc, "heavy_swap", None)
    return swap is not None and swap.registry.swap() is not None


def forward(handler, svc, raw: bytes) -> bool:
    """True when the request was answered here; False when the swap provider cannot take it (the request is then answered as it would be without a swap)."""
    reg = svc.heavy_swap.registry
    function = function_of(handler) or "chat"
    prov = reg.provider_for(function)
    resolved = reg.endpoint_for(function, carica=False)
    if not prov or not resolved or TYPES.get(prov.get("type") or "", {}).get("api") != "openai":
        return False
    base, key, model = resolved
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            return False
    except ValueError:
        return False
    body = {k: v for k, v in body.items() if k in KEEP}
    body["messages"] = without_archives(body.get("messages"))
    body["model"] = model or body.get("model")
    try:
        reply = requests.post(base.rstrip("/") + "/chat/completions", json=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                              stream=True, timeout=(15, 600))
    except requests.RequestException as e:
        handler._json(502, {"error": {"type": "server_error", "message": f"{prov.get('name') or prov['id']} did not answer: {e}"}})
        return True
    tokens, said = 0, {}
    try:
        handler.send_response(reply.status_code)
        for k, v in reply.headers.items():
            if k.lower() not in HOP and not k.lower().startswith("access-control"):
                handler.send_header(k, v)
        handler._cors()
        handler.end_headers()
        for chunk in reply.iter_content(chunk_size=None):
            if not chunk:
                continue
            tokens = max(tokens, _usage(chunk))
            said = _counts(chunk) or said
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (ConnectionError, OSError):
        pass                                                         # the page left: closing the relay ends the request
    finally:
        reply.close()
        handler.close_connection = True
        if tokens:
            _record(prov["id"], tokens)
        log = getattr(svc, "usage_log", None)
        if log is not None and said:
            log.record(prov["id"], prov.get("name") or prov["id"], model or "", function, said["prompt"], said["completion"], said["cached"])
    return True


def without_archives(messages):
    """The messages without the passages of knowledge archives the page or the voice service put in them: which archives they came from is not known here, and an
    archive not cleared for cloud models must not leave this PC. A voice block composed for a provider outside this PC (`knowledge-archive-cloud`) holds only
    cleared archives and stays."""
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            m = {**m, "content": _strip(m["content"]).strip()}
        elif isinstance(m, dict) and isinstance(m.get("content"), list):
            m = {**m, "content": [{**p, "text": _strip(p["text"]).strip()} if isinstance(p, dict) and isinstance(p.get("text"), str) else p for p in m["content"]]}
        out.append(m)
    return out


def _strip(text: str) -> str:
    return LOCAL_BLOCK.sub("", ARCHIVE.sub("", text))


def _counts(chunk: bytes) -> dict:
    """The prompt, completion and cached tokens a piece of the answer reports (empty when it reports none)."""
    best: dict = {}
    for line in chunk.decode("utf-8", "replace").splitlines():
        text = line[5:].strip() if line.startswith("data:") else line.strip()
        if '"total_tokens"' not in text:
            continue
        try:
            best = usage_counts(json.loads(text) or {}) or best
        except (ValueError, TypeError, AttributeError):
            continue
    return best


def _usage(chunk: bytes) -> int:
    """The total tokens a piece of the answer reports, 0 when it reports none."""
    best = 0
    for line in chunk.decode("utf-8", "replace").splitlines():
        text = line[5:].strip() if line.startswith("data:") else line.strip()
        if '"total_tokens"' not in text:
            continue
        try:
            usage = (json.loads(text) or {}).get("usage") or {}
            best = max(best, int(usage.get("total_tokens") or 0))
        except (ValueError, TypeError, AttributeError):
            continue
    return best


def _record(pid: str, tokens: int) -> None:
    try:
        from tadamoo.llm import Ledger
        Ledger().record(pid, tokens)
    except Exception:  # noqa: BLE001 - the count is a courtesy
        pass
