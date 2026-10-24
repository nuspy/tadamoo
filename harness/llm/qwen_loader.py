"""Makes sure the model a function points at is loaded: the narrow interface to the Qwen Loader that the routes use.

The Loader stays a thing of its own (it loads and starts the weights); a function that is routed to this server asks only this:
*is the model there, and if not, load it with the profile of the route*. The rules:

* already loaded: use it as it is (a route's profile is not applied over a model that is up: reloading loses the conversation);
* loading: wait for it, and say who is waiting;
* nothing loaded: load with the route's profile and wait; with no profile and a server that loads by itself on the first request
  (`auto_load`), do nothing: its own setting is not overridden; with neither, the default profile;
* the Loader refuses (another big model holds the memory, a load is already running): the refusal is the answer, in the Loader's words;
* never unloads.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from harness.client import BasaltError


class QwenNotReady(BasaltError):
    def __init__(self, message: str) -> None:
        super().__init__(409, message)


class QwenLoader:
    def __init__(self, endpoint: str, api_key: Optional[str] = None, wait_s: float = 900.0, poll_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 on_wait: Optional[Callable[[str], None]] = None) -> None:
        self.endpoint, self.api_key = endpoint.rstrip("/"), api_key
        self.wait_s, self.poll_s, self.sleep, self.clock = wait_s, poll_s, sleep, clock
        self.on_wait = on_wait                           # told what the wait is for ("loading the model for the Chat")

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        req = urllib.request.Request(self.endpoint + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}

    def _auto_load(self) -> bool:
        code, data = self._call("GET", "/v1/status")
        return code == 200 and bool(data.get("auto_load"))

    def status(self) -> Optional[Dict[str, Any]]:
        code, data = self._call("GET", "/api/loader/status")
        return data if code == 200 else None

    def _profile_values(self, name: str) -> Tuple[Dict[str, Any], str]:
        """(values, model file) of a load profile: the route's, else the server's default."""
        code, data = self._call("GET", "/api/profiles")
        if code != 200:
            return {}, ""
        loads = {p["name"]: p for p in data.get("load", []) if isinstance(p, dict)}
        chosen = loads.get(name) or loads.get(data.get("default") or "")
        if chosen is None:
            return {}, ""
        return dict(chosen.get("values") or {}), chosen.get("model") or ""

    def ensure(self, route: Optional[Dict[str, Any]] = None) -> str:
        """Returns what was done ("loaded", "waited", "started") or raises `QwenNotReady` with the reason."""
        st = self.status()
        if st is None:
            return "unknown"                              # the server does not answer: the call itself will say so
        if st.get("loaded") and not st.get("loading"):
            return "loaded"
        did = "waited"
        profile = str((route or {}).get("profile") or "")
        if not st.get("loading") and not profile and self._auto_load():
            return "auto"                                 # the server loads the model by itself on the first request: nothing to override
        if not st.get("loading"):
            values, model = self._profile_values(str((route or {}).get("profile") or ""))
            body: Dict[str, Any] = {"values": values}
            if model:
                body["model"] = model
            code, data = self._call("POST", "/api/loader/load", body, timeout=30.0)
            if code not in (200, 202):
                error = (data.get("error") or {}) if isinstance(data, dict) else {}
                if error.get("code") == "busy":
                    did = "waited"                        # a load is already running (someone else started it): wait for it
                else:
                    raise QwenNotReady(error.get("message") or f"the model could not be loaded (the Loader answered {code})")
            else:
                did = "started"
        if self.on_wait:
            self.on_wait("loading the model")
        end = self.clock() + self.wait_s
        while self.clock() < end:
            st = self.status()
            if st and st.get("loaded") and not st.get("loading"):
                return did
            self.sleep(self.poll_s)
        raise QwenNotReady("the model did not finish loading in time")
