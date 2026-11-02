"""HTTP Client for Basalt inference server.

Provides OpenAI-compatible chat completions, extraction and formatting of reasoning
content, and server/telemetry metrics retrieval.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional, Tuple


THINK_REGEX = re.compile(r"<think>(.*?)</think>", re.DOTALL)
CONNECT_S = 4.0                # a connection that has not gone through by now is dropped and tried again, not waited for until the system gives up
CONNECT_TRIES = 4
RESET_TRIES = 3                # a request whose connection was closed under it before any answer came is sent again, up to this many times


class _Connection(http.client.HTTPConnection):
    """A connection that tries its connect a few times with a short wait each. Only the connect is repeated, never a request already sent."""

    def connect(self) -> None:
        last: Optional[OSError] = None
        for _ in range(CONNECT_TRIES):
            try:
                wait = CONNECT_S if not self.timeout else min(CONNECT_S, float(self.timeout))
                self.sock = socket.create_connection((self.host, self.port), wait, self.source_address)
                self.sock.settimeout(self.timeout)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return
            except OSError as exc:
                last = exc
        raise last if last is not None else OSError("cannot connect")


class _Handler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_Connection, req)


_OPENER = urllib.request.build_opener(_Handler)


def open_url(req: urllib.request.Request, timeout: float) -> Any:
    """urlopen with a connect that is repeated when it does not go through quickly (a connection to this PC that the system lets hang for 21 seconds)."""
    return _OPENER.open(req, timeout=timeout)


class BasaltError(RuntimeError):
    """The server refused or failed a request; `status` is the HTTP status (0 when unreachable)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def extract_reasoning_and_content(message: Dict[str, Any]) -> Tuple[str, str]:
    """Extracts reasoning and pure content from a message dict.

    Supports explicit `reasoning_content` (OpenAI format), inline `<think>...</think>` tags, and the
    template form where the opening tag is part of the prompt and only `</think>` is generated.
    """
    reasoning = message.get("reasoning_content") or ""
    content = message.get("content") or ""

    if not reasoning:
        if "<think>" in content:
            match = THINK_REGEX.search(content)
            if match:
                reasoning = match.group(1).strip()
                content = THINK_REGEX.sub("", content).strip()
            else:  # opened and never closed: everything after the tag is unfinished reasoning
                before, _, after = content.partition("<think>")
                reasoning, content = after.strip(), before.strip()
        elif "</think>" in content:
            reasoning, _, content = content.partition("</think>")

    return reasoning.strip(), content.strip()


class StreamCancelled(Exception):
    """Raised by an `on_delta` callback to stop reading a stream: the connection is closed and the server stops writing."""


