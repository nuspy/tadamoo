"""Agent2Agent (A2A) client, protocol version 1.x, HTTP+JSON binding only.

It reads an agent's Agent Card from `/.well-known/agent-card.json`, picks its first `HTTP+JSON` interface, sends one
text message with `POST {url}/message:send` and returns the text of the result. A task that is still running is polled
with `GET {url}/tasks/{id}` until it ends, and cancelled with `POST {url}/tasks/{id}:cancel` when the caller cancels or
the time runs out. Streaming, push notifications, files, authenticated extended cards and a follow-up to a task that
asks for input are not done: such a task is reported as it stands. Agents are named by the person (a file of
addresses), never by the model, and everything an agent answers is untrusted text.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

CARD_PATH = "/.well-known/agent-card.json"
MEDIA_TYPE = "application/a2a+json"
BINDING = "HTTP+JSON"
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
INTERRUPTED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
REQUIRED_CARD_FIELDS = ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes",
                        "defaultOutputModes", "skills")
TEXT_MAX = 20_000
BODY_MAX = 4 << 20
POLL_S = 1.0


class A2AError(RuntimeError):
    """The card is unusable, the agent answered with an error, or it did not finish in time."""


def _version(interface: Dict[str, Any]) -> str:
    """Major.Minor of an interface's protocol version (patch numbers are not part of the protocol)."""
    return ".".join(str(interface.get("protocolVersion", "")).split(".")[:2])


class A2AClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0, cancel: Optional[threading.Event] = None) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise A2AError("The address of an A2A agent starts with http:// or https://.")
        self.base_url = base_url.rstrip("/")
        self.token, self.timeout, self.cancel_flag = token, timeout, cancel
        self.card: Dict[str, Any] = {}
        self.interface: Dict[str, Any] = {}

    # -- wire ------------------------------------------------------------------------------------

    def _http(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Accept": MEDIA_TYPE + ", application/json"}
        if self.interface:
            headers["A2A-Version"] = _version(self.interface)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = MEDIA_TYPE
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(BODY_MAX + 1)
        except urllib.error.HTTPError as exc:
            raise A2AError(self._error_text(exc.code, exc.read(8192))) from None
        except (urllib.error.URLError, OSError) as exc:
            raise A2AError(f"The agent could not be reached: {getattr(exc, 'reason', exc)}") from None
        if len(raw) > BODY_MAX:
            raise A2AError("The agent's answer is too large.")
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise A2AError("The agent's answer is not JSON.") from None
        if not isinstance(parsed, dict):
            raise A2AError("The agent's answer is not a JSON object.")
        return parsed

    @staticmethod
    def _error_text(status: int, raw: bytes) -> str:
        try:
            err = json.loads(raw.decode("utf-8", errors="replace")).get("error") or {}
        except (ValueError, AttributeError):
            err = {}
        reasons = [d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")]
        detail = err.get("message") or ""
        return f"The agent answered HTTP {status}" + (f" ({', '.join(reasons)})" if reasons else "") + (f": {detail}" if detail else ".")

    # -- discovery and tasks ---------------------------------------------------------------------

    def discover(self) -> Dict[str, Any]:
        """Reads the Agent Card and chooses the interface to speak to."""
        card = self._http("GET", self.base_url + CARD_PATH)
        missing = [f for f in REQUIRED_CARD_FIELDS if f not in card]
        if missing:
            raise A2AError(f"The Agent Card lacks required fields: {', '.join(missing)}.")
        interfaces = [i for i in card["supportedInterfaces"] if isinstance(i, dict)] if isinstance(card["supportedInterfaces"], list) else []
        rest = [i for i in interfaces if i.get("protocolBinding") == BINDING and isinstance(i.get("url"), str)]
        if not rest:
            offered = ", ".join(sorted({str(i.get("protocolBinding")) for i in interfaces})) or "none"
            raise A2AError(f"The agent has no {BINDING} interface (it offers: {offered}); only {BINDING} is supported.")
        usable = [i for i in rest if _version(i).split(".")[0] == "1"]
        if not usable:
            raise A2AError(f"The agent's {BINDING} interface speaks version {_version(rest[0])}; only 1.x is supported.")
        self.card, self.interface = card, usable[0]
        return card

    def send(self, text: str, wait_s: float = 600.0) -> Dict[str, Any]:
        """One message to the agent; {"agent", "state", "task_id", "text"}. Needs `discover()` first."""
        if not self.interface:
            raise A2AError("Read the Agent Card first.")
        url = self.interface["url"].rstrip("/")
        body: Dict[str, Any] = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]},
                                "configuration": {"acceptedOutputModes": ["text/plain"]}}
        if self.interface.get("tenant"):
            body["tenant"] = self.interface["tenant"]
        reply = self._http("POST", url + "/message:send", body)
        if isinstance(reply.get("message"), dict):
            return {"agent": self.card.get("name", ""), "state": "message", "task_id": "", "text": _parts_text(reply["message"].get("parts"))}
        task = reply.get("task")
        if not isinstance(task, dict) or not task.get("id"):
            raise A2AError("The agent's answer holds neither a task nor a message.")
        deadline = time.monotonic() + wait_s
        while _state(task) not in TERMINAL | INTERRUPTED:
            if (self.cancel_flag is not None and self.cancel_flag.is_set()) or time.monotonic() > deadline:
                why = "the run was cancelled" if self.cancel_flag is not None and self.cancel_flag.is_set() else f"it did not finish in {wait_s:g} seconds"
                try:
                    self._http("POST", f"{url}/tasks/{task['id']}:cancel", {})
                except A2AError:
                    pass
                raise A2AError(f"The task was cancelled because {why}.")
            time.sleep(POLL_S)
            task = self._http("GET", f"{url}/tasks/{task['id']}")
        return {"agent": self.card.get("name", ""), "state": _state(task), "task_id": str(task["id"]), "text": _task_text(task)}


def _state(task: Dict[str, Any]) -> str:
    status = task.get("status")
    return str(status.get("state", "")) if isinstance(status, dict) else ""


def _parts_text(parts: Any) -> str:
    return "\n".join(p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str))


def _task_text(task: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for artifact in task.get("artifacts") or []:
        if isinstance(artifact, dict):
            chunks.append(_parts_text(artifact.get("parts")))
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    if isinstance(status.get("message"), dict):
        chunks.append(_parts_text(status["message"].get("parts")))
    return "\n\n".join(c for c in chunks if c).strip()[:TEXT_MAX]


def ask_agent(base_url: str, text: str, token: str = "", cancel: Optional[threading.Event] = None, wait_s: float = 600.0) -> Dict[str, Any]:
    client = A2AClient(base_url, token=token, cancel=cancel)
    client.discover()
    return client.send(text, wait_s)
