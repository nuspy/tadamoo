"""Other programs that call this server through the OpenAI or Anthropic interface (an editor, a script, another app) while the function `external` is pointed
at a provider: the request is carried to that provider and its answer is handed back in the interface the program spoke. Without a choice the function is the
model loaded in this server and nothing here runs.

Served: `/v1/chat/completions` and `/v1/messages` (streamed or not, with tools), `/v1/messages/count_tokens` (an estimate), `/v1/models`. `/v1/responses` is
answered only by the loaded model, and with a provider chosen it says so. TaDaMoo's own pieces (the Chat page, the voice service, its own clients) are never
carried away: they name themselves by a header or by `TaDaMoo` in the user agent.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from tadamoo import llm
from tadamoo.client import TadamooError
from tadamoo.llm.client import RoutedClient
from tadamoo.llm.usage import UsageLog

FUNCTION = "external"
INTERNAL_HEADERS = ("X-Tadamoo-Function", "X-Tadamoo-Compress", "X-Tadamoo-Bili-Hop", "X-Tadamoo-Session", "X-Tadamoo-Control")
CARRIED = ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens", "/v1/responses")
OPENAI_EXTRA = ("top_p", "stop", "seed", "presence_penalty", "frequency_penalty")
TIMEOUT = 600.0
STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


class Gone(Exception):
    """The program that asked left before the answer was complete."""


def is_internal(headers) -> bool:
    """A request of TaDaMoo's own pieces, which are answered by the function they name and not by this one."""
    return str(headers.get("User-Agent") or "").startswith("TaDaMoo") or any(headers.get(h) for h in INTERNAL_HEADERS)


def chosen(reg=None) -> Optional[Dict[str, Any]]:
    """The provider the function is pointed at, or None when it is the loaded model."""
    reg = reg or llm.registry()
    pid, _ = reg.resolve_route(FUNCTION, True)
    return reg._find(reg.load(), pid) if pid else None


def model_name(reg=None) -> str:
    """The model id the programs are shown: the provider's own."""
    reg = reg or llm.registry()
    found = reg.endpoint_for(FUNCTION, carica=False)
    prov = chosen(reg) or {}
    return (found[2] if found else "") or prov.get("model") or prov.get("name") or prov.get("id") or "model"


def wants(handler, path: str) -> bool:
    return path in CARRIED and listed(handler)


def listed(handler) -> bool:
    """Whether a program outside TaDaMoo is to be answered by a provider."""
    return not is_internal(handler.headers) and chosen() is not None


