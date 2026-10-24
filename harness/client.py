"""HTTP Client for Basalt inference server.

Provides OpenAI-compatible chat completions, extraction and formatting of reasoning
content, and server/telemetry metrics retrieval.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple


THINK_REGEX = re.compile(r"<think>(.*?)</think>", re.DOTALL)


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


class BasaltClient:
    """Client for local Basalt server."""

    def __init__(self, base_url: str = "http://127.0.0.1:7232", api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "BasaltHarness/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def metrics(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Fetches telemetry and engine status from /metrics."""
        req = urllib.request.Request(f"{self.base_url}/metrics", headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        response_format: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Sends a request to /v1/chat/completions."""
        payload: Dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
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

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            raise BasaltError(e.code, detail or f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise BasaltError(0, f"cannot reach the Basalt server at {self.base_url}: {e}") from None
