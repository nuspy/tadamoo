"""A client for each function: it asks the provider the function is pointed at, and falls back only to a route as local or more.

`RoutedClient` has the same `chat_completion` as `BasaltClient`, so the agent's loop, the summaries and the judges use it
without knowing where the words come from. With nothing assigned it IS the loaded model of this Basalt. With a provider
assigned it speaks that provider's dialect (OpenAI-compatible, Anthropic, a command-line agent for text), translates the
reasoning level, counts the tokens against the provider's daily cap and refuses, with the reason, what the registry's rules
do not allow. Errors are the same `BasaltError`s the loop already knows how to retry.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import requests

from harness.client import BasaltError, _collect_stream
from harness.llm import functions
from harness.llm.store import Ledger, SecureRegistry, is_local
from harness.llmswitch import reasoning
from harness.llmswitch.providers import TYPES

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


class NotAllowed(BasaltError):
    """The route is not allowed (consent, a daily cap, a provider that cannot do the work): not worth trying again."""

    def __init__(self, message: str) -> None:
        super().__init__(400, message)


def _lines(resp: "requests.Response"):
    for raw in resp.iter_lines():
        yield raw + b"\n"


def _http_error(prov: Dict[str, Any], resp: "requests.Response") -> BasaltError:
    try:
        detail = resp.json().get("error", {})
        detail = detail.get("message") if isinstance(detail, dict) else str(detail)
    except ValueError:
        detail = ""
    return BasaltError(resp.status_code, f"{prov.get('name') or prov.get('id')}: {detail or 'HTTP ' + str(resp.status_code)}")


# -- Anthropic: the messages and the tools in its own shape ----------------------------------------------------

def to_anthropic(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system" and isinstance(m.get("content"), str))
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "call", "name": fn["name"], "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": str(m.get("content") or "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            text = m.get("content")
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], str):
                out[-1]["content"] += "\n\n" + str(text)
            else:
                out.append({"role": "user", "content": text if isinstance(text, (str, list)) else str(text)})
    body: Dict[str, Any] = {"messages": out}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]
    return body


def from_anthropic(data: Dict[str, Any]) -> Dict[str, Any]:
    text, calls = [], []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id"), "type": "function", "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input") or {})}})
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    stop = data.get("stop_reason")
    usage = data.get("usage") or {}
    finish = "tool_calls" if calls else {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}.get(stop, "stop")
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)}}


class RoutedClient:
    def __init__(self, function: str, registry: SecureRegistry, local: Any, ledger: Optional[Ledger] = None,
                 level: str = reasoning.INHERIT, session: Optional[requests.Session] = None, loader: Any = None) -> None:
        functions.spec(function)
        self.loader = loader                                 # QwenLoader: makes sure the model is there before this server is used
        self.function, self.registry, self.local = function, registry, local
        self.ledger = ledger or Ledger()
        self.level = level
        self.http = session or requests.Session()
        self.last_provider: Optional[str] = None            # who answered the last call, for the page and the audit

    # the loaded model's own client carries these; the loop reads them
    @property
    def base_url(self) -> str:
        return getattr(self.local, "base_url", "")

    def metrics(self, *a: Any, **k: Any) -> Any:
        return self.local.metrics(*a, **k)

    # -- the call ---------------------------------------------------------------------------------------------

    def chat_completion(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
                        tool_choice: Any = None, temperature: float = 0.0, max_tokens: Optional[int] = None, timeout: float = 60.0,
                        response_format: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None,
                        on_delta: Optional[Callable[[str, str], None]] = None) -> Dict[str, Any]:
        chain = self.registry.fallback_chain(self.function)
        spoken = [False]

        def tell(kind: str, text: str) -> None:
            spoken[0] = True
            on_delta(kind, text)
        stream = tell if on_delta is not None else None
        failure: Optional[BasaltError] = None
        for n, pid in enumerate(chain):
            if pid and TYPES.get((self.registry._find(self.registry.load(), pid) or {}).get("type") or "", {}).get("this_server"):
                pid = None                                   # "Basalt (this server)" is the loaded model, whatever the way it is named
            ok, why = self.registry.check(self.function, pid)
            if not ok:
                failure = NotAllowed(why)
                continue
            try:
                if pid is None:
                    self.last_provider = None
                    if self.loader is not None:
                        self.loader.ensure(self._route_here())
                    return self.local.chat_completion(messages=messages, tools=tools, tool_choice=tool_choice, temperature=temperature,
                                                      max_tokens=max_tokens, timeout=timeout, response_format=response_format,
                                                      extra=extra, **({"on_delta": stream} if stream else {}))
                self.last_provider = pid
                return self._call_provider(pid, messages, tools, tool_choice, temperature, max_tokens, timeout, response_format, extra, stream)
            except BasaltError as exc:
                failure = exc
                if spoken[0] or n == len(chain) - 1:        # what was written cannot be taken back; and the last route has no other
                    raise
        raise failure or NotAllowed(f"{self.function} has no route")

    def _route_here(self) -> Optional[Dict[str, Any]]:
        """The provider record this function is pointed at when it is this server (it may carry a load profile)."""
        data = self.registry.load()
        prov = self.registry._find(data, (data.get("assignments") or {}).get(self.function) or "")
        return prov if prov and TYPES.get(prov.get("type") or "", {}).get("this_server") else None

    def _call_provider(self, pid: str, messages, tools, tool_choice, temperature, max_tokens, timeout, response_format, extra, on_delta) -> Dict[str, Any]:
        data = self.registry.load()
        prov = self.registry._find(data, pid)
        spec = TYPES.get(prov.get("type") or "", {})
        capped = self.ledger.over_cap(prov)
        if capped:
            raise NotAllowed(capped)
        api = spec.get("api", "")
        if api == "cli":
            return self._cli(prov, messages)
        resolved = self.registry.endpoint_for(self.function, carica=False)
        if resolved is None:
            raise NotAllowed(f"{self.function} has no provider")
        base, key, model = resolved
        if not base:
            raise NotAllowed(f"{prov.get('name') or pid} has no address: set it first")
        headers = dict(self.registry.headers_for(self.function))
        if api == "anthropic":
            return self._anthropic(prov, base, key, model, messages, tools, temperature, max_tokens, timeout, on_delta)
        body: Dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if response_format is not None:
            body["response_format"] = response_format
        if extra:
            body.update({k: v for k, v in extra.items() if k not in reasoning._REASONING_KEYS})
        shaped = self.registry.reasoning_for(self.function, self.level, base=body, max_tokens=max_tokens)
        body = dict(shaped["params"])                          # the request with the reasoning said in this provider's own words
        if shaped["max_tokens"]:
            body["max_tokens"] = shaped["max_tokens"]
        if on_delta is not None:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        headers.setdefault("Authorization", f"Bearer {key}")
        headers["Content-Type"] = "application/json"
        try:
            resp = self.http.post(base.rstrip("/") + "/chat/completions", data=json.dumps(body), headers=headers, timeout=timeout, stream=on_delta is not None)
        except requests.RequestException as exc:
            raise BasaltError(0, f"cannot reach {prov.get('name') or pid}: {exc}") from None
        if resp.status_code >= 400:
            raise _http_error(prov, resp)
        result = _collect_stream(_lines(resp), on_delta) if on_delta is not None else resp.json()
        self.ledger.record(pid, int((result.get("usage") or {}).get("total_tokens") or 0))
        return result

    def _anthropic(self, prov, base, key, model, messages, tools, temperature, max_tokens, timeout, on_delta) -> Dict[str, Any]:
        body = to_anthropic(messages, tools)
        body.update({"model": model, "max_tokens": max_tokens or DEFAULT_MAX_TOKENS, "temperature": temperature})
        shaped = self.registry.reasoning_for(self.function, self.level, base={}, max_tokens=body["max_tokens"])
        for k, v in shaped["params"].items():
            body[k] = v
        if shaped.get("max_tokens"):
            body["max_tokens"] = shaped["max_tokens"]
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        try:
            resp = self.http.post(base.rstrip("/") + "/messages", data=json.dumps(body), headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise BasaltError(0, f"cannot reach {prov.get('name') or prov.get('id')}: {exc}") from None
        if resp.status_code >= 400:
            raise _http_error(prov, resp)
        result = from_anthropic(resp.json())
        if on_delta is not None and result["choices"][0]["message"].get("content"):
            on_delta("content", result["choices"][0]["message"]["content"])          # whole, not in pieces: this route does not stream
        self.ledger.record(prov["id"], int(result["usage"]["total_tokens"]))
        return result

    def _cli(self, prov: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A command-line agent writes text only: no tools, nothing streams."""
        from harness.llmswitch.agents import AgentError, genera
        prompt = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages if isinstance(m.get("content"), str))
        try:
            text = genera(TYPES[prov["type"]].get("agent") or prov["type"].replace("_cli", ""), prompt, model=prov.get("model", ""))
        except AgentError as exc:
            raise BasaltError(503, str(exc)) from None
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {}}