def models(anthropic: bool) -> Dict[str, Any]:
    """The one model the programs are shown, in the shape of the interface they speak."""
    reg = llm.registry()
    model, owner = model_name(reg), (chosen(reg) or {}).get("name") or "provider"
    if anthropic:
        return {"data": [{"type": "model", "id": model, "display_name": model, "created_at": "1970-01-01T00:00:00Z"}], "has_more": False, "first_id": model, "last_id": model}
    return {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": owner}]}


# -- what a program sends -------------------------------------------------------------------------------------------

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _part(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text", "")}
    if block.get("type") == "image":
        src = block.get("source") or {}
        url = src.get("url") if src.get("type") == "url" else f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def anthropic_messages(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The messages of an Anthropic request as chat messages: the system text, tool results as `tool` messages, tool uses as `tool_calls`."""
    out: List[Dict[str, Any]] = []
    system = _text(req.get("system"))
    if system:
        out.append({"role": "system", "content": system})
    given = req.get("messages")
    if not isinstance(given, list):
        raise ValueError("messages: send a list")
    for m in given:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = [b for b in content or [] if isinstance(b, dict)]
        if role == "assistant":
            calls = [{"id": b.get("id"), "type": "function", "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            message: Dict[str, Any] = {"role": "assistant", "content": _text(blocks) or None}
            if calls:
                message["tool_calls"] = calls
            out.append(message)
            continue
        for b in blocks:                                                     # the tool results come first: they answer the assistant's calls
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": _text(b.get("content"))})
        parts = [p for p in map(_part, blocks) if p]
        if parts:
            out.append({"role": role, "content": parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text" else parts})
    return out


def anthropic_tools(req: Dict[str, Any]):
    tools = [{"type": "function", "function": {"name": t.get("name"), "description": t.get("description", ""), "parameters": t.get("input_schema") or {"type": "object"}}}
             for t in req.get("tools") or [] if isinstance(t, dict) and t.get("name")]
    choice = req.get("tool_choice") or {}
    kind = choice.get("type") if isinstance(choice, dict) else None
    pick = {"auto": "auto", "any": "required", "none": "none"}.get(kind)
    if kind == "tool":
        pick = {"type": "function", "function": {"name": choice.get("name")}}
    return tools or None, pick if tools else None


def call_args(path: str, req: Dict[str, Any]) -> Dict[str, Any]:
    """What `RoutedClient.chat_completion` takes, from a request in either interface."""
    if path.startswith("/v1/messages"):
        tools, choice = anthropic_tools(req)
        extra = {k: req[k] for k in ("top_p",) if k in req}
        if req.get("stop_sequences"):
            extra["stop"] = req["stop_sequences"]
        return {"messages": anthropic_messages(req), "tools": tools, "tool_choice": choice, "temperature": req.get("temperature"),
                "max_tokens": req.get("max_tokens"), "extra": extra or None}
    messages = req.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages: send a list")
    extra = {k: req[k] for k in OPENAI_EXTRA if k in req}
    return {"messages": messages, "tools": req.get("tools") or None, "tool_choice": req.get("tool_choice"), "temperature": req.get("temperature"),
            "max_tokens": req.get("max_completion_tokens") or req.get("max_tokens"), "response_format": req.get("response_format"), "extra": extra or None}


def estimate_tokens(req: Dict[str, Any]) -> int:
    """About a quarter of a character a token: what a program that only wants to size its request needs; the provider's own count comes with its answer."""
    return max(1, math.ceil(len(json.dumps({k: req.get(k) for k in ("system", "messages", "tools")}, ensure_ascii=False)) / 4))


# -- what is sent back ----------------------------------------------------------------------------------------------

def _message(result: Dict[str, Any]) -> Dict[str, Any]:
    return (result.get("choices") or [{}])[0].get("message") or {}


def _finish(result: Dict[str, Any]) -> str:
    return (result.get("choices") or [{}])[0].get("finish_reason") or "stop"


def _usage(result: Dict[str, Any]) -> Dict[str, int]:
    u = result.get("usage") or {}
    prompt, done = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": done, "total_tokens": int(u.get("total_tokens") or prompt + done)}


def _arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def openai_body(result: Dict[str, Any], model: str) -> Dict[str, Any]:
    message = _message(result)
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": message.get("content"), **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})},
                         "finish_reason": _finish(result)}], "usage": _usage(result)}


def anthropic_body(result: Dict[str, Any], model: str) -> Dict[str, Any]:
    message = _message(result)
    content: List[Dict[str, Any]] = [{"type": "text", "text": message["content"]}] if message.get("content") else []
    content += [{"type": "tool_use", "id": c.get("id") or "toolu_" + uuid.uuid4().hex[:20], "name": (c.get("function") or {}).get("name"), "input": _arguments(c)}
                for c in message.get("tool_calls") or []]
    u = _usage(result)
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": STOP.get(_finish(result), "end_turn"), "stop_sequence": None, "usage": {"input_tokens": u["prompt_tokens"], "output_tokens": u["completion_tokens"]}}


class OpenAIStream:
    """The answer as `chat.completion.chunk`s: the text as it arrives, then the tool calls whole, the finish and the usage."""

    def __init__(self, send: Callable[[Optional[str], Dict[str, Any]], None], model: str) -> None:
        self.send, self.model, self.id, self.began = send, model, "chatcmpl-" + uuid.uuid4().hex[:24], False

    def _chunk(self, delta: Dict[str, Any], finish: Optional[str] = None, **extra: Any) -> None:
        if not self.began:
            delta, self.began = {"role": "assistant", **delta}, True
        self.send(None, {"id": self.id, "object": "chat.completion.chunk", "created": int(time.time()), "model": self.model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})

    def text(self, piece: str) -> None:
        self._chunk({"content": piece})

    def finish(self, result: Dict[str, Any], spoken: bool) -> None:
        message = _message(result)
        if message.get("content") and not spoken:
            self.text(message["content"])
        calls = message.get("tool_calls") or []
        if calls:
            self._chunk({"tool_calls": [{"index": i, "id": c.get("id"), "type": "function", "function": c.get("function") or {}} for i, c in enumerate(calls)]})
        self._chunk({}, _finish(result))
        self.send(None, {"id": self.id, "object": "chat.completion.chunk", "created": int(time.time()), "model": self.model, "choices": [], "usage": _usage(result)})
        self.send("[DONE]", {})