def _is_json_text(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _collect_stream(resp: Any, on_delta: Callable[[str, str], None]) -> Dict[str, Any]:
    """Reads a chat completion stream, hands the text on as it comes and returns the whole answer."""
    content: List[str] = []
    reasoning: List[str] = []
    calls: Dict[int, Dict[str, Any]] = {}
    finish, usage, meta = None, None, {}
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue                                       # a keep-alive comment or a blank line
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if isinstance(chunk.get("error"), dict):
            raise BasaltError(500, str(chunk["error"].get("message") or "the engine reported an error"))
        if chunk.get("usage"):
            usage = chunk["usage"]
        if chunk.get("timings"):
            meta["timings"] = chunk["timings"]
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            reasoning.append(delta["reasoning_content"])
            on_delta("reasoning", delta["reasoning_content"])
        if delta.get("content"):
            content.append(delta["content"])
            on_delta("content", delta["content"])
        for tc in delta.get("tool_calls") or []:           # a call arrives in pieces: merge them by index
            cur = calls.setdefault(tc.get("index", len(calls)), {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
            cur["id"] = tc.get("id") or cur["id"]
            fn = tc.get("function") or {}
            cur["function"]["name"] += fn.get("name") or ""
            cur["function"]["arguments"] += fn.get("arguments") or ""
        finish = choice.get("finish_reason") or finish
    if finish is None:
        raise BasaltError(0, "the answer stopped before it was complete")
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    whole = [calls[i] for i in sorted(calls)]
    if finish != "tool_calls":
        whole = [c for c in whole if _is_json_text(c["function"]["arguments"])]       # a call the output ended inside
    if whole:
        message["tool_calls"] = whole
    meta["cut_calls"] = len(calls) - len(whole)                                     # calls left out because they ended mid-argument
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage or {}, **meta}


class BasaltClient:
    """Client for local Basalt server."""

    def __init__(self, base_url: str = "http://127.0.0.1:7232", api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.call_timeout: Optional[float] = 60.0       # seconds a model call may take when the caller gives none; None: no limit (a whole answer arrives at once when it is not streamed)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "BasaltHarness/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if os.environ.get("BASALT_COMPRESS") == "1":
            headers["X-Basalt-Compress"] = "1"          # the server may compress a streamed conversation (Billion Context)
        return headers

    def context_window(self, timeout: float = 3.0) -> Optional[int]:
        """The tokens one request can hold, as /v1/status reports them (each parallel slot holds this many); None when the
        model is not loaded or the server gives no number. Raises BasaltError when the server cannot be reached."""
        status = self._get("/v1/status", timeout)
        if status.get("loaded") is False:
            return None                                    # an engine that is not loaded reports a default, not its context
        context = status.get("context")
        value = (context.get("native") if isinstance(context, dict) else None) or status.get("cache_max_tokens")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None

    def metrics(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Fetches telemetry and engine status from /metrics."""
        return self._get("/metrics", timeout)

    SIDE_ASK_S = 30.0          # how long the server's answer about its technical session is kept
    SIDE_RETRY_S = 3.0         # how long "could not be asked" is kept: a connection that failed once is no proof the session is gone

    def side_context(self, timeout: float = 3.0) -> int:
        """The tokens the server's technical session holds, as /v1/status reports them; 0 when it has none or cannot
        be reached. Asked at most once per SIDE_ASK_S."""
        asked = self.__dict__.get("_side")
        if asked is not None and time.monotonic() - asked[0] < (self.SIDE_ASK_S if asked[2] else self.SIDE_RETRY_S):
            return asked[1]
        answered = True
        try:
            try:
                status = self._get("/v1/status", timeout)
            except BasaltError as first:
                if first.status != 0:
                    raise
                time.sleep(0.5)                              # a connection that did not go through: once more before saying there is no session
                status = self._get("/v1/status", timeout)
            side = (status.get("concurrency") or {}).get("side_slot")
            tokens = int(side.get("context") or 0) if isinstance(side, dict) else 0
        except (BasaltError, TypeError, ValueError):
            tokens, answered = 0, False
        self._side = (time.monotonic(), tokens, answered)
        return tokens

    def _get(self, path: str, timeout: float) -> Dict[str, Any]:
        req = urllib.request.Request(f"{self.base_url}{path}", headers=self._headers(), method="GET")
        try:
            with open_url(req, timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            raise BasaltError(e.code, detail or f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise BasaltError(0, f"cannot reach the Basalt server at {self.base_url}: {e}") from None

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str | Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        """Sends a request to /v1/chat/completions. With `on_delta(kind, text)` the answer is asked for as a stream
        and each piece of text ("content" or "reasoning") is handed on as it arrives; the return value is the same
        whole answer as without it. Without a `temperature` or `max_tokens` the request carries none, so the server's own apply."""
        timeout = timeout if timeout is not None else self.call_timeout
        payload: Dict[str, Any] = {"messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format
        if extra:
            payload.update(extra)                  # sampling options such as seed or top_p
        if on_delta is not None:
            payload["stream"] = True

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        shown = [False]                              # a piece of text already handed on: the request is not made again after that
        if on_delta is not None:
            hand_on = on_delta

            def on_delta(kind: str, text: str) -> None:        # noqa: F811
                shown[0] = True
                hand_on(kind, text)
        for attempt in range(RESET_TRIES):
            try:
                with open_url(req, timeout) as resp:
                    if on_delta is not None:
                        return _collect_stream(resp, on_delta)
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
                except Exception:
                    detail = ""
                raise BasaltError(e.code, detail or f"HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                reset = isinstance(e, ConnectionError) or isinstance(getattr(e, "reason", None), ConnectionError)
                if reset and not shown[0] and attempt + 1 < RESET_TRIES:     # the connection was closed under the request: nothing came back yet
                    time.sleep(1.0 + attempt)
                    continue
                raise BasaltError(0, f"cannot reach the Basalt server at {self.base_url}: {e}") from None
        raise BasaltError(0, f"cannot reach the Basalt server at {self.base_url}")