class AnthropicStream:
    """The answer as Anthropic's events: a text block that grows, then one block for each tool use."""

    def __init__(self, send: Callable[[Optional[str], Dict[str, Any]], None], model: str) -> None:
        self.send, self.model, self.began, self.index, self.text_open = send, model, False, 0, False

    def _start(self) -> None:
        if not self.began:
            self.began = True
            self.send("message_start", {"type": "message_start", "message": {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": self.model,
                                                                          "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def text(self, piece: str) -> None:
        self._start()
        if not self.text_open:
            self.text_open = True
            self.send("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": {"type": "text", "text": ""}})
        self.send("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": {"type": "text_delta", "text": piece}})

    def _close_text(self) -> None:
        if self.text_open:
            self.send("content_block_stop", {"type": "content_block_stop", "index": self.index})
            self.text_open, self.index = False, self.index + 1

    def finish(self, result: Dict[str, Any], spoken: bool) -> None:
        message = _message(result)
        if message.get("content") and not spoken:
            self.text(message["content"])
        self._start()
        self._close_text()
        for c in message.get("tool_calls") or []:
            self.send("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": {
                "type": "tool_use", "id": c.get("id") or "toolu_" + uuid.uuid4().hex[:20], "name": (c.get("function") or {}).get("name"), "input": {}}})
            self.send("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                              "delta": {"type": "input_json_delta", "partial_json": (c.get("function") or {}).get("arguments") or "{}"}})
            self.send("content_block_stop", {"type": "content_block_stop", "index": self.index})
            self.index += 1
        self.send("message_delta", {"type": "message_delta", "delta": {"stop_reason": STOP.get(_finish(result), "end_turn"), "stop_sequence": None},
                                    "usage": {"output_tokens": _usage(result)["completion_tokens"]}})
        self.send("message_stop", {"type": "message_stop"})


def error_body(anthropic: bool, message: str, kind: str = "api_error") -> Dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}} if anthropic else {"error": {"type": kind if kind != "api_error" else "server_error", "message": message}}


# -- the request ----------------------------------------------------------------------------------------------------

def forward(handler, path: str, raw: bytes) -> None:
    """Answers the request with the provider the function is pointed at. Only called when `wants` said yes."""
    anthropic = path.startswith("/v1/messages")

    def fail(status: int, message: str, kind: str = "api_error") -> None:
        handler._json(status, error_body(anthropic, message, kind))

    try:
        req = json.loads(raw or b"{}")
        if not isinstance(req, dict):
            raise ValueError("send a JSON object")
    except ValueError as e:
        return fail(400, str(e), "invalid_request_error")
    reg = llm.registry()
    prov = chosen(reg) or {}
    name = prov.get("name") or prov.get("id")
    if path == "/v1/responses":
        return fail(501, f"this server answers /v1/responses only with the loaded model; other programs are set to {name}, which is reached through "
                         "/v1/chat/completions and /v1/messages", "invalid_request_error")
    if path == "/v1/messages/count_tokens":
        return handler._json(200, {"input_tokens": estimate_tokens(req)})
    try:
        args = call_args(path, req)
    except ValueError as e:
        return fail(400, str(e), "invalid_request_error")
    model = req.get("model") if isinstance(req.get("model"), str) and req.get("model") else model_name(reg)
    client = RoutedClient(FUNCTION, reg, None, usage=UsageLog())
    if not req.get("stream"):
        try:
            result = client.chat_completion(timeout=TIMEOUT, **args)
        except TadamooError as e:
            return fail(e.status if 400 <= e.status < 600 else 502, str(e))
        return handler._json(200, (anthropic_body if anthropic else openai_body)(result, model))
    started = [False]

    def send(event: Optional[str], data: Dict[str, Any]) -> None:
        if not started[0]:
            started[0] = True
            handler._sse()
        try:
            line = "data: [DONE]\n\n" if event == "[DONE]" else (f"event: {event}\n" if event else "") + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            handler.wfile.write(line.encode())
            handler.wfile.flush()
        except OSError:
            raise Gone() from None

    out = (AnthropicStream if anthropic else OpenAIStream)(send, model)
    spoken = [False]

    def on_delta(kind: str, text: str) -> None:
        if kind == "content" and text:
            spoken[0] = True
            out.text(text)
    try:
        result = client.chat_completion(timeout=TIMEOUT, on_delta=on_delta, **args)
        out.finish(result, spoken[0])
    except Gone:
        handler.close_connection = True
    except TadamooError as e:
        if not started[0]:
            return fail(e.status if 400 <= e.status < 600 else 502, str(e))
        try:
            send("error" if anthropic else None, error_body(anthropic, str(e)))
        except Gone:
            pass
        handler.close_connection = True
